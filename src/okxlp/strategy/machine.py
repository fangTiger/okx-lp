"""组合时段、风控、出界判定与头寸编排的主状态机。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from okxlp.strategy.machine_state import (
    MachineSnapshot, MachineState, MachineStateStore, PriceBand,
)
from okxlp.strategy.machine_journal import TransitionJournal, TransitionRecord
from okxlp.strategy.machine_loop import MachineLoop
from okxlp.strategy.machine_stages import MachineStages
from okxlp.strategy.machine_types import (
    MarketSample, RiskDecision, StepResult, build_price_band,
)
from okxlp.strategy.outrange import (
    OutrangeDirection, OutrangeResult,
)


LOGGER = logging.getLogger("okxlp.strategy.machine")
CONFIRMED = frozenset({
    OutrangeResult.TIME_CONFIRMED,
    OutrangeResult.TIMEOUT_CONFIRMED,
})
class MainStateMachine(MachineLoop, MachineStages):
    """每池一个、失败停留当前阶段的同步主状态机。"""

    def __init__(
        self, *, pool_id: str, sessions: Any, risk_gate: Any, market: Any,
        actions: Any, rebalancer: Any, detector: Any,
        state_store: MachineStateStore, transition_journal: TransitionJournal,
        tick_spacing: int, token0_decimals: int, token1_decimals: int,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        if tick_spacing <= 0 or min(token0_decimals, token1_decimals) < 0:
            raise ValueError("tickSpacing 必须为正数，代币 decimals 不得为负数")
        self.pool_id, self.sessions, self.risk_gate = pool_id, sessions, risk_gate
        self.market, self.actions, self.rebalancer = market, actions, rebalancer
        self.detector, self.state_store = detector, state_store
        self.transition_journal = transition_journal
        self.tick_spacing = tick_spacing
        self.token0_decimals, self.token1_decimals = token0_decimals, token1_decimals
        self.clock, self.sleep = clock, sleep
        self.alert = alert if alert is not None else (
            lambda message: LOGGER.error("状态机告警：%s", message))
        self.snapshot = state_store.load()
        self._restore_detector(self.snapshot)

    @property
    def state(self) -> MachineState:
        """返回当前持久化阶段。"""
        return self.snapshot.state

    @property
    def band(self) -> PriceBand | None:
        """返回当前或正在构建的价格区间。"""
        return self.snapshot.band

    def _decide(
        self, now: datetime, make_market: bool, session_reason: str,
        risk: RiskDecision, allow_broadcast: bool,
    ) -> str:
        guard = None
        if not make_market:
            guard = f"离开做市时段：{session_reason}"
        elif not risk.allowed:
            guard = f"风控触发：{risk.reason}"
        if self.state is MachineState.IDLE and guard is not None:
            return f"保持 IDLE：{guard}"
        sample = self.market.snapshot(now)
        if self.snapshot.failure is not None:
            return (
                f"保持 {self.state.value}：阶段已锁停（{self.snapshot.failure}），"
                "等待链上对账或人工处理"
            )
        if self.state is MachineState.EXITING and not risk.allowed and not risk.allow_exit:
            return f"保持 EXITING：风控闸门禁止撤出写链：{risk.reason}"
        if self.state is not MachineState.EXITING and guard is not None:
            transition_reason = self._transition(
                MachineState.EXITING, guard, sample, self.band, now,
            )
            if not risk.allowed and not risk.allow_exit:
                return f"保持 EXITING：风控闸门禁止撤出写链：{risk.reason}"
            exit_reason = self._exit_stage(sample, now, allow_broadcast)
            return f"{transition_reason}；{exit_reason}"
        if self.state is MachineState.IDLE:
            band = self._target_band(sample.price)
            reason = f"做市条件满足：{session_reason}；{risk.reason}"
            return self._transition(MachineState.ENTERING, reason, sample, band, now)
        if self.state is MachineState.ENTERING:
            return self._enter_stage(sample, now, allow_broadcast)
        if self.state is MachineState.IN_RANGE:
            return self._in_range(sample, now)
        if self.state is MachineState.OUT_PENDING:
            return self._out_pending(sample, now)
        if self.state is MachineState.REBALANCING:
            return self._rebalance_stage(sample, now, allow_broadcast)
        return self._exit_stage(sample, now, allow_broadcast)

    def _in_range(self, sample: MarketSample, now: datetime) -> str:
        band = self._required_band()
        event = self.detector.evaluate(
            sample.price, band.price_lower, band.price_upper, now,
        )
        if event is None:
            return "池价仍在区间内"
        side = "下沿" if sample.price < band.price_lower else "上沿"
        reason = f"池价越过区间{side}；{event.reason}"
        return self._transition(
            MachineState.OUT_PENDING, reason, sample, band, now,
            out_since=event.triggered_at, out_direction=event.direction.value,
        )

    def _out_pending(self, sample: MarketSample, now: datetime) -> str:
        band = self._required_band()
        event = self.detector.evaluate(
            sample.price, band.price_lower, band.price_upper, now,
        )
        if event is None:
            return "等待出界判定"
        if event.result is OutrangeResult.REVERTED:
            return self._transition(MachineState.IN_RANGE, event.reason, sample, band, now)
        if event.result in CONFIRMED:
            return self._transition(
                MachineState.REBALANCING, f"确认需要重组：{event.reason}", sample, band, now
            )
        if (
            event.direction.value != self.snapshot.out_direction
            or event.triggered_at != self.snapshot.out_since
        ):
            updated = MachineSnapshot(
                MachineState.OUT_PENDING, band, event.triggered_at, event.direction.value,
            )
            self.state_store.save(updated)
            self.snapshot = updated
        return event.reason

    def _target_band(self, price: Decimal) -> PriceBand:
        return build_price_band(
            price, self.tick_spacing, self.token0_decimals, self.token1_decimals,
        )

    def _required_band(self) -> PriceBand:
        if self.band is None:
            raise RuntimeError(f"状态 {self.state.value} 缺少做市区间")
        return self.band

    def _transition(
        self, state: MachineState, reason: str, sample: MarketSample,
        band: PriceBand | None, now: datetime, *,
        out_since: datetime | None = None, out_direction: str | None = None,
    ) -> str:
        previous = self.snapshot
        current = MachineSnapshot(state, band, out_since, out_direction)
        record_band = band if band is not None else previous.band
        state_saved = False
        try:
            self.state_store.save(current)
            state_saved = True
            self.transition_journal.append(TransitionRecord(
                now, self.pool_id, previous.state, state, reason,
                sample.price, sample.tick, record_band,
            ))
        except Exception:
            try:
                if state_saved:
                    self.state_store.save(previous)
            finally:
                self._restore_detector(previous)
            raise
        self.snapshot = current
        LOGGER.info("状态转移：%s → %s，原因：%s", previous.state.value, state.value, reason)
        return reason

    def _restore_detector(self, snapshot: MachineSnapshot) -> None:
        """让检测器内存态与已持久化的主状态保持一致。"""
        self.detector.reset()
        if snapshot.state is MachineState.OUT_PENDING:
            self.detector.restore_pending(
                snapshot.out_since, OutrangeDirection(snapshot.out_direction),
            )
