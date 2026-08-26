"""启动对账后完整预览 enter 或 exit 的未签署交易。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.campaign.gate import load_fact_gate
from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import load_config
from okxlp.config_validation import address as validate_address
from okxlp.config_validation import mapping, required
from okxlp.exec.approval import ApprovalManager
from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.executor import ExecutionResult
from okxlp.exec.intent import IntentStatus
from okxlp.exec.reconcile import ReconcileResult, reconcile_on_startup
from okxlp.strategy.actions import ActionError, ProductionActions
from okxlp.strategy.machine_types import MarketSample, build_price_band
from okxlp.uniswap.pool import SELECTORS as POOL_SELECTORS
from okxlp.uniswap.pool import _word, decode_int
from okxlp.uniswap.portfolio import PortfolioReader
from okxlp.uniswap.position import PositionManager
from okxlp.uniswap.swap import SwapPolicy, SwapRouter
from okxlp.uniswap.tickmath import TICK_BASE, sqrt_price_x96_to_price


POOLS_CONFIG_PATH = Path("config/pools.yaml")
EXECUTION_CONFIG_PATH = Path("config/execution.yaml")
GAS_BY_SELECTOR = {
    "0x095ea7b3": 70_000,
    "0x04e45aaf": 250_000,
    "0x88316456": 600_000,
    "0x0c49ccbe": 400_000,
    "0xfc6f7865": 250_000,
    "0x42966c68": 100_000,
}


class PreviewExecutor:
    """记录完整交易字段，不模拟、不持久化，也不发送。"""

    def __init__(self, *, owner: str, chain_id: int, printer=print) -> None:
        self.owner = validate_address(owner, "owner")
        self.chain_id = chain_id
        self.printer = printer
        self.transaction_count = 0
        self.total_gas = 0

    def execute(
        self, intent, *, allow_broadcast: bool = False,
        simulation_check=None,
    ):
        """仅把 Intent 渲染为 dry-run 交易。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        if broadcast:
            raise PermissionError("拒绝广播：生产入口在批次 8")
        selector = intent.calldata[:10]
        gas = GAS_BY_SELECTOR.get(selector, 250_000)
        transaction = {
            "chainId": self.chain_id,
            "from": self.owner,
            "to": intent.target,
            "data": intent.calldata,
            "value": intent.value,
            "gas": gas,
        }
        self.transaction_count += 1
        self.total_gas += gas
        envelope = {"intent_id": intent.intent_id, **transaction}
        self.printer(f"交易 {self.transaction_count}（gas 预估 {gas}）")
        self.printer(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
        completed = replace(intent, status=IntentStatus.DRY_RUN)
        return ExecutionResult(completed, transaction)


def build_parser() -> argparse.ArgumentParser:
    """构造全量动作预览参数。"""
    parser = argparse.ArgumentParser(
        description="对账并预览生产动作（只读 + dry-run）"
    )
    parser.add_argument("--owner", required=True, help="需要对账的 EVM 地址")
    parser.add_argument(
        "--action", required=True, choices=("enter", "exit"),
        help="需要预览的动作",
    )
    parser.add_argument(
        "--broadcast", action="store_true",
        help="保留参数；本批次一律拒绝",
    )
    return parser


def _execution_addresses(path: Path) -> tuple[str, str, str]:
    """读取预览所需的 NPM、Router 与 Quoter 地址。"""
    try:
        root = mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "根配置")
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"无法读取执行配置 {path}：{error}") from error
    addresses = mapping(required(root, "addresses", "根配置"), "addresses")
    names = ("npm", "swap_router02", "quoter_v2")
    return tuple(
        validate_address(
            required(addresses, name, "addresses"), f"addresses.{name}"
        )
        for name in names
    )


def _sample_at_block(rpc, pool, block: int) -> MarketSample:
    """在对账区块读取 slot0，避免混用不同区块的价格。"""
    slot0 = rpc.eth_call(pool.address, POOL_SELECTORS["slot0"], hex(block))
    sqrt_price_x96 = decode_int(_word(slot0, 0))
    tick = decode_int(_word(slot0, 1), signed=True, bits=256)
    price = sqrt_price_x96_to_price(
        sqrt_price_x96, pool.token0.decimals, pool.token1.decimals
    )
    return MarketSample(price, tick, sqrt_price_x96)


def _position_amounts(position, current_tick: int) -> tuple[int, int]:
    """估算 decreaseLiquidity 后可 collect 的两腿本金原始数量。"""
    with localcontext() as context:
        context.prec = 80
        lower = TICK_BASE ** (Decimal(position.tick_lower) / Decimal(2))
        upper = TICK_BASE ** (Decimal(position.tick_upper) / Decimal(2))
        current = TICK_BASE ** (Decimal(current_tick) / Decimal(2))
        liquidity = Decimal(position.liquidity)
        if current_tick <= position.tick_lower:
            amount0 = liquidity * (upper - lower) / (lower * upper)
            amount1 = Decimal(0)
        elif current_tick < position.tick_upper:
            amount0 = liquidity * (upper - current) / (current * upper)
            amount1 = liquidity * (current - lower)
        else:
            amount0 = Decimal(0)
            amount1 = liquidity * (upper - lower)
        return (
            int(amount0.to_integral_value(rounding=ROUND_FLOOR)),
            int(amount1.to_integral_value(rounding=ROUND_FLOOR)),
        )


