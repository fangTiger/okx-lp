"""可原子落盘并在重启后链上对账的交易 Intent。"""

from __future__ import annotations

import hashlib
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


class IntentIntegrityError(IntentStoreError):
    """表示落盘 Intent 内容完整性校验失败，记录不可信。"""


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

ALLOWED_TRANSITIONS = {
    IntentStatus.CREATED: frozenset({IntentStatus.PERSISTED}),
    IntentStatus.PERSISTED: frozenset(
        {IntentStatus.SIMULATED, IntentStatus.FAILED}
    ),
    IntentStatus.SIMULATED: frozenset(
        {IntentStatus.SIGNED, IntentStatus.FAILED}
    ),
    IntentStatus.SIGNED: frozenset(
        {IntentStatus.SENT, IntentStatus.DRY_RUN, IntentStatus.FAILED}
    ),
    IntentStatus.SENT: frozenset(
        {IntentStatus.CONFIRMED, IntentStatus.FAILED}
    ),
    IntentStatus.CONFIRMED: frozenset(),
    IntentStatus.FAILED: frozenset(),
    IntentStatus.DRY_RUN: frozenset(),
}


def _content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    def create(
        cls, target: str, calldata: str, *, value: int = 0,
        intent_id: str | None = None,
    ) -> "Intent":
        """创建具有合法唯一 ID 与 UTC 时间的 Intent。"""
        if type(value) is not int or value < 0:
            raise ValueError("Intent value 必须是非负整数")
        selected_id = uuid4().hex if intent_id is None else intent_id
        if type(selected_id) is not str or ID_PATTERN.fullmatch(selected_id) is None:
            raise ValueError("Intent ID 必须是 32 位小写十六进制字符")
        return cls(
            selected_id, target, calldata, value, datetime.now(timezone.utc)
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定 JSON 结构。"""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["status"] = self.status.value
        data["content_hash"] = _content_hash(data)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Intent":
        """从持久化 JSON 结构恢复 Intent。"""
        if type(data) is not dict:
            raise IntentStoreError("Intent 持久化内容格式非法")
        payload = dict(data)
        actual_hash = payload.pop("content_hash", None)
        if actual_hash != _content_hash(payload):
            raise IntentIntegrityError("Intent 落盘内容完整性校验失败")
        try:
            return cls(
                intent_id=payload["intent_id"], target=payload["target"],
                calldata=payload["calldata"], value=payload["value"],
                created_at=datetime.fromisoformat(payload["created_at"]),
                status=IntentStatus(payload["status"]), nonce=payload.get("nonce"),
                tx_hash=payload.get("tx_hash"), error=payload.get("error"),
                transaction=payload.get("transaction"),
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
            intent.value,
        )

    def persist(self, intent: Intent) -> Intent:
        """首次原子落盘；相同 ID 重试返回已有状态，冲突则拒绝。"""
        path = self._path(intent.intent_id)
        if path.exists():
            stored = self.load(intent.intent_id)
            if self._identity(stored) != self._identity(intent):
                raise IntentStoreError(f"Intent {intent.intent_id} 内容冲突")
            return stored
        if intent.status is not IntentStatus.CREATED:
            raise IntentStoreError(
                "Intent 状态转移非法："
                f"期望={IntentStatus.CREATED.value} -> {IntentStatus.PERSISTED.value}，"
                f"实际起点={intent.status.value}"
            )
        persisted = replace(intent, status=IntentStatus.PERSISTED)
        self._write(persisted)
        return persisted

    def save(self, intent: Intent) -> Intent:
        """保存已持久化 Intent 的后续状态。"""
        if not self._path(intent.intent_id).exists():
            raise IntentStoreError("Intent 尚未首次落盘，拒绝更新状态")
        stored = self.load(intent.intent_id)
        if self._identity(stored) != self._identity(intent):
            raise IntentStoreError(f"Intent {intent.intent_id} 内容冲突")
        allowed = ALLOWED_TRANSITIONS[stored.status]
        if intent.status not in allowed:
            expected = ", ".join(status.value for status in sorted(allowed, key=str))
            raise IntentStoreError(
                "Intent 状态转移非法："
                f"{stored.status.value} -> {intent.status.value}；"
                f"允许目标={expected or '无（终态）'}"
            )
        self._write(intent)
        return intent

    def quarantine_corrupted(self, intent: Intent) -> Intent:
        """原样隔离损坏记录，再落盘可供人工追溯的失败标记。"""
        path = self._path(intent.intent_id)
        if not path.exists():
            raise IntentStoreError("Intent 尚未首次落盘，拒绝记录完整性失败")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.root / f"{intent.intent_id}.corrupt-{timestamp}.json"
        if quarantine.exists():
            raise IntentStoreError(f"Intent 隔离文件已存在：{quarantine.name}")
        try:
            os.replace(path, quarantine)
        except OSError:
            raise IntentStoreError(f"Intent 损坏记录隔离失败：{intent.intent_id}") from None
        failed = replace(
            intent,
            status=IntentStatus.FAILED,
            nonce=None,
            tx_hash=None,
            error=(
                "Intent 落盘内容完整性校验失败，"
                f"原始记录已隔离至 {quarantine.name}"
            ),
            transaction=None,
        )
        self._write(failed)
        return failed

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
        paths = (
            path for path in sorted(self.root.glob("*.json"))
            if ID_PATTERN.fullmatch(path.stem) is not None
        )
        intents = (self.load(path.stem) for path in paths)
        return tuple(intent for intent in intents if intent.status not in TERMINAL_STATUSES)

    def reconcile_pending(self, rpc: RpcLike) -> tuple[Intent, ...]:
        """重启时用交易回执核对未完成 Intent，并持久化终态。"""
        reconciled = []
        for intent in self.load_pending():
            if intent.status is not IntentStatus.SENT or not intent.tx_hash:
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
