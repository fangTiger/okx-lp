"""基差 EWMA 的可信初始化与更新。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class BasisEwma:
    """连续三个相近样本建立基线，异常样本不进入正式均值。"""

    threshold: Decimal
    alpha: Decimal
    min_interval_seconds: int = 60
    max_gap_seconds: int = 180
    required_samples: int = 3
    value: Decimal | None = None
    _candidate: Decimal | None = None
    _candidate_count: int = 0
    _candidate_at: datetime | None = None

    def observe(self, basis: Decimal, observed_at: datetime) -> Decimal | None:
        """吸收可信样本并返回当前均值。"""
        if self.value is not None and abs(basis - self.value) <= self.threshold:
            self.value += self.alpha * (basis - self.value)
            self._clear_candidate()
            return self.value
        if self._candidate is None:
            self._start_candidate(basis, observed_at)
            return self.value
        elapsed = (observed_at - self._candidate_at).total_seconds()
        if elapsed < 0 or elapsed > self.max_gap_seconds or abs(basis - self._candidate) > self.threshold:
            self._start_candidate(basis, observed_at)
            return self.value
        if elapsed < self.min_interval_seconds:
            return self.value
        self._candidate += self.alpha * (basis - self._candidate)
        self._candidate_count += 1
        self._candidate_at = observed_at
        if self._candidate_count >= self.required_samples:
            self.value = self._candidate
            self._clear_candidate()
        return self.value

    def _start_candidate(self, basis: Decimal, observed_at: datetime) -> None:
        self._candidate = basis
        self._candidate_count = 1
        self._candidate_at = observed_at

    def _clear_candidate(self) -> None:
        self._candidate = None
        self._candidate_count = 0
        self._candidate_at = None
