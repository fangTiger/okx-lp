"""NAV 快照类型与受节流的按日 JSONL 记录器。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class NavSnapshot:
    """仅包含字符串 Decimal 与整数原始数量的 NAV 快照。"""

    ts: str
    block: int
    price: str
    position_value_usdc: str
    idle0_raw: int
    idle1_raw: int
    total_usdc: str

    def __post_init__(self) -> None:
        _utc_timestamp(self.ts)
        if type(self.block) is not int or self.block < 0:
            raise TypeError("block 必须是非负整数")
        for name in ("idle0_raw", "idle1_raw"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(f"{name} 必须是非负整数")
        for name in ("price", "position_value_usdc", "total_usdc"):
            _decimal_text(getattr(self, name), name)


class NavRecorder:
    """按 UTC 日期追加 NAV 快照，并避免高频循环重复写入。"""

    def __init__(
        self, root: Path = Path("log"), min_interval_seconds: int = 300
    ) -> None:
        if (
            type(min_interval_seconds) is not int
            or min_interval_seconds < 0
        ):
            raise ValueError("min_interval_seconds 必须是非负整数")
        self.root = Path(root)
        self.min_interval = timedelta(seconds=min_interval_seconds)
        self._last_timestamp: datetime | None = None

    def record(self, snapshot: NavSnapshot) -> bool:
        """追加一条快照；同一天间隔不足时返回 False。"""
        if not isinstance(snapshot, NavSnapshot):
            raise TypeError("snapshot 必须是 NavSnapshot")
        timestamp = _utc_timestamp(snapshot.ts)
        if (
            self._last_timestamp is not None
            and timestamp.date() == self._last_timestamp.date()
            and timestamp - self._last_timestamp < self.min_interval
        ):
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"nav_{timestamp.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            json.dump(
                asdict(snapshot), handle, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            )
            handle.write("\n")
        self._last_timestamp = timestamp
        return True


def _utc_timestamp(value: str) -> datetime:
    if type(value) is not str or not value.strip():
        raise TypeError("ts 必须是 UTC ISO8601 字符串")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("ts 必须是 UTC ISO8601 字符串") from None
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError("ts 必须使用 UTC 时区")
    return timestamp


def _decimal_text(value: str, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} 必须是字符串")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} 必须是 Decimal 字符串") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} 必须是有限非负 Decimal 字符串")
