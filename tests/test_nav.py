import json
import tempfile
import unittest
from pathlib import Path

from okxlp.strategy.nav import NavRecorder, NavSnapshot


class NavRecorderTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def snapshot(ts, *, block=100):
        return NavSnapshot(
            ts=ts,
            block=block,
            price="1771.4431701141646",
            position_value_usdc="39.563977660512254",
            idle0_raw=11284654228689677,
            idle1_raw=19573854,
            total_usdc="79.127955321024508",
        )

    def test_throttles_second_snapshot_inside_interval(self):
        recorder = NavRecorder(self.root, min_interval_seconds=300)

        self.assertTrue(recorder.record(self.snapshot("2026-08-26T00:00:00Z")))
        self.assertFalse(
            recorder.record(self.snapshot("2026-08-26T00:04:59Z", block=101))
        )

        lines = (self.root / "nav_2026-08-26.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 1)

    def test_cross_day_writes_different_files_even_inside_interval(self):
        recorder = NavRecorder(self.root, min_interval_seconds=300)

        self.assertTrue(recorder.record(self.snapshot("2026-08-26T23:59:00Z")))
        self.assertTrue(
            recorder.record(self.snapshot("2026-08-27T00:01:00Z", block=101))
        )

        self.assertTrue((self.root / "nav_2026-08-26.jsonl").exists())
        self.assertTrue((self.root / "nav_2026-08-27.jsonl").exists())

    def test_written_json_contains_no_float(self):
        recorder = NavRecorder(self.root, min_interval_seconds=300)
        recorder.record(self.snapshot("2026-08-26T00:00:00+00:00"))

        payload = json.loads(
            (self.root / "nav_2026-08-26.jsonl").read_text(encoding="utf-8")
        )

        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertNotIsInstance(value, float)
                self.assertIn(type(value), (str, int))

    def test_snapshot_rejects_float_values(self):
        with self.assertRaisesRegex(TypeError, "price 必须是字符串"):
            NavSnapshot(
                ts="2026-08-26T00:00:00Z",
                block=100,
                price=1771.44,
                position_value_usdc="1",
                idle0_raw=0,
                idle1_raw=0,
                total_usdc="1",
            )


if __name__ == "__main__":
    unittest.main()
