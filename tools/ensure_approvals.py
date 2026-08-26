"""检查并构造两腿代币授权 Intent；本工具永不签名或广播。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import PoolConfig, load_config
from okxlp.exec.approval import ApprovalManager, ApprovalPlan
from okxlp.uniswap.portfolio import PortfolioReader, PortfolioSnapshot


POOLS_CONFIG_PATH = Path("config/pools.yaml")
EXECUTION_CONFIG_PATH = Path("config/execution.yaml")


class _CapturingReader:
    """保存 ApprovalManager 本次实际使用的唯一账户快照。"""

    def __init__(self, reader: PortfolioReader) -> None:
        self.reader = reader
        self.snapshot: PortfolioSnapshot | None = None

    def read(self, owner: str, *, spenders=()) -> PortfolioSnapshot:
        self.snapshot = self.reader.read(owner, spenders=spenders)
        return self.snapshot


def build_parser() -> argparse.ArgumentParser:
    """构造授权 dry-run 命令行参数。"""
    parser = argparse.ArgumentParser(
        description="读取 allowance 并构造受限 approve Intent"
    )
    parser.add_argument("--owner", required=True, help="需要检查的 EVM 地址")
    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="保留参数；当前批次始终拒绝广播",
    )
    return parser


def _requirements(
    policy: CalldataPolicy,
) -> tuple[tuple[str, str, int], ...]:
    """两腿代币分别检查 NPM 与 SwapRouter02 的配置上限。"""
    return tuple(
        (token, spender, policy.max_approval_raw[token])
        for token in (policy.token0, policy.token1)
        for spender in (policy.npm_address, policy.router_address)
    )


def render_report(
    snapshot: PortfolioSnapshot,
    *,
    requirements: tuple[tuple[str, str, int], ...],
    plans: tuple[ApprovalPlan, ...],
    pool_config: PoolConfig | Any,
    npm_address: str,
    router_address: str,
) -> str:
    """渲染当前额度、充足状态和完整未签名 approve 内容。"""
    token_labels = {
        pool_config.token0.address: pool_config.token0.symbol,
        pool_config.token1.address: pool_config.token1.symbol,
    }
    spender_labels = {
        npm_address: "NPM",
        router_address: "SwapRouter02",
    }
    plans_by_pair = {(plan.token, plan.spender): plan for plan in plans}
    lines = [
        f"区块 {snapshot.block}",
        f"owner {snapshot.owner}",
        "模式 dry_run（只构造，不签名，不广播）",
        "",
        "授权检查:",
    ]
    for token, spender, needed in requirements:
        current = snapshot.allowance_of(token, spender)
        sufficient = current >= needed
        lines.append(
            f"  {token_labels[token]} -> {spender_labels[spender]} "
            f"current={current} needed={needed} "
            f"是否充足={'是' if sufficient else '否'}"
        )
        plan = plans_by_pair.get((token, spender))
        if plan is None:
            continue
        transaction = {
            "intent_id": plan.intent.intent_id,
            "to": plan.intent.target,
            "data": plan.intent.calldata,
            "value": plan.intent.value,
        }
        lines.append("    将要发送的 approve（仅构造）:")
        lines.append(
            "    " + json.dumps(transaction, ensure_ascii=False, sort_keys=True)
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """执行一次同区块 allowance 检查并打印 dry-run 授权计划。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.broadcast:
        parser.error("广播需在生产入口接线完成后启用")

    config = load_config(POOLS_CONFIG_PATH)
    pool = config.find_pool()
    policy = CalldataPolicy.from_config(
        EXECUTION_CONFIG_PATH,
        POOLS_CONFIG_PATH,
        executor_address=args.owner,
        allowed_token_ids=(),
    )
    rpc = JsonRpcClient(config.chain.rpc_urls, chain_id=config.chain.chain_id)
    reader = _CapturingReader(
        PortfolioReader(
            rpc,
            npm_address=policy.npm_address,
            token0=policy.token0,
            token1=policy.token1,
            fee=policy.fee,
        )
    )
    requirements = _requirements(policy)
    plans = ApprovalManager(reader=reader, policy=policy).plan(
        args.owner, requirements
    )
    if reader.snapshot is None:
        raise RuntimeError("授权检查未获得账户快照")
    print(
        render_report(
            reader.snapshot,
            requirements=requirements,
            plans=plans,
            pool_config=pool,
            npm_address=policy.npm_address,
            router_address=policy.router_address,
        )
    )


if __name__ == "__main__":
    main()
