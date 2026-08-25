"""仅依据链上池价持续出界时间进行确认。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class OutrangeState(str, Enum):
    """单池出界判定状态。"""

    IN_RANGE = "IN_RANGE"
    OUT_PENDING = "OUT_PENDING"
    CONFIRMED = "CONFIRMED"


class OutrangeDirection(str, Enum):
    """价格越过区间的方向。"""

    BELOW = "BELOW"
    ABOVE = "ABOVE"


class OutrangeResult(str, Enum):
    """本次出界观察的判定结果。"""

    REVERTED = "REVERTED"
    TIME_PENDING = "TIME_PENDING"
    TIME_CONFIRMED = "TIME_CONFIRMED"
    TIMEOUT_CONFIRMED = "TIMEOUT_CONFIRMED"


@dataclass(frozen=True)
class OutrangeEvent:
    """可直接交给日志层持久化的完整出界记录。"""

    triggered_at: datetime
    observed_at: datetime
    direction: OutrangeDirection
    pool_price: Decimal
    state: OutrangeState
    result: OutrangeResult
    reason: str
    pending_seconds: int


class OutrangeDetector:
    """价格连续位于区间外达到配置时长后确认出界。"""

    def __init__(self, *, confirm_seconds: int = 180, pin_timeout: int = 600) -> None:
        if confirm_seconds <= 0 or pin_timeout <= 0:
            raise ValueError("确认时间与出界上限必须大于零")
        self.confirm_seconds = confirm_seconds
        self.pin_timeout = pin_timeout
        self.state = OutrangeState.IN_RANGE
        self._triggered_at: datetime | None = None
        self._direction: OutrangeDirection | None = None
        self._records: list[OutrangeEvent] = []

    @property
    def records(self) -> tuple[OutrangeEvent, ...]:
        """返回按发生顺序保存的全部出界判定快照。"""
        return tuple(self._records)

    def restore_pending(
        self, triggered_at: datetime, direction: OutrangeDirection,
    ) -> None:
        """恢复进程重启前持久化的首次出界时间与方向。"""
        if triggered_at.tzinfo is None or triggered_at.utcoffset() is None:
            raise ValueError("首次出界时间必须包含时区")
        if not isinstance(direction, OutrangeDirection):
            raise ValueError("出界方向无效")
        self.state = OutrangeState.OUT_PENDING
        self._triggered_at = triggered_at
        self._direction = direction

    def reset(self) -> None:
        """完成重组或价格回归后复位计时状态。"""
        self.state = OutrangeState.IN_RANGE
        self._triggered_at = None
        self._direction = None

    def evaluate(
        self,
        pool_price: Decimal,
        lower_price: Decimal,
        upper_price: Decimal,
        observed_at: datetime,
    ) -> OutrangeEvent | None:
        """处理一个链上价格样本；区间内无待确认事件时返回空值。"""
        if lower_price >= upper_price:
            raise ValueError("区间下沿必须小于上沿")
        direction = (
            OutrangeDirection.BELOW if pool_price < lower_price else
            OutrangeDirection.ABOVE if pool_price > upper_price else None
        )
        if direction is None:
            return self._inside(pool_price, observed_at)
        if self.state is OutrangeState.CONFIRMED:
            return None
        if self.state is OutrangeState.IN_RANGE:
            self.state = OutrangeState.OUT_PENDING
            self._triggered_at = observed_at
            self._direction = direction
        elif direction is not self._direction:
            self._direction = direction
        pending = self._pending_seconds(observed_at)
        if pending >= self.pin_timeout:
            return self._record(
                pool_price, observed_at, OutrangeState.CONFIRMED,
                OutrangeResult.TIMEOUT_CONFIRMED,
                f"出界挂起超时 {self.pin_timeout} 秒，确认出界", pending,
            )
        if pending >= self.confirm_seconds:
            return self._record(
                pool_price, observed_at, OutrangeState.CONFIRMED,
                OutrangeResult.TIME_CONFIRMED,
                f"价格持续位于界外已达 {self.confirm_seconds} 秒，确认出界", pending,
            )
        return self._record(
            pool_price, observed_at, OutrangeState.OUT_PENDING,
            OutrangeResult.TIME_PENDING,
            f"等待价格持续位于界外 {self.confirm_seconds} 秒", pending,
        )

    def _inside(self, pool_price: Decimal, observed_at: datetime) -> OutrangeEvent | None:
        if self.state is not OutrangeState.OUT_PENDING:
            return None
        event = self._record(
            pool_price, observed_at, OutrangeState.IN_RANGE, OutrangeResult.REVERTED,
            "价格在确认前回到区间，重置出界计时", self._pending_seconds(observed_at),
        )
        self._triggered_at = None
        self._direction = None
        return event

    def _record(
        self, pool_price: Decimal, observed_at: datetime, state: OutrangeState,
        result: OutrangeResult, reason: str, pending_seconds: int,
    ) -> OutrangeEvent:
        if self._triggered_at is None or self._direction is None:
            raise RuntimeError("出界事件缺少首次出界时间或方向")
        self.state = state
        event = OutrangeEvent(
            self._triggered_at, observed_at, self._direction, pool_price,
            state, result, reason, pending_seconds,
        )
        self._records.append(event)
        return event

    def _pending_seconds(self, observed_at: datetime) -> int:
        started = self._triggered_at or observed_at
        return max(0, int((observed_at - started).total_seconds()))
