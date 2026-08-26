"""依据同区块 allowance 构造受限 ERC20 授权 Intent。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from eth_abi import encode

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.config_validation import ConfigError, address as validate_address
from okxlp.exec.intent import Intent


APPROVE_SELECTOR = "0x095ea7b3"


class ApprovalError(RuntimeError):
    """表示授权需求不符合显式安全上限或目标约束。"""


@dataclass(frozen=True)
class ApprovalPlan:
    """一次授权补足计划。"""

    token: str
    spender: str
    current: int
    target: int
    intent: Intent


class ApprovalManager:
    """读取一次账户快照，并为不足的 allowance 生成受限 Intent。"""

    def __init__(self, *, reader, policy: CalldataPolicy) -> None:
        if not isinstance(policy, CalldataPolicy):
            raise ApprovalError("policy 必须是 CalldataPolicy")
        self.reader = reader
        self.policy = policy

    def plan(
        self,
        owner: str,
        requirements: Sequence[tuple[str, str, int]],
        *,
        intent_ids: Sequence[str] | None = None,
    ) -> tuple[ApprovalPlan, ...]:
        """对全部需求读取同一快照，缺额时一次授权到配置上限。"""
        try:
            normalized_owner = validate_address(owner, "owner")
        except ConfigError as error:
            raise ApprovalError(str(error)) from None
        if isinstance(requirements, (str, bytes)):
            raise ApprovalError("requirements 必须是授权三元组序列")
        try:
            raw_requirements = tuple(requirements)
        except TypeError:
            raise ApprovalError("requirements 必须是授权三元组序列") from None
        selected_ids = None if intent_ids is None else tuple(intent_ids)
        if selected_ids is not None and len(selected_ids) != len(raw_requirements):
            raise ApprovalError("intent_ids 数量必须与 requirements 数量一致")

        normalized = []
        allowed_spenders = frozenset(
            (self.policy.npm_address, self.policy.router_address)
        )
        for index, requirement in enumerate(raw_requirements):
            if type(requirement) not in (tuple, list) or len(requirement) != 3:
                raise ApprovalError(
                    f"requirements[{index}] 必须是 (token, spender, needed)"
                )
            raw_token, raw_spender, needed = requirement
            try:
                token = validate_address(raw_token, f"requirements[{index}].token")
                spender = validate_address(
                    raw_spender, f"requirements[{index}].spender"
                )
            except ConfigError as error:
                raise ApprovalError(str(error)) from None
            if token not in self.policy.max_approval_raw:
                raise ApprovalError(f"token 不是本池两腿代币：{token}")
            if spender not in allowed_spenders:
                raise ApprovalError(
                    "spender 只允许 NPM 或 SwapRouter02："
                    f"实际值={spender}"
                )
            if type(needed) is not int or needed < 0:
                raise ApprovalError(
                    f"needed 必须是非负整数：实际值={needed}"
                )
            maximum = self.policy.max_approval_raw[token]
            if needed > maximum:
                raise ApprovalError(
                    "授权需求超过配置上限，必须人工提高上限："
                    f"token={token}，needed={needed}，上限={maximum}"
                )
            normalized.append((token, spender, needed))

        spenders = tuple(dict.fromkeys(item[1] for item in normalized))
        snapshot = self.reader.read(normalized_owner, spenders=spenders)
        plans = []
        for index, (token, spender, needed) in enumerate(normalized):
            current = snapshot.allowance_of(token, spender)
            if current >= needed:
                continue
            target = self.policy.max_approval_raw[token]
            calldata = APPROVE_SELECTOR + encode(
                ["address", "uint256"], [spender, target]
            ).hex()
            intent = Intent.create(
                token,
                calldata,
                intent_id=None if selected_ids is None else selected_ids[index],
            )
            self.policy.validate(
                target=intent.target,
                calldata=intent.calldata,
                value=intent.value,
                now_ts=int(time.time()),
            )
            plans.append(
                ApprovalPlan(token, spender, current, target, intent)
            )
        return tuple(plans)
