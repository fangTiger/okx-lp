"""主状态机的持久化状态与结构化转移日志。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any


class MachineState(str, Enum):
    """单池主状态机的六个阶段。"""

    IDLE = "IDLE"
    ENTERING = "ENTERING"
    IN_RANGE = "IN_RANGE"
    OUT_PENDING = "OUT_PENDING"
    REBALANCING = "REBALANCING"
    EXITING = "EXITING"


class StatePersistenceError(RuntimeError):
    """表示状态或转移记录无法可靠持久化。"""


@dataclass(frozen=True)
class PriceBand:
    """已经按 tickSpacing 向外对齐的做市区间。"""

    tick_lower: int
    tick_upper: int
    price_lower: Decimal
    price_upper: Decimal

    def __post_init__(self) -> None:
        if type(self.tick_lower) is not int or type(self.tick_upper) is not int:
            raise ValueError("做市区间 tick 必须是整数")
        prices = (self.price_lower, self.price_upper)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in prices):
            raise ValueError("做市区间价格必须是有限 Decimal")
        if self.tick_lower >= self.tick_upper or self.price_lower >= self.price_upper:
            raise ValueError("做市区间下沿必须小于上沿")


@dataclass(frozen=True)
class MachineSnapshot:
    """可在进程重启后恢复的最小本地状态。"""

    state: MachineState
    band: PriceBand | None = None
    out_since: datetime | None = None
    out_direction: str | None = None
    failure: str | None = None
    failed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state is not MachineState.IDLE and self.band is None:
            raise ValueError(f"状态 {self.state.value} 缺少做市区间")
        pending = self.out_since is not None or self.out_direction is not None
        if self.state is MachineState.OUT_PENDING:
            if self.out_since is None or self.out_direction not in {"BELOW", "ABOVE"}:
                raise ValueError("OUT_PENDING 缺少有效的出界时间或方向")
            if self.out_since.tzinfo is None or self.out_since.utcoffset() is None:
                raise ValueError("出界时间必须包含时区")
        elif pending:
            raise ValueError(f"状态 {self.state.value} 不得保存出界挂起字段")
        failed = self.failure is not None or self.failed_at is not None
        if failed:
            if type(self.failure) is not str or not self.failure.strip() or self.failed_at is None:
                raise ValueError("阶段锁停必须包含失败原因和时间")
            if self.failed_at.tzinfo is None or self.failed_at.utcoffset() is None:
                raise ValueError("阶段失败时间必须包含时区")


class MachineStateStore:
    """用原子替换持久化单池主状态。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> MachineSnapshot:
        """读取上次状态；文件不存在表示首次启动。"""
        if not self.path.exists():
            return MachineSnapshot(MachineState.IDLE)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = MachineState(raw["state"])
            band = None if raw.get("band") is None else _band(raw["band"])
            out_since = None if raw.get("out_since") is None else _datetime(raw["out_since"])
            failed_at = None if raw.get("failed_at") is None else _datetime(raw["failed_at"])
            return MachineSnapshot(
                state=state, band=band, out_since=out_since,
                out_direction=raw.get("out_direction"),
                failure=raw.get("failure"), failed_at=failed_at,
            )
        except (
            OSError, KeyError, TypeError, ValueError, InvalidOperation,
            json.JSONDecodeError,
        ) as error:
            raise StatePersistenceError(f"状态文件非法 {self.path}：{error}") from None

    def save(self, snapshot: MachineSnapshot) -> None:
        """先写临时文件并同步，再原子替换正式状态。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": snapshot.state.value,
            "band": None if snapshot.band is None else band_dict(snapshot.band),
            "out_since": (
                None if snapshot.out_since is None else timestamp_text(snapshot.out_since)
            ),
            "out_direction": snapshot.out_direction,
            "failure": snapshot.failure,
            "failed_at": None if snapshot.failed_at is None else timestamp_text(snapshot.failed_at),
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=".machine-state-", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise StatePersistenceError(f"状态落盘失败：{error}") from None
def _band(raw: Any) -> PriceBand:
    if type(raw) is not dict:
        raise ValueError("band 必须是映射")
    return PriceBand(
        raw["tick_lower"], raw["tick_upper"],
        Decimal(raw["price_lower"]), Decimal(raw["price_upper"]),
    )


def band_dict(band: PriceBand) -> dict[str, Any]:
    return {
        "tick_lower": band.tick_lower, "tick_upper": band.tick_upper,
        "price_lower": str(band.price_lower), "price_upper": str(band.price_upper),
    }


def timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("状态转移时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime:
    if type(value) is not str:
        raise ValueError("时间必须是 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)
