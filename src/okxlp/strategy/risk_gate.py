"""生产风控闸门与按 UTC 日期持久化的再平衡计数器。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okxlp.strategy.machine_types import RiskDecision


class RebalanceCounter:
    """以原子 JSON 文件记录单池当日已完成的再平衡次数。"""

    def __init__(
        self, path: Path = Path("log/rebalance_count.json")
    ) -> None:
        self.path = Path(path)

    def count(self, now: datetime) -> int:
        """返回当前 UTC 日期的计数，跨日时自动视为零。"""
        today = _utc_date(now)
        if not self.path.exists():
            return 0
        payload = self._read()
        return payload["count"] if payload["date"] == today else 0

    def record(self, now: datetime) -> int:
        """把当前 UTC 日期计数加一并原子落盘，返回新计数。"""
        today = _utc_date(now)
        count = self.count(now) + 1
        self._write({"date": today, "count": count})
        return count

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if type(payload) is not dict or set(payload) != {"date", "count"}:
                raise ValueError("根节点字段必须恰好为 date 与 count")
            date = payload["date"]
            count = payload["count"]
            if type(date) is not str:
                raise ValueError("date 必须是字符串")
            parsed = datetime.strptime(date, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != date:
                raise ValueError("date 必须是 YYYY-MM-DD")
            if type(count) is not int or count < 0:
                raise ValueError("count 必须是非负整数")
            return payload
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(
                f"再平衡计数文件非法 {self.path}：{error}"
            ) from None

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=".rebalance-count-", delete=False,
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
            raise ValueError(f"再平衡计数落盘失败：{error}") from None


class ProductionRiskGate:
    """依次检查人工急停、事实闸门与每日再平衡次数。"""

    def __init__(
        self, *, halt_file: Path, fact_gate: Any,
        counter: "RebalanceCounter", max_rebalances_per_day: int,
    ) -> None:
        if (
            not isinstance(counter, RebalanceCounter)
            or type(max_rebalances_per_day) is not int
            or max_rebalances_per_day <= 0
        ):
            raise ValueError("计数器必须有效，每日再平衡上限必须是正整数")
        self.halt_file = Path(halt_file)
        self.fact_gate = fact_gate
        self.counter = counter
        self.max_rebalances_per_day = max_rebalances_per_day

    def check(self, now: datetime) -> RiskDecision:
        """每轮重新读取全部闸门，HALT 时连自动撤出也禁止。"""
        if self.halt_file.exists():
            return RiskDecision(
                False,
                f"人工急停文件 {self.halt_file} 存在，完全冻结一切写链动作",
                allow_exit=False,
            )
        try:
            self.fact_gate.ensure_write_allowed()
        except PermissionError as error:
            return RiskDecision(
                False,
                f"事实闸门 live 级未核实：{error}",
                allow_exit=True,
            )
        count = self.counter.count(now)
        if count >= self.max_rebalances_per_day:
            return RiskDecision(
                False,
                f"当日已再平衡 {count} 次，达到上限 "
                f"{self.max_rebalances_per_day}",
                allow_exit=True,
            )
        return RiskDecision(
            True,
            f"风控放行：当日已再平衡 {count} 次，上限 "
            f"{self.max_rebalances_per_day}",
            allow_exit=False,
        )

    def record_rebalance(self, now: datetime) -> int:
        """记录一次已完成的 REBALANCING → IN_RANGE 转移。"""
        return self.counter.record(now)


def _utc_date(now: datetime) -> str:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("再平衡计数时间必须是带时区的 datetime")
    return now.astimezone(timezone.utc).date().isoformat()