class _ExitPreviewReader:
    """第二次读取时加入 LP 本金，仅用于生成 collect 后的 swap 预览。"""

    def __init__(self, reader, position, current_tick: int) -> None:
        self.reader = reader
        self.position = position
        self.current_tick = current_tick
        self.calls = 0

    def read(self, owner, *, spenders=()):
        snapshot = self.reader.read(owner, spenders=spenders)
        self.calls += 1
        if self.calls == 1:
            return snapshot
        amount0, amount1 = _position_amounts(
            self.position, self.current_tick
        )
        return replace(
            snapshot,
            balance0_raw=snapshot.balance0_raw + amount0,
            balance1_raw=snapshot.balance1_raw + amount1,
        )


def _render_reconcile(result: ReconcileResult) -> str:
    """渲染启动对账结论及全部 warning。"""
    active = result.active_position
    lines = [
        "启动对账完成（链上为准）",
        f"区块: {result.snapshot.block}",
        f"owner: {result.snapshot.owner}",
        f"allowed_token_ids: {sorted(result.token_ids)}",
        "active_position: " + (
            "None" if active is None else (
                f"tokenId={active.token_id}, liquidity={active.liquidity}, "
                f"ticks=[{active.tick_lower}, {active.tick_upper}]"
            )
        ),
        "warnings:",
    ]
    lines.extend(
        (f"  - {warning}" for warning in result.warnings),
    )
    if not result.warnings:
        lines.append("  无")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """对账后按真实 ABI 构造完整动作序列，全程保持 dry-run。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.broadcast:
        parser.error("拒绝 --broadcast：生产入口在批次 8")

    config = load_config(POOLS_CONFIG_PATH)
    pool = config.find_pool()
    npm_address, router_address, quoter_address = _execution_addresses(
        EXECUTION_CONFIG_PATH
    )
    rpc = JsonRpcClient(
        config.chain.rpc_urls, chain_id=config.chain.chain_id
    )
    fee = int(pool.fee_bps * 100)
    reader = PortfolioReader(
        rpc,
        npm_address=npm_address,
        token0=pool.token0.address,
        token1=pool.token1.address,
        fee=fee,
    )
    result = reconcile_on_startup(
        reader,
        args.owner,
        spenders=(npm_address, router_address),
    )
    print(_render_reconcile(result), flush=True)

    # allowed_token_ids 只能取本次链上对账结果，禁止从配置或本地状态补入。
    policy = CalldataPolicy.from_config(
        EXECUTION_CONFIG_PATH,
        POOLS_CONFIG_PATH,
        executor_address=args.owner,
        allowed_token_ids=result.token_ids,
    )
    sample = _sample_at_block(rpc, pool, result.snapshot.block)
    action_reader: Any = reader
    if args.action == "exit" and result.active_position is not None:
        action_reader = _ExitPreviewReader(
            reader, result.active_position, sample.tick
        )
        print(
            "提示: exit swap 数量含按当前 tick 与流动性估算的 LP 本金；"
            "生产执行会在 collect 后重新读取实际余额。",
            flush=True,
        )

    swap_policy = SwapPolicy.from_config()
    executor = PreviewExecutor(
        owner=args.owner, chain_id=config.chain.chain_id
    )
    actions = ProductionActions(
        executor=executor,
        reader=action_reader,
        approval_manager=ApprovalManager(
            reader=action_reader, policy=policy
        ),
        position_manager=PositionManager(policy.npm_address),
        swap_router=SwapRouter(
            rpc=rpc,
            router_address=router_address,
            quoter_address=quoter_address,
            policy=swap_policy,
        ),
        owner=args.owner,
        pool=pool,
        fact_gate=load_fact_gate(),
        swap_policy=swap_policy,
        pool_snapshot_reader=lambda: _sample_at_block(
            rpc, pool, rpc.block_number()
        ),
    )
    band = build_price_band(
        sample.price,
        pool.tick_spacing,
        pool.token0.decimals,
        pool.token1.decimals,
    )
    try:
        if args.action == "enter":
            print(
                "提示: dry-run mint 数量为 swap 报价估算；"
                "生产广播会在 swap 后重新读取真实余额。",
                flush=True,
            )
            actions.enter(sample, band, allow_broadcast=False)
        else:
            actions.exit(sample, allow_broadcast=False)
    except ActionError as error:
        parser.exit(1, f"ActionError: {error}\n")
    print(
        f"总笔数: {executor.transaction_count}；"
        f"预估 gas 总量: {executor.total_gas}"
    )
    print("模式: dry_run；未签署；广播交易数: 0")


if __name__ == "__main__":
    main()
