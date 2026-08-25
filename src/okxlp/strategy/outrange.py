"""基于参考价基差突变的出界确认状态机。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from okxlp.strategy.basis import BasisEwma
ONE = Decimal("1")
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

    TRUE_MOVE = "TRUE_MOVE"
    PIN_PENDING = "PIN_PENDING"
    REVERTED = "REVERTED"
    TIME_PENDING = "TIME_PENDING"
    BASELINE_PENDING = "BASELINE_PENDING"
    TIME_CONFIRMED = "TIME_CONFIRMED"
    TIMEOUT_CONFIRMED = "TIMEOUT_CONFIRMED"

@dataclass(frozen=True)
class OutrangeEvent:
    """可直接交给日志层持久化的完整出界记录。"""

    triggered_at: datetime
    observed_at: datetime
    direction: OutrangeDirection
    pool_price: Decimal
    reference_price: Decimal | None
    basis: Decimal | None
    basis_ewma: Decimal | None
    state: OutrangeState
    result: OutrangeResult
    reason: str
    pending_seconds: int

class OutrangeDetector:
    """比较基差与近期均值，并在参考价缺失时按时间确认。"""

    def __init__(
        self,
        *,
        basis_jump_threshold: Decimal = Decimal("0.004"),
        confirm_seconds: int = 180,
        pin_timeout: int = 600,
        ewma_alpha: Decimal = Decimal("0.2"),
    ) -> None:
        if basis_jump_threshold <= 0 or confirm_seconds <= 0 or pin_timeout <= 0:
            raise ValueError("基差阈值与确认时间必须大于零")
        if not Decimal("0") < ewma_alpha <= ONE:
            raise ValueError("EWMA 权重必须在 0 到 1 之间")
        if pin_timeout < confirm_seconds:
            raise ValueError("插针超时不得短于无参考价确认时间")
        self.basis_jump_threshold = basis_jump_threshold
        self.confirm_seconds = confirm_seconds
        self.pin_timeout = pin_timeout
        self.ewma_alpha = ewma_alpha
        self.state = OutrangeState.IN_RANGE
        self._basis_ewma = BasisEwma(basis_jump_threshold, ewma_alpha)
        self._triggered_at: datetime | None = None
        self._direction: OutrangeDirection | None = None
        self._records: list[OutrangeEvent] = []

    @property
    def records(self) -> tuple[OutrangeEvent, ...]:
        """返回按发生顺序保存的全部出界判定快照。"""
        return tuple(self._records)

    @property
    def basis_ewma(self) -> Decimal | None:
        """返回已经通过可信初始化的基差均值。"""
        return self._basis_ewma.value

    def restore_basis_ewma(self, value: Decimal) -> None:
        """恢复已经持久化并校验过的可信基差均值。"""
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("恢复的基差均值必须是有限 Decimal")
        self._basis_ewma.value = value

    def reset(self) -> None:
        """完成重组后复位状态，同时保留基差均值与历史。"""
        self.state = OutrangeState.IN_RANGE
        self._triggered_at = None
        self._direction = None

    def evaluate(
        self,
        pool_price: Decimal,
        lower_price: Decimal,
        upper_price: Decimal,
        reference_price: Decimal | None,
        observed_at: datetime,
    ) -> OutrangeEvent | None:
        """处理一个价格样本；区间内无事件时返回空值。"""
        if lower_price >= upper_price:
            raise ValueError("区间下沿必须小于上沿")
        basis = self._basis(pool_price, reference_price)
        direction = (
            OutrangeDirection.BELOW if pool_price < lower_price else
            OutrangeDirection.ABOVE if pool_price > upper_price else None
        )
        if direction is None:
            return self._inside(pool_price, reference_price, basis, observed_at)
        if self.state is OutrangeState.CONFIRMED:
            return None
        if self.state is OutrangeState.IN_RANGE or direction is not self._direction:
            self.state = OutrangeState.OUT_PENDING
            self._triggered_at = observed_at
            self._direction = direction
        pending = self._pending_seconds(observed_at)
        if pending >= self.pin_timeout:
            return self._record(
                pool_price, reference_price, basis, observed_at,
                OutrangeState.CONFIRMED, OutrangeResult.TIMEOUT_CONFIRMED,
                f"出界挂起超时 {self.pin_timeout} 秒，强制确认", pending,
            )
        if basis is None:
            if pending >= self.confirm_seconds:
                reason = f"参考价不可用，价格持续位于界外已达 {self.confirm_seconds} 秒"
                return self._record(
                    pool_price, reference_price, basis, observed_at,
                    OutrangeState.CONFIRMED, OutrangeResult.TIME_CONFIRMED, reason, pending,
                )
            reason = f"参考价不可用，等待价格持续位于界外 {self.confirm_seconds} 秒"
            return self._record(
                pool_price, reference_price, basis, observed_at,
                OutrangeState.OUT_PENDING, OutrangeResult.TIME_PENDING, reason, pending,
            )
        if self.basis_ewma is None:
            return self._record(
                pool_price, reference_price, basis, observed_at,
                OutrangeState.OUT_PENDING, OutrangeResult.BASELINE_PENDING,
                "参考价可用但基差均值尚未建立，等待可信基线或挂起超时", pending,
            )
        if abs(basis - self.basis_ewma) <= self.basis_jump_threshold:
            event = self._record(
                pool_price, reference_price, basis, observed_at,
                OutrangeState.CONFIRMED, OutrangeResult.TRUE_MOVE,
                "基差相对均值未突变，确认真实移动", pending,
            )
            self._basis_ewma.observe(basis, observed_at)
            return event
        return self._record(
            pool_price, reference_price, basis, observed_at,
            OutrangeState.OUT_PENDING, OutrangeResult.PIN_PENDING,
            "基差相对均值突变，判定插针并挂起", pending,
        )

    def _inside(
        self, pool_price: Decimal, reference_price: Decimal | None,
        basis: Decimal | None, observed_at: datetime,
    ) -> OutrangeEvent | None:
        if self.state is not OutrangeState.OUT_PENDING:
            if basis is not None:
                self._basis_ewma.observe(basis, observed_at)
            return None
        event = self._record(
            pool_price, reference_price, basis, observed_at,
            OutrangeState.IN_RANGE, OutrangeResult.REVERTED,
            "价格在确认前回到区间，判定为插针或短时出界", self._pending_seconds(observed_at),
        )
        if basis is not None:
            self._basis_ewma.observe(basis, observed_at)
        self._triggered_at = None
        self._direction = None
        return event

    def _record(
        self, pool_price: Decimal, reference_price: Decimal | None,
        basis: Decimal | None, observed_at: datetime, state: OutrangeState,
        result: OutrangeResult, reason: str, pending_seconds: int,
    ) -> OutrangeEvent:
        self.state = state
        event = OutrangeEvent(
            self._triggered_at or observed_at, observed_at, self._direction,
            pool_price, reference_price, basis, self.basis_ewma,
            state, result, reason, pending_seconds,
        )
        self._records.append(event)
        return event

    def _basis(self, pool_price: Decimal, reference_price: Decimal | None) -> Decimal | None:
        if reference_price is None or reference_price <= 0:
            return None
        return pool_price / reference_price - ONE

    def _pending_seconds(self, observed_at: datetime) -> int:
        return max(0, int((observed_at - (self._triggered_at or observed_at)).total_seconds()))
