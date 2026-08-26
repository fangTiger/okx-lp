"""主状态机三个写链阶段的锁停执行器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from okxlp.strategy.machine_state import MachineSnapshot, MachineState


class MachineStages:
    """交易阶段失败后持久化锁停，禁止循环自动重做。"""

    def _enter_stage(self, sample: Any, now: datetime, allow_broadcast: bool) -> str:
        try:
            self.actions.enter(sample, self.band, allow_broadcast=allow_broadcast)
            reason = "建仓完成：已用 USDC 买入一半标的并 mint ±0.5% 区间"
            return self._transition(MachineState.IN_RANGE, reason, sample, self.band, now)
        except Exception as error:
            self._lock_stage(now, error)
            raise

    def _rebalance_stage(self, sample: Any, now: datetime, allow_broadcast: bool) -> str:
        try:
            target = self._target_band(sample.price)
            actions = self.actions.rebalance_actions(sample, target)
            self.rebalancer.execute(actions, allow_broadcast=allow_broadcast)
            reason = "再平衡完成：burn → collect → swap → mint"
            output = self._transition(MachineState.IN_RANGE, reason, sample, target, now)
            self.detector.reset()
            return output
        except Exception as error:
            self._lock_stage(now, error)
            raise

    def _exit_stage(self, sample: Any, now: datetime, allow_broadcast: bool) -> str:
        try:
            self.actions.exit(sample, allow_broadcast=allow_broadcast)
            reason = "撤出完成：burn → collect → 全部换成 USDC"
            output = self._transition(MachineState.IDLE, reason, sample, None, now)
            self.detector.reset()
            return output
        except Exception as error:
            self._lock_stage(now, error)
            raise

    def _lock_stage(self, now: datetime, error: Exception) -> None:
        detail = str(error) or error.__class__.__name__
        locked = MachineSnapshot(
            state=self.state, band=self.band,
            out_since=self.snapshot.out_since,
            out_direction=self.snapshot.out_direction,
            failure=detail, failed_at=now,
        )
        self.snapshot = locked
        try:
            self.state_store.save(locked)
        except Exception as persistence_error:
            raise RuntimeError(
                f"{detail}；阶段锁停落盘失败：{persistence_error}"
            ) from error
