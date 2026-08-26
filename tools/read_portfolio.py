"""读取指定地址的 NPM 头寸、两腿余额与 ERC20 授权额度。"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from okxlp.chain.rpc import JsonRpcClient
from okxlp.config import PoolConfig, load_config
from okxlp.config_validation import address as validate_address
from okxlp.config_validation import mapping, required
from okxlp.uniswap.pool import SELECTORS as POOL_SELECTORS
from okxlp.uniswap.pool import _word, decode_int
from okxlp.uniswap.portfolio import PortfolioReader, PortfolioSnapshot


POOLS_CONFIG_PATH = Path("config/pools.yaml")
EXECUTION_CONFIG_PATH = Path("config/execution.yaml")


def build_parser() -> argparse.ArgumentParser:
    """构造仅接收 owner 的只读命令行参数。"""
    parser = argparse.ArgumentParser(description="读取账户链上 LP 头寸与授权")
    parser.add_argument("--owner", required=True, help="需要读取的 EVM 地址")
    parser.add_argument(
        "--pool-id", help="目标池配置 ID；缺省使用首个池"
    )
    return parser


def _load_execution_addresses(path: Path) -> tuple[str, str]:
    """读取并校验 NPM 与 SwapRouter02 地址。"""
    try:
        root = mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "根配置")
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"无法读取执行配置 {path}：{error}") from error
    addresses = mapping(required(root, "addresses", "根配置"), "addresses")
    npm_address = validate_address(
        required(addresses, "npm", "addresses"), "addresses.npm"
    )
    router_address = validate_address(
        required(addresses, "swap_router02", "addresses"),
        "addresses.swap_router02",
    )
    return npm_address, router_address


def _human_amount(raw: int, decimals: int) -> str:
    value = Decimal(raw) / (Decimal(10) ** decimals)
    return format(value.normalize(), "f")


def render_snapshot(
    snapshot: PortfolioSnapshot,
    *,
    pool_config: PoolConfig | Any,
    current_tick: int,
    npm_address: str,
    router_address: str,
) -> str:
    """把账户快照渲染为便于人工核对的中文文本。"""
    total_position_count = (
        len(snapshot.positions) + snapshot.other_pool_position_count
    )
    lines = [
        f"区块        {snapshot.block}",
        f"owner       {snapshot.owner}",
        f"当前 tick   {current_tick}",
        f"NPM balanceOf {total_position_count}",
        f"other_pool_position_count {snapshot.other_pool_position_count}",
        "",
        "本池头寸:",
    ]
    if not snapshot.positions:
        lines.append("  无")
    for position in snapshot.positions:
        in_range = position.tick_lower <= current_tick < position.tick_upper
        lines.extend(
            [
                f"  tokenId      {position.token_id}",
                f"  token0       {position.token0}",
                f"  token1       {position.token1}",
                f"  fee          {position.fee}",
                f"  tickLower    {position.tick_lower}",
                f"  tickUpper    {position.tick_upper}",
                f"  liquidity    {position.liquidity}",
                f"  in-range     {'是' if in_range else '否'}",
            ]
        )

    lines.extend(["", "两腿余额:"])
    for token, raw in (
        (pool_config.token0, snapshot.balance0_raw),
        (pool_config.token1, snapshot.balance1_raw),
    ):
        lines.append(
            f"  {token.symbol} raw={raw} human={_human_amount(raw, token.decimals)}"
        )

    lines.extend(["", "授权额度:"])
    for token in (pool_config.token0, pool_config.token1):
        for label, spender in (("NPM", npm_address), ("SwapRouter02", router_address)):
            lines.append(
                f"  {token.symbol} -> {label} "
                f"raw={snapshot.allowance_of(token.address, spender)}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """执行一次固定区块的只读账户检查并打印结果。"""
    args = build_parser().parse_args(argv)
    config = load_config(POOLS_CONFIG_PATH)
    pool = config.find_pool(args.pool_id)
    npm_address, router_address = _load_execution_addresses(EXECUTION_CONFIG_PATH)
    rpc = JsonRpcClient(config.chain.rpc_urls, chain_id=config.chain.chain_id)
    fee = int(pool.fee_bps * 100)
    reader = PortfolioReader(
        rpc,
        npm_address=npm_address,
        token0=pool.token0.address,
        token1=pool.token1.address,
        fee=fee,
    )
    snapshot = reader.read(args.owner, spenders=(npm_address, router_address))
    slot0 = rpc.eth_call(pool.address, POOL_SELECTORS["slot0"], hex(snapshot.block))
    current_tick = decode_int(_word(slot0, 1), signed=True, bits=256)
    print(
        render_snapshot(
            snapshot,
            pool_config=pool,
            current_tick=current_tick,
            npm_address=npm_address,
            router_address=router_address,
        )
    )


if __name__ == "__main__":
    main()
