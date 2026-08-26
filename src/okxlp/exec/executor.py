"""默认不广播、逐步落日志的安全交易执行器。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from eth_utils import keccak, to_checksum_address

from okxlp.exec.authorization import require_broadcast_flag
from okxlp.exec.intent import Intent, IntentStatus, IntentStore, TERMINAL_STATUSES


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
        gas_estimator: Any, whitelist: Any, store: IntentStore,
        chain_id: int, printer: Callable[[str], None] = print,
        confirmation_timeout: float = 120.0, poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rpc = rpc
        self.signer = signer
        self.nonce_manager = nonce_manager
        self.gas_estimator = gas_estimator
        self.whitelist = whitelist
        self.store = store
        self.chain_id = chain_id
        self.printer = printer
        self.confirmation_timeout = confirmation_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.monotonic = monotonic

    def execute(self, intent: Intent, *, allow_broadcast: bool = False) -> ExecutionResult:
        """执行安全流水线；只有显式授权时才调用广播函数。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        LOGGER.info("Intent %s 开始白名单校验", intent.intent_id)
        try:
            selector = self.whitelist.validate(intent.target, intent.calldata)
        except Exception as error:
            LOGGER.error("Intent %s 白名单拒绝：%s", intent.intent_id, error)
            raise
        LOGGER.info("Intent %s 白名单校验通过：%s", intent.intent_id, selector)
        current = self.store.persist(intent)
        LOGGER.info("Intent %s 已落盘：%s", intent.intent_id, current.status.value)
        if current.status in TERMINAL_STATUSES:
            LOGGER.info("Intent %s 已是终态，幂等返回", intent.intent_id)
            return ExecutionResult(current, current.transaction or {})
        if current.status == IntentStatus.SENT:
            return self._resume_sent(current)
        if current.status == IntentStatus.SIGNED and current.transaction:
            return self._finish_signed(current, broadcast)

        rpc_transaction = {
            "from": self.signer.address, "to": intent.target,
            "data": intent.calldata, "value": hex(intent.value),
        }
        LOGGER.info("Intent %s 开始 eth_call 模拟", intent.intent_id)
        try:
            self.rpc.call("eth_call", [rpc_transaction, "pending"])
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(
                replace(current, status=IntentStatus.FAILED, error=f"模拟回滚：{reason}")
            )
            LOGGER.error("Intent %s 模拟回滚，中止执行：%s", intent.intent_id, reason)
            raise ExecutionError(f"交易模拟回滚：{reason}") from None
        current = self.store.save(replace(current, status=IntentStatus.SIMULATED))
        LOGGER.info("Intent %s eth_call 模拟通过", intent.intent_id)

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
            raw = self.signer.sign_transaction(transaction)
        except Exception as error:
            reason = str(error) or error.__class__.__name__
            self.store.save(replace(current, status=IntentStatus.FAILED, error=reason))
            LOGGER.error("Intent %s gas、nonce 或签名步骤失败：%s", intent.intent_id, reason)
            raise ExecutionError(f"交易准备失败：{reason}") from None
        tx_hash = "0x" + keccak(raw).hex()
        current = self.store.save(
            replace(
                current, status=IntentStatus.SIGNED, nonce=nonce,
                tx_hash=tx_hash, transaction=transaction,
            )
        )
        LOGGER.info("Intent %s 签名完成，预期交易哈希：%s", intent.intent_id, tx_hash)
        return self._finish_signed(current, broadcast, raw)

    def _finish_signed(
        self, intent: Intent, allow_broadcast: bool, raw: bytes | None = None
    ) -> ExecutionResult:
        broadcast = require_broadcast_flag(allow_broadcast)
        transaction = intent.transaction or {}
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
