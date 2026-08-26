"""启动时交叉校验池配置与 X Layer 链上事实。"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from okxlp.campaign.gate import FactGate, load_fact_gate
from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import AppConfig, PoolConfig, load_config
from okxlp.exec.authorization import RunMode, load_run_mode
from okxlp.uniswap.pool import PoolSnapshot, UniswapV3Pool


LOGGER = logging.getLogger("okxlp.campaign.verifier")


class VerificationError(RuntimeError):
    """表示配置与链上事实不一致，必须拒绝启动。"""


@dataclass(frozen=True)
class VerificationReport:
    """链上交叉校验通过后的只读报告。"""

    verified_pool_ids: tuple[str, ...]
    block: int


def _display(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _difference(path: str, configured: Any, onchain: Any) -> str:
    return f"- {path}：配置值={_display(configured)}，链上值={_display(onchain)}"


def _has_code(rpc: Any, address: str, block: int) -> bool:
    code = rpc.call("eth_getCode", [address, hex(block)])
    if code == "0x":
        return False
    try:
        return int(code, 16) != 0
    except (TypeError, ValueError) as error:
        raise VerificationError(f"代币 {address} 的 eth_getCode 返回值无效：{code}") from error


def _pool_differences(pool: PoolConfig, snapshot: PoolSnapshot, rpc: Any) -> list[str]:
    prefix = pool.pool_id
    differences: list[str] = []
    checks = (
        (f"{prefix}.token0.address", pool.token0.address, snapshot.token0.address.lower()),
        (f"{prefix}.token1.address", pool.token1.address, snapshot.token1.address.lower()),
        (f"{prefix}.fee_bps", pool.fee_bps, Decimal(snapshot.fee) / Decimal(100)),
        (f"{prefix}.tick_spacing", pool.tick_spacing, snapshot.tick_spacing),
        (f"{prefix}.token0.decimals", pool.token0.decimals, snapshot.token0.decimals),
        (f"{prefix}.token1.decimals", pool.token1.decimals, snapshot.token1.decimals),
    )
    for path, configured, onchain in checks:
        if configured != onchain:
            differences.append(_difference(path, configured, onchain))
    for name, token in (("token0", pool.token0), ("token1", pool.token1)):
        if not _has_code(rpc, token.address, snapshot.block):
            differences.append(_difference(f"{prefix}.{name}.has_code", "存在", "不存在"))
    return differences


def verify_campaign(
    config: AppConfig,
    rpc: Any,
    *,
    pool_factory: Callable[[Any, str], Any] = UniswapV3Pool,
) -> VerificationReport:
    """验证所有池；收集全部差异后一次性拒绝启动。"""
    rpc.ensure_chain_id()
    differences: list[str] = []
    verified: list[str] = []
    block = 0
    for pool in config.pools:
        if pool.uniswap_version != "v3":
            differences.append(_difference(f"{pool.pool_id}.uniswap_version", pool.uniswap_version, "仅支持 v3 校验"))
            continue
        snapshot = pool_factory(rpc, pool.address).snapshot()
        block = snapshot.block
        pool_differences = _pool_differences(pool, snapshot, rpc)
        differences.extend(pool_differences)
        if not pool_differences:
            verified.append(pool.pool_id)
    if differences:
        raise VerificationError("链上配置校验失败，拒绝启动：\n" + "\n".join(differences))
    return VerificationReport(tuple(verified), block)


def run_startup(
    config_path: Path = Path("config/pools.yaml"),
    facts_path: Path = Path("config/facts.yaml"),
) -> tuple[VerificationReport, FactGate]:
    """执行只读启动闸门并返回校验报告。"""
    config = load_config(config_path)
    gate = load_fact_gate(facts_path)
    gate.log_startup()
    rpc = JsonRpcClient(config.chain.rpc_urls, chain_id=config.chain.chain_id)
    report = verify_campaign(config, rpc)
    return report, gate


def main() -> int:
    """提供可独立执行的启动校验命令。"""
    parser = argparse.ArgumentParser(description="校验池配置与活动事实闸门")
    parser.add_argument("--config", type=Path, default=Path("config/pools.yaml"))
    parser.add_argument("--facts", type=Path, default=Path("config/facts.yaml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run_mode = load_run_mode()
        report, gate = run_startup(args.config, args.facts)
    except Exception as error:
        LOGGER.error("启动校验未通过：%s", error)
        return 2
    effective_live = run_mode is RunMode.LIVE and not gate.forced_dry_run
    if effective_live:
        mode = "live（可请求实盘）"
    elif run_mode is RunMode.LIVE:
        mode = "dry_run（事实闸门强制：存在 live 级未核实事实）"
    else:
        mode = "dry_run（禁止广播）"
    LOGGER.info("链上校验通过：池=%s，区块=%s，模式=%s", ",".join(report.verified_pool_ids), report.block, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
