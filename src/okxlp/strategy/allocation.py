"""两腿余额的 50/50 配置与差额计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MIN_SWAP_USD = Decimal("1")


class AllocationConfigError(ValueError):
    """表示再平衡金额配置不可安全使用。"""


@dataclass(frozen=True)
class BalanceSnapshot:
    """collect 后钱包两腿余额与 token1 计价价格。"""

    token0: str
    token1: str
    amount0_raw: int
    amount1_raw: int
    token0_decimals: int
    token1_decimals: int
    price_token1_per_token0: Decimal | str


@dataclass(frozen=True)
class SwapRequirement:
    """恢复 50/50 所需的唯一兑换方向与金额。"""

    token_in: str
    token_out: str
    amount_in: int
    amount_usd: Decimal


def load_min_swap_usd(path: Path = Path("config/risk.yaml")) -> Decimal:
    """读取最小兑换金额；缺省键使用 1 USD。"""
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise AllocationConfigError(f"无法读取再平衡风控配置 {path}：{error}") from error
    if type(root) is not dict or type(root.get("swap", {})) is not dict:
        raise AllocationConfigError("再平衡风控配置的根节点与 swap 必须是映射")
    try:
        return validate_min_swap_usd(
            root.get("swap", {}).get("min_amount_usd", DEFAULT_MIN_SWAP_USD)
        )
    except ValueError as error:
        raise AllocationConfigError(f"最小兑换金额配置非法：{error}") from None


def calculate_50_50_swap(
    snapshot: BalanceSnapshot,
    min_swap_usd: Decimal | str = DEFAULT_MIN_SWAP_USD,
) -> SwapRequirement | None:
    """统一计算两腿差额，小于最小金额时忽略粉尘。"""
    values = (snapshot.amount0_raw, snapshot.amount1_raw)
    decimals = (snapshot.token0_decimals, snapshot.token1_decimals)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("两腿原始余额必须是非负整数")
    if any(type(value) is not int or value < 0 for value in decimals):
        raise ValueError("代币 decimals 必须是非负整数")
    price = _positive_decimal(snapshot.price_token1_per_token0, "池价")
    threshold = validate_min_swap_usd(min_swap_usd)
    amount0 = Decimal(snapshot.amount0_raw) / (Decimal(10) ** snapshot.token0_decimals)
    amount1 = Decimal(snapshot.amount1_raw) / (Decimal(10) ** snapshot.token1_decimals)
    token0_value = amount0 * price
    delta = token0_value - (token0_value + amount1) / Decimal(2)
    amount_usd = abs(delta)
    if amount_usd < threshold:
        return None
    if delta > 0:
        raw = _raw_amount(delta / price, snapshot.token0_decimals)
        result = SwapRequirement(snapshot.token0, snapshot.token1, raw, amount_usd)
    else:
        raw = _raw_amount(-delta, snapshot.token1_decimals)
        result = SwapRequirement(snapshot.token1, snapshot.token0, raw, amount_usd)
    return None if result.amount_in == 0 else result


def validate_min_swap_usd(value: Any) -> Decimal:
    """校验最小兑换金额为有限正数。"""
    result = _positive_decimal(value, "min_amount_usd")
    return result


def _positive_decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} 必须是正数") from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} 必须是正数")
    return result


def _raw_amount(value: Decimal, decimals: int) -> int:
    return int(
        (value * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_FLOOR)
    )
