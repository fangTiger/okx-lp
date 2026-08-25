import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.strategy.machine_journal import TransitionJournal, TransitionRecord
from okxlp.strategy.machine_state import (
    MachineSnapshot,
    MachineState,
    MachineStateStore,
    PriceBand,
    StatePersistenceError,
)


UTC = timezone.utc


class MachineStateTest(unittest.TestCase):
    def test_missing_state_starts_idle_and_saved_range_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MachineStateStore(Path(directory) / "state.json")
            self.assertEqual(store.load(), MachineSnapshot(MachineState.IDLE))
            band = PriceBand(-50, 50, Decimal("0.995"), Decimal("1.006"))

            store.save(MachineSnapshot(MachineState.IN_RANGE, band))
            restored = MachineStateStore(store.path).load()

        self.assertEqual(restored, MachineSnapshot(MachineState.IN_RANGE, band))

    def test_out_pending_time_and_direction_are_restored(self):
        at = datetime(2026, 8, 26, 1, 2, 3, tzinfo=UTC)
        band = PriceBand(-50, 50, Decimal("0.995"), Decimal("1.006"))
        pending = MachineSnapshot(MachineState.OUT_PENDING, band, at, "ABOVE")
        with tempfile.TemporaryDirectory() as directory:
            store = MachineStateStore(Path(directory) / "state.json")

            store.save(pending)

            self.assertEqual(MachineStateStore(store.path).load(), pending)

    def test_corrupt_or_unknown_state_is_rejected_instead_of_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"state":"UNKNOWN","band":null}', encoding="utf-8")

            with self.assertRaisesRegex(StatePersistenceError, "状态文件非法"):
                MachineStateStore(path).load()

    def test_non_finite_price_or_boolean_tick_is_rejected(self):
        invalid_bands = (
            '{"tick_lower":true,"tick_upper":50,"price_lower":"0.995","price_upper":"1.006"}',
            '{"tick_lower":-50,"tick_upper":50,"price_lower":"NaN","price_upper":"1.006"}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for band in invalid_bands:
                with self.subTest(band=band):
                    path.write_text(
                        f'{{"state":"IN_RANGE","band":{band}}}', encoding="utf-8"
                    )
                    with self.assertRaisesRegex(StatePersistenceError, "状态文件非法"):
                        MachineStateStore(path).load()

    def test_transition_log_contains_required_structured_fields(self):
        at = datetime(2026, 8, 26, 1, 2, 3, tzinfo=UTC)
        band = PriceBand(-50, 50, Decimal("0.995"), Decimal("1.006"))
        record = TransitionRecord(
            at, "pool-1", MachineState.IDLE, MachineState.ENTERING,
            "做市条件满足", Decimal("1.001"), 10, band,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.log"
            TransitionJournal(path).append(record)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["timestamp"], "2026-08-26T01:02:03Z")
        self.assertEqual(saved["old_state"], "IDLE")
        self.assertEqual(saved["new_state"], "ENTERING")
        self.assertEqual(saved["reason"], "做市条件满足")
        self.assertEqual(saved["pool_price"], "1.001")
        self.assertEqual(saved["range"]["tick_lower"], -50)


if __name__ == "__main__":
    unittest.main()
