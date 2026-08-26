"""Uniswap V3 NPM 头寸操作的纯 Intent 构造器。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from eth_abi import encode
from eth_utils import keccak

from okxlp.config_validation import address as validate_address
from okxlp.exec.intent import Intent
from okxlp.uniswap.tickmath import aligned_tick_range


MINT = (
    "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))",
    "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)",
)
DECREASE = (
    "decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))",
    "(uint256,uint128,uint256,uint256,uint256)",
)
COLLECT = (
    "collect((uint256,address,uint128,uint128))",
    "(uint256,address,uint128,uint128)",
)
BURN = ("burn(uint256)", "uint256")
MAX_UINT128 = 2**128 - 1


def _calldata(method: tuple[str, str], values: Any) -> str:
    """按官方函数签名编码 calldata，并把编码错误转为中文。"""
    signature, abi_type = method
    try:
        payload = encode([abi_type], [values])
    except Exception as error:
        raise ValueError(f"{signature.split('(')[0]} 参数无法编码：{error}") from None
    return "0x" + (keccak(text=signature)[:4] + payload).hex()


class PositionManager:
    """构造 NPM 头寸操作 Intent，不读取私钥也不发送交易。"""

    def __init__(self, address: str) -> None:
        self.address = validate_address(address, "npm.address")

    def mint(
        self, *, token0: str, token1: str, fee: int,
        current_tick: int | None = None, width: Decimal | None = None,
        tick_spacing: int | None = None, tick_lower: int | None = None,
        tick_upper: int | None = None, amount0_desired: int,
        amount1_desired: int, amount0_min: int, amount1_min: int,
        recipient: str, deadline: int, value: int = 0,
        intent_id: str | None = None,
    ) -> Intent:
        """使用预计算区间，或兼容旧调用按 M1 向外对齐区间。"""
        explicit_range = tick_lower is not None or tick_upper is not None
        calculated_range = any(
            value is not None for value in (current_tick, width, tick_spacing)
        )
        if explicit_range:
            if calculated_range or type(tick_lower) is not int or type(tick_upper) is not int:
                raise ValueError("显式 tick 区间必须成对提供，且不得同时要求重新计算")
            if tick_lower >= tick_upper:
                raise ValueError("显式 tick 区间必须满足 tick_lower < tick_upper")
        else:
            if current_tick is None or width is None or tick_spacing is None:
                raise ValueError("必须提供显式 tick 区间或完整的区间计算参数")
            tick_lower, tick_upper = aligned_tick_range(
                current_tick, width, tick_spacing
            )
        values = (
            token0, token1, fee, tick_lower, tick_upper,
            amount0_desired, amount1_desired, amount0_min, amount1_min,
            recipient, deadline,
        )
        return Intent.create(
            self.address, _calldata(MINT, values), value=value,
            intent_id=intent_id,
        )

    def decrease_liquidity(
        self, *, token_id: int, liquidity: int, amount0_min: int,
        amount1_min: int, deadline: int, value: int = 0,
        intent_id: str | None = None,
    ) -> Intent:
        """构造再平衡 burn 阶段使用的 decreaseLiquidity Intent。"""
        values = (token_id, liquidity, amount0_min, amount1_min, deadline)
        return Intent.create(
            self.address, _calldata(DECREASE, values), value=value,
            intent_id=intent_id,
        )

    def collect(
        self, *, token_id: int, recipient: str,
        amount0_max: int = MAX_UINT128, amount1_max: int = MAX_UINT128,
        value: int = 0, intent_id: str | None = None,
    ) -> Intent:
        """构造 collect Intent，默认领取两腿全部应计金额。"""
        values = (token_id, recipient, amount0_max, amount1_max)
        return Intent.create(
            self.address, _calldata(COLLECT, values), value=value,
            intent_id=intent_id,
        )

    def burn(
        self, token_id: int, *, value: int = 0,
        intent_id: str | None = None,
    ) -> Intent:
        """构造销毁已清空头寸 NFT 的 burn Intent。"""
        return Intent.create(
            self.address, _calldata(BURN, token_id), value=value,
            intent_id=intent_id,
        )
