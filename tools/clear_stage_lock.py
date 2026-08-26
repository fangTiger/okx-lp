"""链上对账后受控清除主状态机的阶段锁停字段。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import load_config
from okxlp.config_validation import address as validate_address
from okxlp.config_validation import mapping, required
from okxlp.exec.reconcile import reconcile_on_startup
from okxlp.strategy.machine_state import MachineState, MachineStateStore
from okxlp.uniswap.pool import SELECTORS as POOL_SELECTORS
from okxlp.uniswap.pool import _word, decode_int
from okxlp.uniswap.portfolio import PortfolioReader
from okxlp.uniswap.tickmath import sqrt_price_x96_to_price


POOLS_CONFIG_PATH = Path("config/pools.yaml")
EXECUTION_CONFIG_PATH = Path("config/execution.yaml")
STATE_DIR = Path("log")
CONFIRMATION = "我确认清除"


class ReadOnlyContext:
    """清锁前链上对账所需的只读依赖。"""

    def __init__(self, pool: Any, rpc: Any, reader: Any, spenders) -> None:
        self.pool = pool
        self.rpc = rpc
        self.reader = reader
        self.spenders = tuple(spenders)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="链上对账后清除阶段锁停")
    parser.add_argument("--pool-id", required=True, help="目标池配置 ID")
    parser.add_argument("--owner", required=True, help="需要对账的 EVM 地址")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    return parser


def _execution_addresses(path: Path = EXECUTION_CONFIG_PATH) -> tuple[str, str]:
    try:
        root = mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "根配置")
        addresses = mapping(required(root, "addresses", "根配置"), "addresses")
        return tuple(
            validate_address(
                required(addresses, name, "addresses"), f"addresses.{name}"
            )
            for name in ("npm", "swap_router02")
        )
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"无法读取执行配置 {path}：{error}") from error


def create_read_only_context(pool_id: str) -> ReadOnlyContext:
    """仅创建读 RPC、账户 reader 与对账 spender。"""
    config = load_config(POOLS_CONFIG_PATH)
    pool = config.find_pool(pool_id)
    npm, router = _execution_addresses()
    rpc = JsonRpcClient(config.chain.rpc_urls, chain_id=config.chain.chain_id)
    reader = PortfolioReader(
        rpc,
        npm_address=npm,
        token0=pool.token0.address,
        token1=pool.token1.address,
        fee=int(pool.fee_bps * 100),
    )
    return ReadOnlyContext(pool, rpc, reader, (npm, router))


def _current_price(context: ReadOnlyContext, block: int) -> Decimal:
    slot0 = context.rpc.eth_call(
        context.pool.address, POOL_SELECTORS["slot0"], hex(block)
    )
    sqrt_price_x96 = decode_int(_word(slot0, 0))
    return sqrt_price_x96_to_price(
        sqrt_price_x96,
        context.pool.token0.decimals,
        context.pool.token1.decimals,
    )


def _human(raw: int, decimals: int) -> str:
    return format(
        (Decimal(raw) / (Decimal(10) ** decimals)).normalize(), "f"
    )


def _render_reconcile(result, context: ReadOnlyContext, price: Decimal) -> str:
    snapshot = result.snapshot
    lines = [f"对账区块：{snapshot.block}", "当前链上头寸："]
    if not snapshot.positions:
        lines.append("  无")
    for position in snapshot.positions:
        lines.append(
            f"  tokenId={position.token_id} liquidity={position.liquidity} "
            f"ticks=[{position.tick_lower}, {position.tick_upper}]"
        )
    lines.extend(["两腿余额："])
    for token, raw in (
        (context.pool.token0, snapshot.balance0_raw),
        (context.pool.token1, snapshot.balance1_raw),
    ):
        lines.append(
            f"  {token.symbol} raw={raw} human={_human(raw, token.decimals)}"
        )
    lines.append(f"当前池价：{price}")
    for warning in result.warnings:
        lines.append(f"对账警告：{warning}")
    return "\n".join(lines)


def _atomic_clear(path: Path) -> None:
    """只把 failure 与 failed_at 改为 null 后原子替换。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if type(payload) is not dict:
        raise ValueError("状态文件根节点必须是映射")
    payload["failure"] = None
    payload["failed_at"] = None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".clear-stage-lock-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def main(
    argv: list[str] | None = None, *,
    context_factory: Callable[[str], Any] | None = None,
    state_dir: Path = STATE_DIR,
    input_fn: Callable[[str], str] = input,
    printer: Callable[[str], None] = print,
) -> int:
    """完成只读对账、人工确认与单文件原子清锁。"""
    args = build_parser().parse_args(argv)
    try:
        owner = validate_address(args.owner, "owner")
        context = (context_factory or create_read_only_context)(args.pool_id)
        result = reconcile_on_startup(
            context.reader, owner, spenders=context.spenders
        )
        price = _current_price(context, result.snapshot.block)
        state_path = state_dir / f"machine_state_{context.pool.pool_id}.json"
        state = MachineStateStore(state_path).load()
        printer(_render_reconcile(result, context, price))
        printer(f"持久化状态：{state.state.value}")
        printer(f"failure 原因：{state.failure or '无'}")
        if (
            result.active_position is not None
            and state.state in (MachineState.ENTERING, MachineState.IDLE)
        ):
            printer(
                "！！！警告：状态与链上不一致：链上已有本池流动性头寸，"
                "建议人工核对后再清除"
            )
        printer(f"清除后系统将进入的状态：{state.state.value}")
        if not args.yes:
            printer(f"请输入“{CONFIRMATION}”；其他输入将退出")
            if input_fn("清除确认：") != CONFIRMATION:
                printer("确认字符串不匹配，状态文件未修改")
                return 2
        _atomic_clear(state_path)
        printer("阶段锁停已清除：failure 与 failed_at 已置空")
        return 0
    except Exception as error:
        printer(f"清除失败：{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
