import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.strategy.outrange import (
    OutrangeDetector,
    OutrangeDirection,
    OutrangeResult,
    OutrangeState,
)


UTC = timezone.utc
START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
BASIS = Decimal("0.0032")


def pool_at_basis(fair_price):
    return Decimal(fair_price) * (Decimal("1") + BASIS)


class OutrangeTest(unittest.TestCase):
    def _detector(self):
        return OutrangeDetector(
            basis_jump_threshold=Decimal("0.004"),
            confirm_seconds=180,
            pin_timeout=600,
        )

    def _warm(self, detector):
        for seconds in (-120, -60, 0):
            detector.evaluate(
                pool_at_basis("100"), Decimal("99"), Decimal("101"),
                Decimal("100"), START + timedelta(seconds=seconds),
            )

    def test_real_one_way_move_is_confirmed_immediately(self):
        detector = self._detector()
        self._warm(detector)

        event = detector.evaluate(
            pool_at_basis("102"), Decimal("99"), Decimal("101"), Decimal("102"), START + timedelta(seconds=5)
        )

        self.assertEqual(detector.state, OutrangeState.CONFIRMED)
        self.assertEqual(event.result, OutrangeResult.TRUE_MOVE)
        self.assertEqual(event.direction, OutrangeDirection.ABOVE)
        self.assertEqual(event.basis, BASIS)
        self.assertEqual(event.basis_ewma, BASIS)
        self.assertEqual(event.pending_seconds, 0)
        self.assertIn("基差相对均值未突变", event.reason)

    def test_stable_positive_basis_is_not_misclassified_as_pin(self):
        detector = self._detector()
        for offset, fair in enumerate(("99.7", "99.9", "100", "100.1", "100.3")):
            detector.evaluate(
                pool_at_basis(fair), Decimal("95"), Decimal("105"),
                Decimal(fair), START + timedelta(seconds=offset * 60),
            )

        event = detector.evaluate(
            pool_at_basis("106"), Decimal("95"), Decimal("105"),
            Decimal("106"), START + timedelta(seconds=300),
        )

        self.assertEqual(event.result, OutrangeResult.TRUE_MOVE)
        self.assertEqual(event.basis, BASIS)
        self.assertEqual(event.basis_ewma, BASIS)
        self.assertNotIn("插针", event.reason)

    def test_pin_is_held_then_recorded_when_price_returns(self):
        detector = self._detector()
        self._warm(detector)

        pending = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), Decimal("100"), START + timedelta(seconds=5)
        )
        detector.evaluate(
            Decimal("102.1"), Decimal("99"), Decimal("101"),
            Decimal("100"), START + timedelta(seconds=8),
        )
        returned = detector.evaluate(
            pool_at_basis("100"), Decimal("99"), Decimal("101"), Decimal("100"), START + timedelta(seconds=12)
        )

        self.assertEqual(pending.state, OutrangeState.OUT_PENDING)
        self.assertEqual(pending.result, OutrangeResult.PIN_PENDING)
        self.assertEqual(pending.basis_ewma, BASIS)
        self.assertEqual(detector.basis_ewma, BASIS)
        self.assertEqual(returned.state, OutrangeState.IN_RANGE)
        self.assertEqual(returned.result, OutrangeResult.REVERTED)
        self.assertEqual(returned.triggered_at, START + timedelta(seconds=5))
        self.assertEqual(returned.pending_seconds, 7)
        self.assertIn("回到区间", returned.reason)

    def test_unavailable_reference_requires_continuous_time_confirmation(self):
        detector = self._detector()

        first = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), None, START
        )
        early = detector.evaluate(
            Decimal("102.1"), Decimal("99"), Decimal("101"), None, START + timedelta(seconds=179)
        )
        confirmed = detector.evaluate(
            Decimal("102.2"), Decimal("99"), Decimal("101"), None, START + timedelta(seconds=180)
        )

        self.assertEqual(first.result, OutrangeResult.TIME_PENDING)
        self.assertEqual(early.result, OutrangeResult.TIME_PENDING)
        self.assertEqual(confirmed.result, OutrangeResult.TIME_CONFIRMED)
        self.assertEqual(confirmed.state, OutrangeState.CONFIRMED)
        self.assertIsNone(confirmed.reference_price)
        self.assertIsNone(confirmed.basis)
        self.assertIsNone(confirmed.basis_ewma)
        self.assertEqual(confirmed.pending_seconds, 180)
        self.assertIn("参考价不可用", confirmed.reason)

    def test_returning_in_range_restarts_unavailable_reference_timer(self):
        detector = self._detector()

        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), None, START)
        detector.evaluate(
            Decimal("100"), Decimal("99"), Decimal("101"), None,
            START + timedelta(seconds=100),
        )
        detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), None,
            START + timedelta(seconds=120),
        )
        still_pending = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), None,
            START + timedelta(seconds=180),
        )

        self.assertEqual(still_pending.result, OutrangeResult.TIME_PENDING)
        self.assertEqual(still_pending.pending_seconds, 60)

    def test_bootstrap_rejects_anomalous_first_basis_and_recovers(self):
        detector = self._detector()

        for seconds in (0, 5):
            detector.evaluate(
                Decimal("200"), Decimal("150"), Decimal("250"),
                Decimal("100"), START + timedelta(seconds=seconds),
            )
        self.assertIsNone(detector.basis_ewma)
        for seconds in (60, 120, 180):
            detector.evaluate(
                pool_at_basis("100"), Decimal("99"), Decimal("101"),
                Decimal("100"), START + timedelta(seconds=seconds),
            )

        event = detector.evaluate(
            pool_at_basis("102"), Decimal("99"), Decimal("101"),
            Decimal("102"), START + timedelta(seconds=181),
        )

        self.assertEqual(detector.basis_ewma, BASIS)
        self.assertEqual(event.result, OutrangeResult.TRUE_MOVE)

    def test_established_bad_baseline_recovers_from_consistent_new_basis(self):
        detector = self._detector()
        for seconds in (0, 60, 120):
            detector.evaluate(
                Decimal("200"), Decimal("150"), Decimal("250"),
                Decimal("100"), START + timedelta(seconds=seconds),
            )
        self.assertEqual(detector.basis_ewma, Decimal("1"))

        for seconds in (180, 240, 300):
            detector.evaluate(
                pool_at_basis("100"), Decimal("99"), Decimal("101"),
                Decimal("100"), START + timedelta(seconds=seconds),
            )

        self.assertEqual(detector.basis_ewma, BASIS)

    def test_bootstrap_candidate_expires_after_long_gap(self):
        detector = self._detector()

        detector.evaluate(
            pool_at_basis("100"), Decimal("99"), Decimal("101"), Decimal("100"), START
        )
        detector.evaluate(
            pool_at_basis("100"), Decimal("99"), Decimal("101"),
            Decimal("100"), START + timedelta(seconds=181),
        )

        self.assertIsNone(detector.basis_ewma)

    def test_available_reference_without_ewma_waits_for_pin_timeout(self):
        detector = self._detector()

        first = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), Decimal("100"), START
        )
        after_fallback_time = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"),
            Decimal("100"), START + timedelta(seconds=180),
        )
        timed_out = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"),
            Decimal("100"), START + timedelta(seconds=600),
        )

        self.assertEqual(first.result, OutrangeResult.BASELINE_PENDING)
        self.assertEqual(after_fallback_time.result, OutrangeResult.BASELINE_PENDING)
        self.assertNotIn("参考价不可用", after_fallback_time.reason)
        self.assertEqual(timed_out.result, OutrangeResult.TIMEOUT_CONFIRMED)
        self.assertNotIn("插针", timed_out.reason)

    def test_pin_timeout_forces_confirmation(self):
        detector = self._detector()
        self._warm(detector)

        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), Decimal("100"), START)
        event = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"), Decimal("100"), START + timedelta(seconds=600)
        )

        self.assertEqual(event.result, OutrangeResult.TIMEOUT_CONFIRMED)
        self.assertEqual(event.state, OutrangeState.CONFIRMED)
        self.assertEqual(event.triggered_at, START)
        self.assertEqual(event.pool_price, Decimal("102"))
        self.assertEqual(event.reference_price, Decimal("100"))
        self.assertEqual(event.basis, Decimal("0.02"))
        self.assertEqual(event.basis_ewma, BASIS)
        self.assertEqual(event.pending_seconds, 600)
        self.assertIn("挂起超时", event.reason)


if __name__ == "__main__":
    unittest.main()
