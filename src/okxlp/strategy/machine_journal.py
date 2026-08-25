"""主状态机的结构化状态转移日志。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from okxlp.strategy.machine_state import (
    MachineState, PriceBand, StatePersistenceError, band_dict, timestamp_text,
)


@dataclass(frozen=True)
class TransitionRecord:
    """一次状态转移的完整审计字段。"""

    timestamp: datetime
    pool_id: str
    old_state: MachineState
    new_state: MachineState
    reason: str
    pool_price: Decimal
    tick: int
    band: PriceBand | None


class TransitionJournal:
    """以一行一个 JSON 对象记录所有状态转移。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: TransitionRecord) -> None:
        """追加并同步一条结构化转移记录。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "timestamp": timestamp_text(record.timestamp), "pool_id": record.pool_id,
            "old_state": record.old_state.value, "new_state": record.new_state.value,
            "reason": record.reason, "pool_price": str(record.pool_price),
            "tick": record.tick,
            "range": None if record.band is None else band_dict(record.band),
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise StatePersistenceError(f"状态转移日志落盘失败：{error}") from None
