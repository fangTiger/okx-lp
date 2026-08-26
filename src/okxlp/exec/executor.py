"""默认不广播、逐步落日志的安全交易执行器。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from eth_utils import keccak, to_checksum_address

from okxlp.chain.calldata_policy import CalldataPolicy
from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.intent import (
    Intent, IntentIntegrityError, IntentStatus, IntentStore, IntentStoreError,
    TERMINAL_STATUSES,
)


LOGGER = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    """表示交易在安全执行流程中止。"""


class RpcLike(Protocol):
    """执行器所需的最小 RPC 接口。"""

    def call(self, method: str, params: list[Any]) -> Any: ...
    def send_raw_transaction(
        self, raw_transaction: bytes, *, allow_broadcast: bool = False
    ) -> str: ...


@dataclass(frozen=True)
class ExecutionResult:
    """执行后的 Intent、完整交易和可选回执。"""

    intent: Intent
    transaction: dict[str, Any]
    receipt: dict[str, Any] | None = None


class TransactionExecutor:
    """按固定安全顺序模拟、签名并可选发送单个 Intent。"""

    def __init__(
        self, *, rpc: RpcLike, signer: Any, nonce_manager: Any,
        gas_estimator: Any, whitelist: Any, calldata_policy: CalldataPolicy,
        store: IntentStore,
        chain_id: int, printer: Callable[[str], None] = print,
        confirmation_timeout: float = 120.0, poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.rpc = rpc
        self.signer = signer
        self.nonce_manager = nonce_manager
        self.gas_estimator = gas_estimator
        self.whitelist = whitelist
        self.calldata_policy = calldata_policy
        self.store = store
        self.chain_id = chain_id
        self.printer = printer
        self.confirmation_timeout = confirmation_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.monotonic = monotonic
        self.clock = clock

    def execute(
        self, intent: Intent, *, allow_broadcast: bool = False,
        simulation_check: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        """执行安全流水线；只有显式授权时才调用广播函数。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        self._validate_intent(intent)
        try:
            current = self.store.persist(intent)
        except IntentIntegrityError:
            self.store.quarantine_corrupted(intent)
            LOGGER.error("Intent %s 落盘内容完整性校验失败", intent.intent_id)
            raise
        LOGGER.info("Intent %s 已落盘：%s", intent.intent_id, current.status.value)
        if current.status in TERMINAL_STATUSES:
            LOGGER.info("Intent %s 已是终态，幂等返回", intent.intent_id)
            return ExecutionResult(current, current.transaction or {})
        if current.status == IntentStatus.SENT:
            return self._resume_sent(current)
        if current.status == IntentStatus.SIGNED:
            return self._resume_signed(
                current, broadcast, simulation_check
            )

        current = self._simulate(
            intent, current,
            persist_success=current.status is IntentStatus.PERSISTED,
            simulation_check=simulation_check,
        )
        rpc_transaction = self._rpc_transaction(intent)

        try:
            quote = self.gas_estimator.estimate(rpc_transaction)
            LOGGER.info(
                "Intent %s gas 估算完成：limit=%d maxFee=%d priority=%d",
                intent.intent_id, quote.gas_limit, quote.max_fee_per_gas,
                quote.max_priority_fee_per_gas,
            )
            nonce = self.nonce_manager.reserve()
            LOGGER.info("Intent %s pending nonce 已对账并保留：%d", intent.intent_id, nonce)
            transaction = {
                "chainId": self.chain_id, "nonce": nonce,
                "to": to_checksum_address(intent.target), "data": intent.calldata,
                "value": intent.value, "gas": quote.gas_limit,
                "maxFeePerGas": quote.max_fee_per_gas,
                "maxPriorityFeePerGas": quote.max_priority_fee_per_gas, "type": 2,
            }
            candidate = replace(
                current, status=IntentStatus.SIGNED, nonce=nonce,
                transaction=transaction,
            )
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(replace(current, status=IntentStatus.FAILED, error=reason))
            LOGGER.error(
                "Intent %s gas、nonce 或交易构造步骤失败：%s",
                intent.intent_id, reason,
            )
            raise ExecutionError(f"交易准备失败：{reason}") from None
        self._assert_transaction_matches_intent(candidate)
        try:
            raw = self.signer.sign_transaction(transaction)
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(replace(current, status=IntentStatus.FAILED, error=reason))
            LOGGER.error("Intent %s 签名步骤失败：%s", intent.intent_id, reason)
            raise ExecutionError(f"交易准备失败：{reason}") from None
        tx_hash = "0x" + keccak(raw).hex()
        current = self.store.save(
            replace(
                candidate,
                tx_hash=tx_hash, transaction=transaction,
            )
        )
        LOGGER.info("Intent %s 签名完成，预期交易哈希：%s", intent.intent_id, tx_hash)
        return self._finish_signed(current, broadcast, raw)

    def _validate_intent(self, intent: Intent) -> None:
        LOGGER.info("Intent %s 开始白名单校验", intent.intent_id)
        try:
            selector = self.whitelist.validate(intent.target, intent.calldata)
        except Exception as error:
            self._record_existing_validation_failure(intent, error)
            LOGGER.error("Intent %s 白名单拒绝：%s", intent.intent_id, error)
            raise
        LOGGER.info("Intent %s 白名单校验通过：%s", intent.intent_id, selector)
        LOGGER.info("Intent %s 开始 calldata 参数策略校验", intent.intent_id)
        try:
            self.calldata_policy.validate(
                target=intent.target, calldata=intent.calldata,
                value=intent.value, now_ts=self.clock(),
            )
        except Exception as error:
            self._record_existing_validation_failure(intent, error)
            LOGGER.error("Intent %s calldata 参数策略拒绝：%s", intent.intent_id, error)
            raise
        LOGGER.info("Intent %s calldata 参数策略校验通过", intent.intent_id)

    def _record_existing_validation_failure(
        self, intent: Intent, error: Exception
    ) -> None:
        """已有可信记录校验失败时按状态表落为 FAILED。"""
        try:
            current = self.store.load(intent.intent_id)
        except IntentStoreError:
            return
        stored_identity = (
            current.intent_id, current.target, current.calldata,
            current.value,
        )
        incoming_identity = (
            intent.intent_id, intent.target, intent.calldata,
            intent.value,
        )
        if stored_identity != incoming_identity:
            return
        if current.status in TERMINAL_STATUSES:
            return
        reason = str(error) or error.__class__.__name__
        self.store.save(
            replace(current, status=IntentStatus.FAILED, error=reason)
        )

    def _rpc_transaction(self, intent: Intent) -> dict[str, Any]:
        return {
            "from": self.signer.address, "to": intent.target,
            "data": intent.calldata, "value": hex(intent.value),
        }

    def _simulate(
        self, intent: Intent, current: Intent, *, persist_success: bool,
        simulation_check: Callable[[str], None] | None = None,
    ) -> Intent:
        rpc_transaction = self._rpc_transaction(intent)
        LOGGER.info("Intent %s 开始 eth_call 模拟", intent.intent_id)
        try:
            raw_result = self.rpc.call(
                "eth_call", [rpc_transaction, "pending"]
            )
            if simulation_check is not None:
                simulation_check(raw_result)
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(
                replace(current, status=IntentStatus.FAILED, error=f"模拟回滚：{reason}")
            )
            LOGGER.error("Intent %s 模拟回滚，中止执行：%s", intent.intent_id, reason)
            raise ExecutionError(f"交易模拟回滚：{reason}") from None
        if persist_success:
            current = self.store.save(replace(current, status=IntentStatus.SIMULATED))
        LOGGER.info("Intent %s eth_call 模拟通过", intent.intent_id)
        return current

    def _finish_signed(
        self, intent: Intent, allow_broadcast: bool, raw: bytes | None = None
    ) -> ExecutionResult:
        broadcast = require_broadcast_flag(allow_broadcast)
        transaction = self._assert_transaction_matches_intent(intent)
        if raw is None:
            raw = self.signer.sign_transaction(transaction)
        expected_hash = "0x" + keccak(raw).hex()
        if broadcast is not True:
            rendered = json.dumps(transaction, ensure_ascii=False, sort_keys=True)
            self.printer(f"dry-run 完整交易内容：{rendered}")
            dry_run = self.store.save(replace(intent, status=IntentStatus.DRY_RUN))
            LOGGER.info("Intent %s dry-run 完成，广播保持禁用", intent.intent_id)
            return ExecutionResult(dry_run, transaction)
        try:
            returned_hash = self.rpc.send_raw_transaction(raw, allow_broadcast=True)
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(replace(intent, status=IntentStatus.FAILED, error=reason))
            LOGGER.error("Intent %s 广播失败：%s", intent.intent_id, reason)
            raise ExecutionError(f"交易广播失败：{reason}") from None
        if returned_hash.lower() != expected_hash.lower():
            reason = (
                "节点返回的交易哈希与本地签名不一致："
                f"本地={expected_hash}，节点={returned_hash}"
            )
            self.store.save(replace(intent, status=IntentStatus.FAILED, error=reason))
            LOGGER.error("Intent %s %s", intent.intent_id, reason)
            raise ExecutionError("节点返回的交易哈希与本地签名不一致，已中止")
        sent = self.store.save(
            replace(intent, status=IntentStatus.SENT, tx_hash=returned_hash)
        )
        LOGGER.info("Intent %s 已发送，等待确认：%s", intent.intent_id, returned_hash)
        return self._wait_for_receipt(sent)

    def _resume_signed(
        self, intent: Intent, allow_broadcast: bool,
        simulation_check: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        """对 SIGNED 记录重新授权、核对并模拟后再进入签名边界。"""
        LOGGER.info("Intent %s 从已签名状态恢复，重新执行全部安全校验", intent.intent_id)
        self._validate_intent(intent)
        self._assert_transaction_matches_intent(intent)
        self._simulate(
            intent, intent, persist_success=False,
            simulation_check=simulation_check,
        )
        return self._finish_signed(intent, allow_broadcast)

    def _assert_transaction_matches_intent(self, intent: Intent) -> dict[str, Any]:
        """拒绝任何与已授权 Intent 不完全一致的持久化交易。"""
        transaction = intent.transaction
        matches = type(transaction) is dict
        if matches:
            try:
                matches = (
                    type(transaction["chainId"]) is int
                    and transaction["chainId"] == self.chain_id
                    and to_checksum_address(transaction["to"])
                    == to_checksum_address(intent.target)
                    and transaction["data"] == intent.calldata
                    and type(transaction["value"]) is int
                    and transaction["value"] == intent.value
                    and type(transaction["type"]) is int
                    and transaction["type"] == 2
                )
                for name in (
                    "nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas"
                ):
                    value = transaction[name]
                    matches = matches and type(value) is int and value >= 0
            except (KeyError, TypeError, ValueError):
                matches = False
        if not matches:
            message = "持久化交易与 Intent 不一致，已中止"
            self.store.save(
                replace(intent, status=IntentStatus.FAILED, error=message)
            )
            LOGGER.error("Intent %s %s", intent.intent_id, message)
            raise ExecutionError(message)
        return transaction

    def _resume_sent(self, intent: Intent) -> ExecutionResult:
        LOGGER.info("Intent %s 从已发送状态恢复确认", intent.intent_id)
        return self._wait_for_receipt(intent)

    def _wait_for_receipt(self, intent: Intent) -> ExecutionResult:
        deadline = self.monotonic() + self.confirmation_timeout
        while self.monotonic() <= deadline:
            receipt = self.rpc.call("eth_getTransactionReceipt", [intent.tx_hash])
            if receipt is not None:
                succeeded = int(receipt.get("status", "0x0"), 16) == 1
                status = IntentStatus.CONFIRMED if succeeded else IntentStatus.FAILED
                error = None if succeeded else "链上交易执行失败"
                final = self.store.save(replace(intent, status=status, error=error))
                LOGGER.info("Intent %s 确认结果：%s", intent.intent_id, status.value)
                return ExecutionResult(final, intent.transaction or {}, receipt)
            self.sleep(self.poll_interval)
        LOGGER.warning("Intent %s 等待确认超时，保留 sent 状态供重启对账", intent.intent_id)
        return ExecutionResult(intent, intent.transaction or {})
