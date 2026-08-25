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


START = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class OutrangeTest(unittest.TestCase):
    def test_outside_before_confirmation_stays_pending(self):
        detector = OutrangeDetector(confirm_seconds=180, pin_timeout=600)

        first = detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), START)
        early = detector.evaluate(
            Decimal("102.1"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=179),
        )

        self.assertEqual(first.result, OutrangeResult.TIME_PENDING)
        self.assertEqual(early.state, OutrangeState.OUT_PENDING)
        self.assertEqual(early.pending_seconds, 179)

    def test_outside_at_confirmation_is_confirmed(self):
        detector = OutrangeDetector(confirm_seconds=180, pin_timeout=600)
        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), START)

        event = detector.evaluate(
            Decimal("102.1"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=180),
        )

        self.assertEqual(event.result, OutrangeResult.TIME_CONFIRMED)
        self.assertEqual(event.state, OutrangeState.CONFIRMED)
        self.assertEqual(event.direction, OutrangeDirection.ABOVE)
        self.assertEqual(event.triggered_at, START)
        self.assertEqual(event.pending_seconds, 180)

    def test_returning_inside_before_confirmation_resets_timer_and_state(self):
        detector = OutrangeDetector(confirm_seconds=180, pin_timeout=600)
        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), START)

        returned = detector.evaluate(
            Decimal("100"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=100),
        )
        restarted = detector.evaluate(
            Decimal("98"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=120),
        )

        self.assertEqual(returned.result, OutrangeResult.REVERTED)
        self.assertEqual(returned.state, OutrangeState.IN_RANGE)
        self.assertEqual(restarted.result, OutrangeResult.TIME_PENDING)
        self.assertEqual(restarted.direction, OutrangeDirection.BELOW)
        self.assertEqual(restarted.triggered_at, START + timedelta(seconds=120))
        self.assertEqual(restarted.pending_seconds, 0)

    def test_outside_at_timeout_is_confirmed_as_upper_bound(self):
        detector = OutrangeDetector(confirm_seconds=900, pin_timeout=600)
        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), START)

        event = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=600),
        )

        self.assertEqual(event.result, OutrangeResult.TIMEOUT_CONFIRMED)
        self.assertEqual(event.state, OutrangeState.CONFIRMED)
        self.assertEqual(event.pending_seconds, 600)

    def test_direction_change_preserves_first_outside_time_and_timeout(self):
        detector = OutrangeDetector(confirm_seconds=900, pin_timeout=600)
        detector.evaluate(Decimal("102"), Decimal("99"), Decimal("101"), START)

        changed = detector.evaluate(
            Decimal("98"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=100),
        )
        timed_out = detector.evaluate(
            Decimal("102"), Decimal("99"), Decimal("101"),
            START + timedelta(seconds=600),
        )

        self.assertEqual(changed.direction, OutrangeDirection.BELOW)
        self.assertEqual(changed.triggered_at, START)
        self.assertEqual(changed.pending_seconds, 100)
        self.assertEqual(timed_out.result, OutrangeResult.TIMEOUT_CONFIRMED)
        self.assertEqual(timed_out.triggered_at, START)


if __name__ == "__main__":
    unittest.main()
