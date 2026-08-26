import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from okxlp.strategy.risk_gate import ProductionRiskGate, RebalanceCounter


class FakeFactGate:
    def __init__(self, error=None):
        self.error = error

    def ensure_write_allowed(self):
        if self.error is not None:
            raise self.error


class ProductionRiskGateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.halt_file = self.root / "HALT"
        self.counter = RebalanceCounter(self.root / "count.json")
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def gate(self, *, fact_gate=None, limit=30):
        return ProductionRiskGate(
            halt_file=self.halt_file,
            fact_gate=fact_gate or FakeFactGate(),
            counter=self.counter,
            max_rebalances_per_day=limit,
        )

    def test_halt_completely_freezes_writes_including_exit(self):
        self.halt_file.touch()

        decision = self.gate().check(self.now)

        self.assertFalse(decision.allowed)
        self.assertIs(decision.allow_exit, False)
        self.assertIn("人工急停", decision.reason)
        self.assertIn(str(self.halt_file), decision.reason)

    def test_unverified_live_fact_blocks_entry_but_allows_exit(self):
        gate = self.gate(
            fact_gate=FakeFactGate(PermissionError("事实项 F8 尚未核实"))
        )

        decision = gate.check(self.now)

        self.assertFalse(decision.allowed)
        self.assertIs(decision.allow_exit, True)
        self.assertIn("事实", decision.reason)
        self.assertIn("F8", decision.reason)

    def test_daily_limit_blocks_entry_but_allows_exit(self):
        gate = self.gate(limit=2)
        gate.record_rebalance(self.now)
        gate.record_rebalance(self.now)

        decision = gate.check(self.now)

        self.assertFalse(decision.allowed)
        self.assertIs(decision.allow_exit, True)
        self.assertEqual(decision.reason, "当日已再平衡 2 次，达到上限 2")

    def test_checks_pass(self):
        decision = self.gate(limit=2).check(self.now)

        self.assertTrue(decision.allowed)
        self.assertIs(decision.allow_exit, False)
        self.assertIn("当日已再平衡 0 次", decision.reason)
        self.assertIn("上限 2", decision.reason)

    def test_halt_file_is_read_on_every_check(self):
        gate = self.gate()
        self.assertTrue(gate.check(self.now).allowed)

        self.halt_file.touch()
        self.assertFalse(gate.check(self.now).allowed)

        self.halt_file.unlink()
        self.assertTrue(gate.check(self.now).allowed)


class RebalanceCounterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "nested" / "count.json"
        self.counter = RebalanceCounter(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_rolls_over_at_utc_date_boundary(self):
        before = datetime(2026, 8, 26, 23, 59, tzinfo=timezone.utc)
        after = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(self.counter.record(before), 1)
        self.assertEqual(self.counter.count(before), 1)
        self.assertEqual(self.counter.count(after), 0)
        self.assertEqual(self.counter.record(after), 1)

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"date": "2026-08-27", "count": 1},
        )

    def test_corrupt_or_invalid_file_fails_closed(self):
        self.path.parent.mkdir(parents=True)
        invalid_contents = (
            "not json",
            '{"date":"2026-08-26","count":true}',
            '{"date":"not-a-date","count":1}',
            '{"date":"2026-08-26","count":-1}',
        )
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for content in invalid_contents:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "再平衡计数文件非法"):
                    self.counter.count(now)

    def test_atomic_record_can_be_read_back(self):
        now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)

        self.assertEqual(self.counter.record(now), 1)
        self.assertEqual(self.counter.record(now), 2)

        reloaded = RebalanceCounter(self.path)
        self.assertEqual(reloaded.count(now), 2)
        self.assertEqual(list(self.path.parent.glob(".rebalance-count-*")), [])


if __name__ == "__main__":
    unittest.main()
