"""白名单交易 calldata 的 ABI 参数级安全策略。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from eth_abi import decode

from okxlp.config import load_config
from okxlp.config_validation import ConfigError, address as validate_address


MINT = (
    "0x88316456",
    "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)",
    11,
)
DECREASE = ("0x0c49ccbe", "(uint256,uint128,uint256,uint256,uint256)", 5)
COLLECT = ("0xfc6f7865", "(uint256,address,uint128,uint128)", 4)
BURN = ("0x42966c68", "uint256", 1)
SWAP = ("0x04e45aaf", "(address,address,uint24,address,uint256,uint256,uint160)", 7)
APPROVE = ("0x095ea7b3", "(address,uint256)", 2)


class CalldataPolicyError(ValueError):
    """表示 calldata 的目标、ABI 或参数不符合资金安全策略。"""


def _read_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CalldataPolicyError(f"无法读取{label} {path}：{error}") from error
    if type(data) is not dict:
        raise CalldataPolicyError(f"{label}根节点必须是映射")
    return data


def _required(data: dict[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise CalldataPolicyError(f"{path}.{key} 缺少必填字段")
    return data[key]


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise CalldataPolicyError(f"{path} 类型不符：应为映射")
    return value


def _address(value: Any, path: str) -> str:
    try:
        return validate_address(value, path)
    except ConfigError as error:
        raise CalldataPolicyError(str(error)) from None


@dataclass(frozen=True)
class CalldataPolicy:
    """把允许的方法进一步收窄到唯一池、收款人和头寸集合。"""

    executor_address: str
    npm_address: str
    router_address: str
    token0: str
    token1: str
    fee: int
    allowed_token_ids: frozenset[int]
    max_approval_raw: Mapping[str, int]
    max_deadline_seconds: int = 3600

    def __post_init__(self) -> None:
        for field in (
            "executor_address", "npm_address", "router_address", "token0", "token1"
        ):
            object.__setattr__(self, field, _address(getattr(self, field), field))
        if type(self.fee) is not int or self.fee <= 0:
            raise CalldataPolicyError(f"fee 必须是正整数，实际值={self.fee}")
        if type(self.max_deadline_seconds) is not int or self.max_deadline_seconds <= 0:
            raise CalldataPolicyError(
                "max_deadline_seconds 必须是正整数，"
                f"实际值={self.max_deadline_seconds}"
            )
        try:
            token_ids = frozenset(self.allowed_token_ids)
        except TypeError:
            raise CalldataPolicyError("allowed_token_ids 必须是可迭代的整数集合") from None
        for token_id in token_ids:
            if type(token_id) is not int or token_id < 0:
                raise CalldataPolicyError(
                    f"allowed_token_ids 只能包含非负整数，实际值={token_id}"
                )
        object.__setattr__(self, "allowed_token_ids", token_ids)
        if not isinstance(self.max_approval_raw, Mapping):
            raise CalldataPolicyError("max_approval_raw 必须是地址到正整数的映射")
        limits: dict[str, int] = {}
        for raw_token, amount in self.max_approval_raw.items():
            token = _address(raw_token, "max_approval_raw 的键")
            if token in limits:
                raise CalldataPolicyError(
                    f"max_approval_raw 包含重复代币地址：{token}"
                )
            if type(amount) is not int or amount <= 0:
                raise CalldataPolicyError(
                    "max_approval_raw 的值必须是正整数："
                    f"token={token}，实际值={amount}"
                )
            limits[token] = amount
        expected_tokens = frozenset((self.token0, self.token1))
        if len(expected_tokens) != 2:
            raise CalldataPolicyError("token0 与 token1 必须是两个不同地址")
        if frozenset(limits) != expected_tokens:
            missing = sorted(expected_tokens - frozenset(limits))
            extra = sorted(frozenset(limits) - expected_tokens)
            raise CalldataPolicyError(
                "max_approval_raw 必须恰好包含 token0 与 token1："
                f"缺少={missing}，多余={extra}"
            )
        object.__setattr__(
            self, "max_approval_raw", MappingProxyType(dict(limits))
        )

    def with_token_ids(
        self, token_ids: frozenset[int]
    ) -> "CalldataPolicy":
        """派生出只有 allowed_token_ids 不同的新策略，其余字段一律不变。"""
        try:
            normalized = frozenset(token_ids)
        except TypeError:
            raise CalldataPolicyError("token_ids 必须是可迭代的整数集合") from None
        if len(normalized) > 50:
            raise CalldataPolicyError("token_ids 数量不得超过 50")
        for token_id in normalized:
            if type(token_id) is not int or token_id < 0:
                raise CalldataPolicyError(
                    f"token_ids 只能包含非负整数，实际值={token_id}"
                )
        return replace(self, allowed_token_ids=normalized)

    @classmethod
    def from_config(
        cls, execution_path: Path, pools_path: Path, *, executor_address: str,
        allowed_token_ids: Any, pool_id: str | None = None,
    ) -> "CalldataPolicy":
        """从执行配置和匹配的池配置严格构造参数策略。"""
        execution = _read_mapping(execution_path, "执行配置")
        addresses = _mapping(
            _required(execution, "addresses", "执行配置"), "execution.addresses"
        )
        npm_address = _required(addresses, "npm", "execution.addresses")
        router_address = _required(
            addresses, "swap_router02", "execution.addresses"
        )
        approval = _mapping(
            _required(execution, "approval", "执行配置"), "execution.approval"
        )
        raw_limits = _mapping(
            _required(approval, "max_amount_raw", "execution.approval"),
            "execution.approval.max_amount_raw",
        )
        approval_limits = {}
        for raw_token, amount in raw_limits.items():
            token = _address(raw_token, "execution.approval.max_amount_raw 的键")
            if token in approval_limits:
                raise CalldataPolicyError(
                    f"execution.approval.max_amount_raw 包含重复代币：{token}"
                )
            approval_limits[token] = (
                int(amount)
                if type(amount) is str and re.fullmatch(r"[0-9]+", amount)
                else amount
            )

        try:
            pool = load_config(pools_path).find_pool(pool_id)
        except ConfigError as error:
            raise CalldataPolicyError(str(error)) from None
        selected_tokens = (pool.token0.address, pool.token1.address)
        try:
            selected_limits = {
                token: approval_limits[token] for token in selected_tokens
            }
        except KeyError as error:
            raise CalldataPolicyError(
                f"execution.approval.max_amount_raw 缺少所选池代币：{error.args[0]}"
            ) from None
        try:
            fee_units = Decimal(str(pool.fee_bps)) * Decimal(100)
        except InvalidOperation:
            raise CalldataPolicyError(
                f"池 {pool.pool_id} 的 fee_bps 不是有效数值：{pool.fee_bps}"
            ) from None
        if fee_units != fee_units.to_integral_value():
            raise CalldataPolicyError(
                f"池 {pool.pool_id} 的 fee_bps 无法精确换算为 fee：{pool.fee_bps}"
            )
        return cls(
            executor_address=executor_address,
            npm_address=npm_address,
            router_address=router_address,
            token0=pool.token0.address,
            token1=pool.token1.address,
            fee=int(fee_units),
            allowed_token_ids=frozenset(allowed_token_ids),
            max_approval_raw=selected_limits,
        )

    def validate(
        self, *, target: str, calldata: str, value: int, now_ts: int
    ) -> None:
        """完整解码并校验一笔白名单调用的全部安全关键参数。"""
        if type(value) is not int or value != 0:
            raise CalldataPolicyError(
                f"value 不合规：期望值=0，实际值={value}"
            )
        if type(now_ts) is not int:
            raise CalldataPolicyError(
                f"now_ts 不合规：期望整数时间戳，实际值={now_ts}"
            )
        normalized_target = _address(
            target.lower() if type(target) is str else target, "target"
        )
        if type(calldata) is not str or not calldata.startswith("0x"):
            raise CalldataPolicyError(f"calldata 格式非法，实际值={calldata}")
        encoded = calldata[2:]
        if re.fullmatch(r"[0-9a-fA-F]*", encoded) is None:
            raise CalldataPolicyError(
                f"calldata 不是连续有效十六进制，实际值={calldata}"
            )
        try:
            raw = bytes.fromhex(encoded)
        except ValueError:
            raise CalldataPolicyError(f"calldata 不是有效十六进制，实际值={calldata}") from None
        if len(raw) < 4:
            raise CalldataPolicyError(
                f"calldata 长度不足：期望至少 4 字节，实际值={len(raw)} 字节"
            )
        selector = "0x" + raw[:4].hex()
        dispatch = self._dispatch(normalized_target, selector)
        abi_type, slots, validator = dispatch
        payload = raw[4:]
        expected_size = slots * 32
        if len(payload) != expected_size:
            kind = "包含多余尾随字节" if len(payload) > expected_size else "长度不足"
            raise CalldataPolicyError(
                f"calldata {kind}：期望参数长度={expected_size} 字节，"
                f"实际值={len(payload)} 字节"
            )
        try:
            decoded = decode([abi_type], payload, strict=True)[0]
        except Exception as error:
            raise CalldataPolicyError(f"calldata 无法按 ABI 完整解码：{error}") from None
        validator(decoded, now_ts)

    def _dispatch(self, target: str, selector: str) -> tuple[str, int, Any]:
        approve_route = (
            APPROVE[1], APPROVE[2],
            lambda values, now_ts: self._validate_approve(
                target, values, now_ts
            ),
        )
        routes = {
            (self.npm_address, MINT[0]): (MINT[1], MINT[2], self._validate_mint),
            (self.npm_address, DECREASE[0]): (
                DECREASE[1], DECREASE[2], self._validate_decrease
            ),
            (self.npm_address, COLLECT[0]): (
                COLLECT[1], COLLECT[2], self._validate_collect
            ),
            (self.npm_address, BURN[0]): (BURN[1], BURN[2], self._validate_burn),
            (self.router_address, SWAP[0]): (SWAP[1], SWAP[2], self._validate_swap),
            (self.token0, APPROVE[0]): approve_route,
            (self.token1, APPROVE[0]): approve_route,
        }
        try:
            return routes[(target, selector)]
        except KeyError:
            raise CalldataPolicyError(
                "目标地址与方法选择器组合不在参数策略中："
                f"target={target}，selector={selector}"
            ) from None

    @staticmethod
    def _equal(name: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            raise CalldataPolicyError(
                f"{name} 不合规：期望值={expected}，实际值={actual}"
            )

    def _deadline(self, deadline: Any, now_ts: int) -> None:
        maximum = now_ts + self.max_deadline_seconds
        if type(deadline) is not int or not 0 < deadline <= maximum:
            raise CalldataPolicyError(
                f"deadline 不合规：期望范围=1..{maximum}，实际值={deadline}"
            )

    def _token_id(self, token_id: Any) -> None:
        if token_id not in self.allowed_token_ids:
            raise CalldataPolicyError(
                "tokenId 不合规："
                f"期望属于={sorted(self.allowed_token_ids)}，实际值={token_id}"
            )

    def _validate_mint(self, values: tuple[Any, ...], now_ts: int) -> None:
        (
            token0, token1, fee, tick_lower, tick_upper,
            amount0_desired, amount1_desired, amount0_min, amount1_min,
            recipient, deadline,
        ) = values
        self._equal("token0", token0.lower(), self.token0)
        self._equal("token1", token1.lower(), self.token1)
        self._equal("fee", fee, self.fee)
        self._equal("recipient", recipient.lower(), self.executor_address)
        if (
            type(tick_lower) is not int
            or type(tick_upper) is not int
            or tick_lower >= tick_upper
        ):
            raise CalldataPolicyError(
                "tick 范围不合规：期望 tickLower < tickUpper，"
                f"实际值=({tick_lower}, {tick_upper})"
            )
        self._deadline(deadline, now_ts)
        for name, amount in (
            ("amount0Desired", amount0_desired),
            ("amount1Desired", amount1_desired),
            ("amount0Min", amount0_min),
            ("amount1Min", amount1_min),
        ):
            if type(amount) is not int or amount < 0:
                raise CalldataPolicyError(
                    f"{name} 不合规：期望非负整数，实际值={amount}"
                )
        if amount0_desired + amount1_desired <= 0:
            raise CalldataPolicyError(
                "amount0Desired 与 amount1Desired 不得同时为 0"
            )
        # mint minimum 是存入比例约束，窄区间价格小幅变动就会使
        # 两腿比例剧烈摆动；因此允许双零，价值保护由 mint 模拟结果完成。

    def _validate_decrease(self, values: tuple[Any, ...], now_ts: int) -> None:
        token_id, liquidity, amount0_min, amount1_min, deadline = values
        self._token_id(token_id)
        if type(liquidity) is not int or liquidity <= 0:
            raise CalldataPolicyError(
                f"liquidity 不合规：期望正整数，实际值={liquidity}"
            )
        if any(
            type(amount) is not int or amount < 0
            for amount in (amount0_min, amount1_min)
        ):
            raise CalldataPolicyError(
                "amount0Min 与 amount1Min 必须是非负整数"
            )
        if amount0_min == 0 and amount1_min == 0:
            raise CalldataPolicyError(
                "amount0Min 与 amount1Min 不得同时为 0"
            )
        self._deadline(deadline, now_ts)

    def _validate_collect(self, values: tuple[Any, ...], _now_ts: int) -> None:
        token_id, recipient, _amount0_max, _amount1_max = values
        self._token_id(token_id)
        self._equal("recipient", recipient.lower(), self.executor_address)

    def _validate_burn(self, token_id: int, _now_ts: int) -> None:
        self._token_id(token_id)

    def _validate_swap(self, values: tuple[Any, ...], _now_ts: int) -> None:
        (
            token_in, token_out, fee, recipient, amount_in,
            amount_out_minimum, _sqrt_price_limit_x96,
        ) = values
        token_in, token_out = token_in.lower(), token_out.lower()
        allowed = frozenset((self.token0, self.token1))
        if token_in not in allowed:
            raise CalldataPolicyError(
                f"tokenIn 不合规：期望属于={sorted(allowed)}，实际值={token_in}"
            )
        if token_out not in allowed:
            raise CalldataPolicyError(
                f"tokenOut 不合规：期望属于={sorted(allowed)}，实际值={token_out}"
            )
        if token_in == token_out:
            raise CalldataPolicyError(
                f"tokenIn 与 tokenOut 不合规：期望两者不同，实际值={token_in}"
            )
        self._equal("fee", fee, self.fee)
        self._equal("recipient", recipient.lower(), self.executor_address)
        if type(amount_in) is not int or amount_in <= 0:
            raise CalldataPolicyError(
                f"amountIn 不合规：期望正整数，实际值={amount_in}"
            )
        if type(amount_out_minimum) is not int or amount_out_minimum <= 0:
            raise CalldataPolicyError(
                "amountOutMinimum 不合规："
                f"期望正整数，实际值={amount_out_minimum}"
            )

    def _validate_approve(
        self, target: str, values: tuple[Any, ...], _now_ts: int
    ) -> None:
        spender, amount = values
        spender = spender.lower()
        allowed_spenders = frozenset((self.npm_address, self.router_address))
        if spender not in allowed_spenders:
            raise CalldataPolicyError(
                "spender 不合规："
                f"期望属于={sorted(allowed_spenders)}，实际值={spender}"
            )
        maximum = self.max_approval_raw[target]
        if type(amount) is not int or not 0 <= amount <= maximum:
            raise CalldataPolicyError(
                f"amount 不合规：期望范围=0..{maximum}，实际值={amount}"
            )
