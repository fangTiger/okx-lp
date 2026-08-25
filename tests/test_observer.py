import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.observer import Observer, build_record, load_pool_settings
from okxlp.chain.rpc import ChainIdMismatchError, RpcError
from okxlp.uniswap.pool import PoolSnapshot, TokenMetadata


def make_snapshot():
    return PoolSnapshot(
        block=68886709,
        address="0xc3d659028117f1ae5db9b9c68239b4a71f03ef37",
        factory="0x4b2ab38dbf28d31d467aa8993f6c2585981d6804",
        fee=500,
        tick_spacing=10,
        sqrt_price_x96=3333962123355733730549486,
        tick=-201526,
        active_liquidity=14942241291635132,
        token0=TokenMetadata("0x" + "11" * 20, "wASMLx", "Wrapped ASML xStock", 18, 56345412000000000000),
        token1=TokenMetadata("0x" + "22" * 20, "USDC", "USDC", 6, 144828242844),
    )


class FakePool:
    def __init__(self, values):
        self.values = iter(values)

    def snapshot(self):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class FakeStopEvent:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.clock.now += delay
        if self.clock.now > 300:
            self.stopped = True

    def set(self):
        self.stopped = True


class ObserverTest(unittest.TestCase):
    def test_loads_pool_and_rpc_from_yaml(self):
        settings = load_pool_settings(Path("config/pools.yaml"), "wASMLx_USDC")
        self.assertEqual(settings.chain_id, 196)
        self.assertEqual(settings.rpc_urls, ("https://rpc.xlayer.tech",))
        self.assertEqual(settings.address, "0xc3d659028117f1ae5db9b9c68239b4a71f03ef37")

    def test_builds_required_record_and_shares(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        record = build_record(make_snapshot(), now)

        self.assertEqual(
            set(record),
            {
                "ts",
                "block",
                "price",
                "tick",
                "active_liquidity",
                "range_lower",
                "range_upper",
                "share_at",
                "pool_balance_token0",
                "pool_balance_token1",
            },
        )
        self.assertEqual(record["ts"], "2026-08-25T12:00:00Z")
        self.assertEqual((record["range_lower"], record["range_upper"]), (-201580, -201470))
        self.assertEqual(set(record["share_at"]), {"50", "100", "500", "2000", "5000"})
        self.assertLess(record["share_at"]["50"], record["share_at"]["5000"])
        self.assertAlmostEqual(record["price"], 1770.77, places=6)

    def test_appends_one_json_object_per_line(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            observer = Observer(FakePool([make_snapshot(), make_snapshot()]), Path(directory))
            observer.observe_once(now)
            observer.observe_once(now)

            path = Path(directory) / "observer_2026-08-25.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["block"], 68886709)

    def test_network_failure_warns_and_next_poll_succeeds(self):
        now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            pool = FakePool([RpcError("网络离线"), make_snapshot()])
            with self.assertLogs("okxlp.observer", level=logging.WARNING) as captured:
                observer = Observer(pool, Path(directory))
                self.assertIsNone(observer.observe_once(now))
                self.assertIsNotNone(observer.observe_once(now))
            self.assertIn("本轮观测失败", captured.output[0])

    def test_wrong_chain_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = Observer(FakePool([ChainIdMismatchError("错误链")]), Path(directory))
            with self.assertRaises(ChainIdMismatchError):
                observer.observe_once()

    def test_log_write_failure_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            not_a_directory = Path(directory) / "普通文件"
            not_a_directory.write_text("占位", encoding="utf-8")
            observer = Observer(FakePool([make_snapshot()]), not_a_directory)
            with self.assertRaises(OSError):
                observer.observe_once()

    def test_run_polls_every_thirty_seconds_and_summarizes_at_five_minutes(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            observer = Observer(FakePool([make_snapshot()] * 11), Path(directory))
            observer.stop_event = FakeStopEvent(clock)
            with patch("okxlp.observer.time.monotonic", clock.monotonic), patch(
                "builtins.print"
            ) as printer:
                observer.run()

            lines = next(Path(directory).glob("observer_*.jsonl")).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 11)
            printer.assert_called_once()

    def test_defaults_to_thirty_second_poll_and_five_minute_summary(self):
        observer = Observer(FakePool([]), Path("log"))
        self.assertEqual(observer.poll_interval, 30.0)
        self.assertEqual(observer.summary_interval, 300.0)
        observer.stop()
        self.assertTrue(observer.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
