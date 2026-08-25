"""可原子落盘并在重启后链上对账的交易 Intent。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class IntentStoreError(RuntimeError):
    """表示 Intent 内容、落盘或恢复失败。"""


class IntentStatus(str, Enum):
    """Intent 从创建到终态的持久化状态。"""

    CREATED = "created"
    PERSISTED = "persisted"
    SIMULATED = "simulated"
    SIGNED = "signed"
    SENT = "sent"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    DRY_RUN = "dry_run"


TERMINAL_STATUSES = frozenset(
    {IntentStatus.CONFIRMED, IntentStatus.FAILED, IntentStatus.DRY_RUN}
)


class RpcLike(Protocol):
    """Intent 恢复所需的最小 RPC 接口。"""

    def call(self, method: str, params: list[Any]) -> Any: ...


@dataclass(frozen=True)
class Intent:
    """策略层与执行层之间不含密钥的交易意图。"""

    intent_id: str
    target: str
    calldata: str
    value: int
    created_at: datetime
    status: IntentStatus = IntentStatus.CREATED
    nonce: int | None = None
    tx_hash: str | None = None
    error: str | None = None
    transaction: dict[str, Any] | None = None

    @classmethod
    def create(cls, target: str, calldata: str, *, value: int = 0) -> "Intent":
        """创建具有随机唯一 ID 与 UTC 时间的 Intent。"""
        if type(value) is not int or value < 0:
            raise ValueError("Intent value 必须是非负整数")
        return cls(uuid4().hex, target, calldata, value, datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定 JSON 结构。"""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Intent":
        """从持久化 JSON 结构恢复 Intent。"""
        try:
            return cls(
                intent_id=data["intent_id"], target=data["target"],
                calldata=data["calldata"], value=data["value"],
                created_at=datetime.fromisoformat(data["created_at"]),
                status=IntentStatus(data["status"]), nonce=data.get("nonce"),
                tx_hash=data.get("tx_hash"), error=data.get("error"),
                transaction=data.get("transaction"),
            )
        except (KeyError, TypeError, ValueError):
            raise IntentStoreError("Intent 持久化内容格式非法") from None


class IntentStore:
    """以每个 Intent 一个 JSON 文件实现幂等和崩溃恢复。"""

    def __init__(self, root: Path = Path("log/intents")) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, intent_id: str) -> Path:
        if ID_PATTERN.fullmatch(intent_id) is None:
            raise IntentStoreError("Intent ID 格式非法")
        return self.root / f"{intent_id}.json"

    @staticmethod
    def _identity(intent: Intent) -> tuple[Any, ...]:
        return (
            intent.intent_id, intent.target, intent.calldata,
            intent.value, intent.created_at,
        )

    def persist(self, intent: Intent) -> Intent:
        """首次原子落盘；相同 ID 重试返回已有状态，冲突则拒绝。"""
        path = self._path(intent.intent_id)
        if path.exists():
            stored = self.load(intent.intent_id)
            if self._identity(stored) != self._identity(intent):
                raise IntentStoreError(f"Intent {intent.intent_id} 内容冲突")
            return stored
        persisted = replace(intent, status=IntentStatus.PERSISTED)
        self._write(persisted)
        return persisted

    def save(self, intent: Intent) -> Intent:
        """保存已持久化 Intent 的后续状态。"""
        if not self._path(intent.intent_id).exists():
            raise IntentStoreError("Intent 尚未首次落盘，拒绝更新状态")
        self._write(intent)
        return intent

    def _write(self, intent: Intent) -> None:
        path = self._path(intent.intent_id)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, prefix=".intent-", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(intent.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise IntentStoreError(f"Intent 落盘失败：{intent.intent_id}") from None

    def load(self, intent_id: str) -> Intent:
        """按 ID 读取一个 Intent。"""
        try:
            return Intent.from_dict(json.loads(self._path(intent_id).read_text(encoding="utf-8")))
        except OSError:
            raise IntentStoreError(f"无法读取 Intent：{intent_id}") from None
        except json.JSONDecodeError:
            raise IntentStoreError(f"Intent JSON 已损坏：{intent_id}") from None

    def load_pending(self) -> tuple[Intent, ...]:
        """读取所有未进入终态的 Intent。"""
        intents = (self.load(path.stem) for path in sorted(self.root.glob("*.json")))
        return tuple(intent for intent in intents if intent.status not in TERMINAL_STATUSES)

    def reconcile_pending(self, rpc: RpcLike) -> tuple[Intent, ...]:
        """重启时用交易回执核对未完成 Intent，并持久化终态。"""
        reconciled = []
        for intent in self.load_pending():
            if not intent.tx_hash:
                reconciled.append(intent)
                continue
            receipt = rpc.call("eth_getTransactionReceipt", [intent.tx_hash])
            if receipt is None:
                reconciled.append(intent)
                continue
            succeeded = int(receipt.get("status", "0x0"), 16) == 1
            status = IntentStatus.CONFIRMED if succeeded else IntentStatus.FAILED
            error = intent.error if succeeded else "链上交易执行失败"
            intent = self.save(replace(intent, status=status, error=error))
            reconciled.append(intent)
        return tuple(reconciled)
