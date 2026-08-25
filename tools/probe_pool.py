"""X Layer 上 Uniswap V3 池子的链上探针

只读校验：确认池子真实存在、代币与费率、当前价、活跃流动性，
并计算给定本金在 ±0.5% 区间能拿到的流动性份额。
"""
import json
import ssl
import urllib.request

import certifi

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

RPC = "https://rpc.xlayer.tech"
POOL = "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37"

SEL = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "tickSpacing": "0xd0c93a7c",
    "liquidity": "0x1a686502",
    "slot0": "0x3850c7bd",
    "factory": "0xc45a0155",
}
ERC20 = {"symbol": "0x95d89b41", "decimals": "0x313ce567", "name": "0x06fdde03"}


def rpc(method, params, _id=1):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method} {params}: {out['error']}")
    return out["result"]


def call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def word(hexstr, i):
    """取第 i 个 32 字节字"""
    h = hexstr[2:]
    return h[i * 64:(i + 1) * 64]


def as_int(h, signed=False, bits=256):
    v = int(h, 16)
    if signed and v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


def decode_string(hexstr):
    """兼容 string 与 bytes32 两种 symbol 返回"""
    h = hexstr[2:]
    if len(h) == 64:  # bytes32
        return bytes.fromhex(h).rstrip(b"\x00").decode("utf-8", "replace")
    offset = int(h[0:64], 16) * 2
    length = int(h[offset:offset + 64], 16) * 2
    return bytes.fromhex(h[offset + 64:offset + 64 + length]).decode("utf-8", "replace")


def token_info(addr):
    code = rpc("eth_getCode", [addr, "latest"])
    info = {"address": addr, "has_code": len(code) > 2}
    if not info["has_code"]:
        return info
    info["symbol"] = decode_string(call(addr, ERC20["symbol"]))
    info["name"] = decode_string(call(addr, ERC20["name"]))
    info["decimals"] = as_int(call(addr, ERC20["decimals"]))
    bal = call(addr, "0x70a08231" + "0" * 24 + POOL[2:])
    info["pool_balance_raw"] = as_int(bal)
    return info


def main():
    print(f"链 ID : {int(rpc('eth_chainId', []), 16)}")
    print(f"区块高 : {int(rpc('eth_blockNumber', []), 16)}")

    code = rpc("eth_getCode", [POOL, "latest"])
    print(f"池合约字节码长度 : {len(code) // 2} bytes")
    if len(code) <= 2:
        print("!! 该地址在 X Layer 上没有合约代码，池地址有误")
        return

    t0 = call(POOL, SEL["token0"])
    t1 = call(POOL, SEL["token1"])
    a0 = "0x" + t0[-40:]
    a1 = "0x" + t1[-40:]
    fee = as_int(call(POOL, SEL["fee"]))
    tick_spacing = as_int(call(POOL, SEL["tickSpacing"]), signed=True, bits=24)
    factory = "0x" + call(POOL, SEL["factory"])[-40:]
    liq = as_int(call(POOL, SEL["liquidity"]))
    s0 = call(POOL, SEL["slot0"])
    sqrt_p = as_int(word(s0, 0))
    # int24 在 ABI 里按 256 位符号扩展存放，必须按 256 位解符号再校验范围
    tick = as_int(word(s0, 1), signed=True, bits=256)
    assert -887272 <= tick <= 887272, f"tick 越界: {tick}"

    i0 = token_info(a0)
    i1 = token_info(a1)

    print(f"\nfactory     : {factory}")
    print(f"fee         : {fee} ({fee / 10000}%)")
    print(f"tickSpacing : {tick_spacing}")
    print(f"当前 tick    : {tick}")
    print(f"活跃流动性 L : {liq}")

    for tag, i in (("token0", i0), ("token1", i1)):
        print(f"\n{tag} : {i['address']}")
        print(f"  symbol/name : {i.get('symbol')} / {i.get('name')}")
        print(f"  decimals    : {i.get('decimals')}")
        d = i.get("decimals", 0)
        print(f"  池内余额     : {i['pool_balance_raw'] / 10 ** d:,.6f}")

    d0, d1 = i0["decimals"], i1["decimals"]
    raw_price = (sqrt_p / 2 ** 96) ** 2          # token1_raw per token0_raw
    human_price = raw_price * 10 ** (d0 - d1)     # token1 per token0
    print(f"\n当前价格 : 1 {i0['symbol']} = {human_price:,.6f} {i1['symbol']}")

    # 池内两腿名义价值（以 token1 计），用于粗估深度
    v0 = i0["pool_balance_raw"] / 10 ** d0 * human_price
    v1 = i1["pool_balance_raw"] / 10 ** d1
    print(f"池内名义总额（含所有区间的闲置部分）: {v0 + v1:,.0f} {i1['symbol']}")

    # ---- ±0.5% 区间下的份额测算 ----
    # V(以 token1 计) = L_raw * (2*sqrt(P) - P/sqrt(Pb) - sqrt(Pa)) / 10**d1
    print("\n=== ±0.5% 区间份额测算 ===")
    delta = 0.005
    P = raw_price
    Pa, Pb = P * (1 - delta), P * (1 + delta)
    coeff = 2 * P ** 0.5 - P / Pb ** 0.5 - Pa ** 0.5   # 每单位 L 占用的 token1 raw 数量

    # 现有活跃流动性折算成美元（token1 若为 USDC 则近似等于美元）
    active_usd = liq * coeff / 10 ** d1
    print(f"当前活跃流动性 L 折算成 ±0.5% 等效本金 : {active_usd:,.0f} {i1['symbol']}")

    print(f"\n{'投入本金':>12} {'份额':>10}")
    for capital in (2000, 5000, 10000, 15000, 25000, 50000):
        l_mine = capital * 10 ** d1 / coeff
        share = l_mine / (liq + l_mine)
        print(f"{capital:>10,} U {share:>9.2%}")

    # ---- factory 反查：确认该池确由此 factory 创建（内部一致性校验）----
    gp = call(factory, "0x1698ee82" + "0" * 24 + a0[2:] + "0" * 24 + a1[2:] + f"{fee:064x}")
    print(f"\nfactory.getPool 反查 : 0x{gp[-40:]}")
    print(f"  与池地址一致 : {('0x' + gp[-40:]).lower() == POOL.lower()}")

    # ---- ±0.5% 对应的 tick 区间（按 tickSpacing 对齐，一律向外取整）----
    # 注意：tick 是对数刻度，上下两侧不对称。
    # -0.5% 需要 |ln(0.995)|/ln(1.0001) = 50.13 tick，+0.5% 只需 49.88 tick，
    # 两侧必须分别计算，否则下边界会偏窄。与 src/okxlp/uniswap/tickmath.py 保持一致。
    import math
    ticks_down = -math.log(1 - delta) / math.log(1.0001)
    ticks_up = math.log(1 + delta) / math.log(1.0001)
    lo = int(math.floor((tick - ticks_down) / tick_spacing) * tick_spacing)
    hi = int(math.ceil((tick + ticks_up) / tick_spacing) * tick_spacing)
    print(f"\n±0.5% ≈ -{ticks_down:.2f} / +{ticks_up:.2f} ticks；tickSpacing={tick_spacing}")
    print(f"  对齐后区间 : [{lo}, {hi}]")
    print(f"  实际宽度   : -{(1 - 1.0001 ** (lo - tick)) * 100:.3f}% / +{(1.0001 ** (hi - tick) - 1) * 100:.3f}%")


if __name__ == "__main__":
    main()
