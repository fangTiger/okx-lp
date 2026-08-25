"""带异常值保护的 EIP-1559 gas 估算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Protocol

import yaml


GWEI = Decimal(1_000_000_000)


class GasError(RuntimeError):
    """表示 gas 估值越过安全边界或配置无效。"""


class RpcLike(Protocol):
    """gas 估算所需的最小 RPC 接口。"""

    def call(self, method: str, params: list[Any]) -> Any: ...


@dataclass(frozen=True)
class GasPolicy:
    """gas limit 与 EIP-1559 费率的硬边界。"""

    gas_limit_multiplier: Decimal
    min_gas_limit: int
    max_gas_limit: int
    base_fee_multiplier: Decimal
    min_max_fee_per_gas: int
    max_max_fee_per_gas: int
    min_priority_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True)
class GasQuote:
    """可直接放入 EIP-1559 交易的 gas 字段。"""

    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


def _decimal(data: dict[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(data[key]))
    except (KeyError, InvalidOperation, TypeError):
        raise GasError(f"gas.{key} 配置无效") from None


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise GasError(f"gas.{key} 配置必须是整数")
    return value


def load_gas_policy(path: Path = Path("config/execution.yaml")) -> GasPolicy:
    """从 execution.yaml 加载 gas 硬边界。"""
    try:
        gas = yaml.safe_load(path.read_text(encoding="utf-8"))["gas"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        raise GasError(f"无法读取 gas 配置：{path}") from None
    if type(gas) is not dict:
        raise GasError("gas 配置必须是映射")
    to_wei = lambda key: int((_decimal(gas, key) * GWEI).to_integral_value())
    policy = GasPolicy(
        _decimal(gas, "gas_limit_multiplier"),
        _integer(gas, "min_gas_limit"),
        _integer(gas, "max_gas_limit"),
        _decimal(gas, "base_fee_multiplier"),
        to_wei("min_max_fee_gwei"),
        to_wei("max_max_fee_gwei"),
        to_wei("min_priority_fee_gwei"),
        to_wei("max_priority_fee_gwei"),
    )
    if policy.min_gas_limit <= 0 or policy.min_gas_limit > policy.max_gas_limit:
        raise GasError("gas limit 上下限无效")
    return policy


class GasEstimator:
    """从 pending 区块与节点建议生成受限 EIP-1559 报价。"""

    def __init__(self, rpc: RpcLike, policy: GasPolicy) -> None:
        self.rpc = rpc
        self.policy = policy

    def estimate(self, transaction: dict[str, Any]) -> GasQuote:
        """估算单笔交易；异常高值一律失败关闭。"""
        estimated = int(self.rpc.call("eth_estimateGas", [transaction]), 16)
        buffered = int(
            (Decimal(estimated) * self.policy.gas_limit_multiplier).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        gas_limit = max(buffered, self.policy.min_gas_limit)
        if estimated > self.policy.max_gas_limit or gas_limit > self.policy.max_gas_limit:
            raise GasError(f"gas limit 异常偏高：{estimated}")
        block = self.rpc.call("eth_getBlockByNumber", ["pending", False])
        try:
            base_fee = int(block["baseFeePerGas"], 16)
        except (KeyError, TypeError, ValueError):
            raise GasError("pending 区块缺少有效 baseFeePerGas") from None
        priority = int(self.rpc.call("eth_maxPriorityFeePerGas", []), 16)
        if priority > self.policy.max_priority_fee_per_gas:
            raise GasError(f"优先费异常偏高：{priority} wei")
        priority = max(priority, self.policy.min_priority_fee_per_gas)
        max_fee = int(
            (Decimal(base_fee) * self.policy.base_fee_multiplier).to_integral_value(
                rounding=ROUND_CEILING
            )
        ) + priority
        max_fee = max(max_fee, self.policy.min_max_fee_per_gas)
        if max_fee > self.policy.max_max_fee_per_gas:
            raise GasError(f"maxFeePerGas 异常偏高：{max_fee} wei")
        return GasQuote(gas_limit, max_fee, priority)
