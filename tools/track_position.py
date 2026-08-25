"""仓位追踪器：为 F2/F3 校准采集真值

每 60 秒记录一次自己的头寸状态与已累积手续费，
与 observer 的池子快照配合，可重建任意时段的手续费份额。
输出：log/position_{tokenId}_{日期}.jsonl
"""
import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

import certifi

getcontext().prec = 60
CTX = ssl.create_default_context(cafile=certifi.where())
RPC = "https://rpc.xlayer.tech"
POOL = "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37"
NPM = "0x315e413a11ab0df498ef83873012430ca36638ae"
TOKEN_ID = 15857
INTERVAL = 60
OWNER = ""      # 启动时由 owner_of() 填充


def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=CTX) as f:
        out = json.loads(f.read())
    if "error" in out:
        raise RuntimeError(str(out["error"])[:120])
    return out["result"]


def word(hexstr, i):
    return hexstr[2:][i * 64:(i + 1) * 64]


def signed(x, bits=256):
    v = int(x, 16)
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def owner_of():
    """读取头寸 NFT 的持有者地址"""
    res = rpc("eth_call", [{"to": NPM, "data": "0x6352211e" + f"{TOKEN_ID:064x}"}, "latest"])
    return "0x" + res[-40:]


def collectable():
    """用 eth_call 模拟 collect，取得当前真实可领取的手续费"""
    max_u128 = (1 << 128) - 1
    data = ("0xfc6f7865" + f"{TOKEN_ID:064x}" + "0" * 24 + OWNER[2:]
            + f"{max_u128:064x}" + f"{max_u128:064x}")
    res = rpc("eth_call", [{"to": NPM, "from": OWNER, "data": data}, "latest"])
    return int(res[2:66], 16) / 1e18, int(res[66:130], 16) / 1e6


def snapshot():
    """采集一次头寸 + 池子的联合快照"""
    pos = rpc("eth_call", [{"to": NPM, "data": "0x99fbab88" + f"{TOKEN_ID:064x}"}, "latest"])
    slot0 = rpc("eth_call", [{"to": POOL, "data": "0x3850c7bd"}, "latest"])
    active = int(rpc("eth_call", [{"to": POOL, "data": "0x1a686502"}, "latest"]), 16)
    block = int(rpc("eth_blockNumber", []), 16)

    tick_lower, tick_upper = signed(word(pos, 5)), signed(word(pos, 6))
    liquidity = int(word(pos, 7), 16)
    tick = signed(word(slot0, 1))
    sqrt_price = int(word(slot0, 0), 16)
    price = Decimal(sqrt_price) ** 2 / Decimal(2) ** 192 * Decimal(10) ** 12
    in_range = tick_lower <= tick < tick_upper

    # positions() 返回的 tokensOwed / feeGrowthInsideLast 是**上次操作头寸时的快照**，
    # 不碰头寸就永远不变，不能当作实时手续费。
    # 真实值要对 NPM.collect 做 eth_call —— 它内部先 pool.burn(...,0) poke 一次再结算。
    fee0, fee1 = collectable()

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": block,
        "token_id": TOKEN_ID,
        "price": float(price),
        "tick": tick,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "in_range": in_range,
        "my_liquidity": liquidity,
        "active_liquidity": active,
        # 份额：出界时为 0，因为不产生手续费
        "share": float(Decimal(liquidity) / Decimal(active)) if (in_range and active) else 0.0,
        # 实时可领取手续费（F2/F3 校准的真值）
        "fee0": fee0,
        "fee1": fee1,
        "fee_usd": fee0 * float(price) + fee1,
        # 快照值一并保留，便于对照说明两者的差异
        "tokens_owed0_stale": int(word(pos, 10), 16) / 1e18,
        "tokens_owed1_stale": int(word(pos, 11), 16) / 1e6,
    }


def main():
    global OWNER
    OWNER = owner_of()
    print(f"仓位追踪器启动：#{TOKEN_ID}，持有者 {OWNER}，每 {INTERVAL} 秒记录一次", flush=True)
    while True:
        try:
            rec = snapshot()
            # 输出路径每轮重算，跨零点自动滚动到新日期文件
            out = Path(f"log/position_{TOKEN_ID}_{datetime.now().strftime('%Y-%m-%d')}.jsonl")
            with out.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{rec['ts']}  价格 {rec['price']:.2f}  在区间 {rec['in_range']}  "
                  f"份额 {rec['share']*100:.4f}%  手续费 ${rec['fee_usd']:.6f}", flush=True)
        except Exception as exc:
            print(f"采集失败（不中断）：{exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
