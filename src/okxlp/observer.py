"""X Layer Uniswap V3 池的只读轮询观测器。"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from okxlp.chain.rpc import ChainIdMismatchError, JsonRpcClient, RpcError
from okxlp.uniswap.pool import PoolSnapshot, UniswapV3Pool
from okxlp.uniswap.tickmath import aligned_tick_range, capital_to_liquidity, liquidity_share


LOGGER = logging.getLogger("okxlp.observer")
WIDTH = Decimal("0.005")
CAPITAL_LEVELS = (50, 100, 500, 2000, 5000)


@dataclass(frozen=True)
class PoolSettings:
    """观测器所需的最小配置。"""

    chain_id: int
    rpc_urls: tuple[str, ...]
    pool_id: str
    address: str


def load_pool_settings(path: Path, pool_id: str | None = None) -> PoolSettings:
    """从 pools.yaml 选择一个池，不以交易启用状态限制只读观测。"""
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    chain = data.get("chain", {})
    pools = data.get("pools", [])
    selected = next((item for item in pools if pool_id is None or item.get("id") == pool_id), None)
    if selected is None:
        raise ValueError(f"配置中找不到池：{pool_id or '首个条目'}")
    urls = tuple(chain.get("rpc", ()))
    if not urls:
        raise ValueError("配置中没有 RPC 节点")
    return PoolSettings(int(chain["id"]), urls, str(selected["id"]), str(selected["address"]))


def build_record(snapshot: PoolSnapshot, observed_at: datetime) -> dict[str, Any]:
    """把池快照转换为要求的 JSON 记录。"""
    lower, upper = aligned_tick_range(snapshot.tick, WIDTH, snapshot.tick_spacing)
    shares: dict[str, float] = {}
    for capital in CAPITAL_LEVELS:
        own_liquidity = capital_to_liquidity(
            Decimal(capital),
            snapshot.price,
            WIDTH,
            snapshot.token0.decimals,
            snapshot.token1.decimals,
        )
        shares[str(capital)] = float(liquidity_share(snapshot.active_liquidity, own_liquidity))
    timestamp = observed_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "ts": timestamp,
        "block": snapshot.block,
        "price": float(snapshot.price),
        "tick": snapshot.tick,
        "active_liquidity": snapshot.active_liquidity,
        "range_lower": lower,
        "range_upper": upper,
        "share_at": shares,
        "pool_balance_token0": float(snapshot.token0.balance),
        "pool_balance_token1": float(snapshot.token1.balance),
    }


def _append_record(record: dict[str, Any], log_dir: Path, observed_at: datetime) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = observed_at.astimezone().strftime("%Y-%m-%d")
    path = log_dir / f"observer_{day}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def _summary(record: dict[str, Any]) -> str:
    shares = "、".join(f"{capital}U={share:.2%}" for capital, share in record["share_at"].items())
    return (
        f"观测摘要：区块 {record['block']}，价格 {record['price']:.6f}，"
        f"tick {record['tick']}，活跃流动性 {record['active_liquidity']}，"
        f"区间 [{record['range_lower']}, {record['range_upper']}]，份额 {shares}"
    )


class Observer:
    """定时读取池状态并追加每日 JSONL。"""

    def __init__(
        self,
        pool: UniswapV3Pool,
        log_dir: Path = Path("log"),
        *,
        poll_interval: float = 30.0,
        summary_interval: float = 300.0,
    ) -> None:
        if poll_interval <= 0 or summary_interval <= 0:
            raise ValueError("轮询与摘要间隔必须大于零")
        self.pool = pool
        self.log_dir = log_dir
        self.poll_interval = poll_interval
        self.summary_interval = summary_interval
        self.stop_event = threading.Event()

    def stop(self) -> None:
        """请求观测循环优雅退出。"""
        self.stop_event.set()

    def observe_once(self, observed_at: datetime | None = None) -> dict[str, Any] | None:
        """执行一轮观测；失败时告警并返回空值。"""
        observed_at = observed_at or datetime.now(timezone.utc)
        try:
            snapshot = self.pool.snapshot()
        except ChainIdMismatchError:
            raise
        except RpcError as error:
            LOGGER.warning("本轮观测失败，将在下一轮重试：%s", error)
            return None
        record = build_record(snapshot, observed_at)
        _append_record(record, self.log_dir, observed_at)
        return record

    def run(self) -> None:
        """持续轮询，直到收到停止请求。"""
        next_poll = time.monotonic()
        next_summary = next_poll + self.summary_interval
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_poll:
                self.stop_event.wait(next_poll - now)
                continue
            record = self.observe_once()
            now = time.monotonic()
            while next_poll <= now:
                next_poll += self.poll_interval
            if record is not None and now >= next_summary:
                print(_summary(record), flush=True)
                while next_summary <= now:
                    next_summary += self.summary_interval


def _install_signals(observer: Observer) -> None:
    def handle_stop(_signum: int, _frame: Any) -> None:
        LOGGER.info("收到退出信号，正在停止观测器")
        observer.stop()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)


def main() -> None:
    """加载配置并启动只读观测循环。"""
    parser = argparse.ArgumentParser(description="观测 X Layer 上的 Uniswap V3 池")
    parser.add_argument("--config", type=Path, default=Path("config/pools.yaml"), help="池配置文件")
    parser.add_argument("--pool-id", help="要观测的池标识；默认使用首个条目")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_pool_settings(args.config, args.pool_id)
    rpc = JsonRpcClient(settings.rpc_urls, chain_id=settings.chain_id)
    try:
        rpc.ensure_chain_id()
    except ChainIdMismatchError as error:
        LOGGER.error("RPC 节点不属于 X Layer，拒绝启动：%s", error)
        return
    except RpcError as error:
        LOGGER.warning("启动时链 ID 校验失败，将继续定时重试：%s", error)
    observer = Observer(UniswapV3Pool(rpc, settings.address))
    _install_signals(observer)
    LOGGER.info("只读观测器已启动：%s，每 30 秒记录一次", settings.pool_id)
    try:
        observer.run()
    except ChainIdMismatchError as error:
        LOGGER.error("检测到错误链，观测器已停止：%s", error)
    LOGGER.info("只读观测器已退出")


if __name__ == "__main__":
    main()
