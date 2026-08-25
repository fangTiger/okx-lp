"""按小时统计池子成交量分布

用于检验「做市窗口 vs 撤出期」的成交活跃度差异。
采样方式：每小时取一段连续区块，统计 Swap 事件笔数与成交额后外推。
"""
import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
RPC = "https://rpc.xlayer.tech"
POOL = "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37"
SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SAMPLE = 300          # 每小时采样区块数（约 5 分钟）
SPAN = 100            # 公共 RPC 允许的单次 getLogs 最大跨度


def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=CTX) as f:
        out = json.loads(f.read())
    if "error" in out:
        raise RuntimeError(str(out["error"])[:80])
    return out["result"]


def s256(hexword):
    """解码 int256（Swap 事件的 amount 为有符号数）"""
    v = int(hexword, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def classify(et):
    """判断该 ET 时刻属于做市窗口还是撤出期（阿姆斯特丹 ∪ NASDAQ 开市即撤出）"""
    if et.weekday() >= 5:
        return "做市窗口"
    minutes = et.hour * 60 + et.minute
    ams = 3 * 60 <= minutes < 11 * 60 + 40
    ny = 9 * 60 + 30 <= minutes < 16 * 60
    return "撤出期" if (ams or ny) else "做市窗口"


def main():
    head = int(rpc("eth_blockNumber", []), 16)
    scale = 3600 / SAMPLE
    print(f"当前区块 {head}，每小时采样 {SAMPLE} 块后外推\n", flush=True)
    print(f"{'ET 时刻':<9}{'时段':<11}{'笔数':>5}{'外推笔/时':>10}{'外推成交额/时':>15}", flush=True)

    buckets = {}
    for h in range(24):
        top = head - h * 3600
        cnt, vol = 0, 0.0
        for start in range(top - SAMPLE, top, SPAN):
            try:
                logs = rpc("eth_getLogs", [{"address": POOL, "topics": [SWAP],
                                            "fromBlock": hex(start),
                                            "toBlock": hex(min(start + SPAN - 1, top))}])
            except Exception:
                continue
            for lg in logs:
                cnt += 1
                vol += abs(s256(lg["data"][2:][64:128])) / 1e6   # amount1 = USDC
            time.sleep(0.03)
        try:
            ts = int(rpc("eth_getBlockByNumber", [hex(top), False])["timestamp"], 16)
        except Exception:
            continue
        et = datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo("America/New_York"))
        seg = classify(et)
        print(f"{et:%m-%d %H:%M}  {seg:<11}{cnt:>5}{cnt * scale:>10.1f}{vol * scale:>15,.0f}", flush=True)
        b = buckets.setdefault(seg, [0, 0.0, 0])
        b[0] += cnt
        b[1] += vol
        b[2] += 1

    print(flush=True)
    for seg, (cnt, vol, n) in sorted(buckets.items()):
        fee = vol / n * scale * 0.0005
        print(f"{seg}：{n} 小时样本，共 {cnt} 笔", flush=True)
        print(f"    每小时约 {cnt / n * scale:.1f} 笔，成交额 ${vol / n * scale:,.0f}，"
              f"池子手续费 ${fee:.2f}/小时", flush=True)


if __name__ == "__main__":
    main()
