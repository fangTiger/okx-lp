"""QuoterV2 报价与 SwapRouter02 单池兑换 Intent 构造。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

import yaml
from eth_abi import decode, encode
from eth_utils import keccak
from okxlp.config_validation import address as validate_address
from okxlp.exec.intent import Intent


BPS = Decimal("10000")
QUOTE_SIGNATURE = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
QUOTE_TYPE = "(address,address,uint256,uint24,uint160)"
SWAP_SIGNATURE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
SWAP_TYPE = "(address,address,uint24,address,uint256,uint256,uint160)"
class SwapConfigError(ValueError):
    """表示兑换风控配置不可安全使用。"""
@dataclass(frozen=True)
class SwapPolicy:
    """滑点和条件拆单参数。"""

    max_slippage_bps: Decimal = Decimal("30")
    split_threshold_usd: Decimal = Decimal("500")
    split_parts_min: int = 3
    split_parts_max: int = 5
    split_interval_seconds_min: int = 20
    split_interval_seconds_max: int = 30

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.max_slippage_bps < BPS:
            raise SwapConfigError("max_slippage_bps 必须位于 0 到 10000 之间")
        if self.split_threshold_usd <= 0:
            raise SwapConfigError("split_threshold_usd 必须大于零")
        if not 3 <= self.split_parts_min <= self.split_parts_max <= 5:
            raise SwapConfigError("拆单笔数必须位于 3 到 5 之间")
        if not 20 <= self.split_interval_seconds_min <= self.split_interval_seconds_max <= 30:
            raise SwapConfigError("拆单间隔必须位于 20 到 30 秒之间")

    @classmethod
    def from_config(cls, path: Path = Path("config/risk.yaml")) -> "SwapPolicy":
        """从 risk.yaml 加载，缺省项使用锁定的 MVP 默认值。"""
        try:
            root = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise SwapConfigError(f"无法读取兑换风控配置 {path}：{error}") from error
        if type(root) is not dict:
            raise SwapConfigError("兑换风控配置根节点必须是映射")
        limits, split = root.get("limits", {}), root.get("swap", {})
        if type(limits) is not dict or type(split) is not dict:
            raise SwapConfigError("limits 与 swap 必须是映射")
        try:
            return cls(
                max_slippage_bps=_decimal(limits.get("max_slippage_bps", 30)),
                split_threshold_usd=_decimal(split.get("split_threshold_usd", 500)),
                split_parts_min=_integer(split.get("parts_min", 3), "parts_min"),
                split_parts_max=_integer(split.get("parts_max", 5), "parts_max"),
                split_interval_seconds_min=_integer(
                    split.get("interval_seconds_min", 20), "interval_seconds_min"
                ),
                split_interval_seconds_max=_integer(
                    split.get("interval_seconds_max", 30), "interval_seconds_max"
                ),
            )
        except (ValueError, SwapConfigError) as error:
            raise SwapConfigError(f"兑换风控配置非法：{error}") from None
@dataclass(frozen=True)
class SwapQuote:
    """一笔 exact input 的链上报价与价格保护参数。"""

    amount_in: int
    amount_out: int
    amount_out_minimum: int
    sqrt_price_x96_after: int
    initialized_ticks_crossed: int
    gas_estimate: int
    slippage_bps: Decimal
@dataclass(frozen=True)
class ScheduledSwap:
    """带执行前等待时间的独立 swap Intent。"""

    intent: Intent
    quote: SwapQuote
    delay_seconds: int = 0
class SwapRouter:
    """先报价再构造 SwapRouter02 Intent，绝不自行发送。"""

    def __init__(
        self, *, rpc: Any, router_address: str, quoter_address: str,
        policy: SwapPolicy | None = None, random_source: Any | None = None,
    ) -> None:
        self.rpc = rpc
        self.router_address = validate_address(
            router_address, "swap_router02.address"
        )
        self.quoter_address = validate_address(quoter_address, "quoter_v2.address")
        self.policy = policy or SwapPolicy()
        self.random = random_source or random.SystemRandom()

    def quote_exact_input_single(
        self, *, token_in: str, token_out: str, fee: int, amount_in: int,
        slippage_bps: Decimal | None = None, sqrt_price_limit_x96: int = 0,
    ) -> SwapQuote:
        """调用 QuoterV2，并按允许滑点向下计算最小到账量。"""
        selected = self.policy.max_slippage_bps if slippage_bps is None else _decimal(slippage_bps)
        if not Decimal(0) <= selected <= self.policy.max_slippage_bps:
            raise ValueError(f"滑点 {selected} bps 超过配置上限 {self.policy.max_slippage_bps} bps")
        if type(amount_in) is not int or amount_in <= 0:
            raise ValueError("amount_in 必须是正整数")
        params = (token_in, token_out, amount_in, fee, sqrt_price_limit_x96)
        data = _calldata(QUOTE_SIGNATURE, QUOTE_TYPE, params)
        result = self.rpc.eth_call(self.quoter_address, data)
        try:
            amount_out, sqrt_after, ticks, gas = decode(
                ["uint256", "uint160", "uint32", "uint256"],
                bytes.fromhex(result.removeprefix("0x")),
            )
        except Exception as error:
            raise ValueError(f"QuoterV2 返回值无法解码：{error}") from None
        minimum = int(
            (Decimal(amount_out) * (BPS - selected) / BPS).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if minimum <= 0:
            raise ValueError("最低到账数量为零，拒绝无保护的 swap")
        return SwapQuote(amount_in, amount_out, minimum, sqrt_after, ticks, gas, selected)

    def exact_input_single(
        self, *, token_in: str, token_out: str, fee: int, recipient: str,
        amount_in: int, slippage_bps: Decimal | None = None,
        sqrt_price_limit_x96: int = 0, delay_seconds: int = 0,
    ) -> ScheduledSwap:
        """以交易前报价构造一笔 exactInputSingle Intent。"""
        quote = self.quote_exact_input_single(
            token_in=token_in, token_out=token_out, fee=fee, amount_in=amount_in,
            slippage_bps=slippage_bps, sqrt_price_limit_x96=sqrt_price_limit_x96,
        )
        params = (
            token_in, token_out, fee, recipient, amount_in,
            quote.amount_out_minimum, sqrt_price_limit_x96,
        )
        intent = Intent.create(
            self.router_address, _calldata(SWAP_SIGNATURE, SWAP_TYPE, params)
        )
        return ScheduledSwap(intent, quote, delay_seconds)

    def plan_exact_input_single(
        self, *, token_in: str, token_out: str, fee: int, recipient: str,
        amount_in: int, amount_usd: Decimal, slippage_bps: Decimal | None = None,
        sqrt_price_limit_x96: int = 0,
    ) -> tuple[ScheduledSwap, ...]:
        """仅当单笔美元金额达到阈值时拆为 3 到 5 笔。"""
        usd = _decimal(amount_usd)
        if usd < 0:
            raise ValueError("amount_usd 必须是非负数")
        count = 1 if usd < self.policy.split_threshold_usd else self.random.randint(
            self.policy.split_parts_min, self.policy.split_parts_max
        )
        if type(amount_in) is not int or amount_in < count:
            raise ValueError("amount_in 不足以按要求拆单")
        quotient, remainder = divmod(amount_in, count)
        parts = [quotient + (1 if index < remainder else 0) for index in range(count)]
        return tuple(
            self.exact_input_single(
                token_in=token_in, token_out=token_out, fee=fee,
                recipient=recipient, amount_in=part, slippage_bps=slippage_bps,
                sqrt_price_limit_x96=sqrt_price_limit_x96,
                delay_seconds=0 if index == 0 else self.random.randint(
                    self.policy.split_interval_seconds_min,
                    self.policy.split_interval_seconds_max,
                ),
            )
            for index, part in enumerate(parts)
        )
def _calldata(signature: str, abi_type: str, values: Any) -> str:
    try:
        payload = encode([abi_type], [values])
    except Exception as error:
        raise ValueError(f"{signature.split('(')[0]} 参数无法编码：{error}") from None
    return "0x" + (keccak(text=signature)[:4] + payload).hex()
def _decimal(value: Any) -> Decimal:
    if type(value) is bool:
        raise ValueError("数值不得是布尔值")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("数值不是有效 Decimal") from None
    if not result.is_finite():
        raise ValueError("数值必须有限")
    return result
def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} 必须是整数")
    return value
