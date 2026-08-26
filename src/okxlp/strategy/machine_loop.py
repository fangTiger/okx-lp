"""主状态机的安全单步入口与 5/60 秒循环。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from okxlp.exec.authorization import require_broadcast_flag
from okxlp.strategy.machine_types import RiskDecision, StepResult


LOGGER = logging.getLogger("okxlp.strategy.machine")


class MachineLoop:
    """为主状态机提供统一异常处理和循环节奏。"""

    def step(self, *, allow_broadcast: bool = False) -> StepResult:
        """严格按时段、风控、策略的顺序执行一轮。"""
        now = self.clock()
        make_market, session_reason = False, "时段检查未完成"
        risk = RiskDecision(False, "风控检查未完成")
        try:
            broadcast = require_broadcast_flag(allow_broadcast)
            make_market, session_reason = self.sessions.should_make_market(now)
            risk = self.risk_gate.check(now)
            if not isinstance(risk, RiskDecision):
                raise TypeError("风控闸门必须返回 RiskDecision")
            reason = self._decide(now, make_market, session_reason, risk, broadcast)
        except Exception as error:
            detail = str(error) or error.__class__.__name__
            reason = f"步骤失败，停留在 {self.state.value}：{detail}"
            LOGGER.error("%s", reason, exc_info=True)
            try:
                self.alert(reason)
            except Exception as alert_error:
                LOGGER.error("告警发送失败：%s", alert_error)
        return StepResult(self.state, reason, make_market, risk.allowed)

    def run(
        self, *, allow_broadcast: bool = False,
        stop: Callable[[], bool] = lambda: False,
        max_iterations: int | None = None,
    ) -> None:
        """持续运行；只有显式布尔值 True 才把广播权限传给执行层。"""
        broadcast = require_broadcast_flag(allow_broadcast)
        if max_iterations is not None and max_iterations <= 0:
            raise ValueError("max_iterations 必须大于零")
        count = 0
        while not stop():
            result = self.step(allow_broadcast=broadcast)
            count += 1
            self.sleep(5 if result.should_make_market else 60)
            if max_iterations is not None and count >= max_iterations:
                return
