import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.config import load_config
from okxlp.market.sessions import MarketSessions, should_make_market


UTC = timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
EMPTY_EVENTS = "events: []\n"


class SessionsTest(unittest.TestCase):
    def _scheduler(self, events=EMPTY_EVENTS, *, ignore_listings=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.yaml"
            path.write_text(events, encoding="utf-8")
            return MarketSessions.from_files(
                events_path=path, ignore_listings=ignore_listings
            )

    def _single_venue_scheduler(self, venue_index, *, ignore_listings=False):
        config = load_config(Path("config/pools.yaml"))
        pool = replace(config.pools[0], listings=(config.pools[0].listings[venue_index],))
        return MarketSessions(
            pool, config.fx_sunday_open, (), ignore_listings=ignore_listings
        )

    def test_ignore_listings_allows_amsterdam_open_with_warning_reason(self):
        scheduler = self._single_venue_scheduler(0, ignore_listings=True)

        allowed, reason = scheduler.should_make_market(
            datetime(2026, 8, 31, 7, 0, tzinfo=UTC)
        )

        self.assertTrue(allowed)
        self.assertIn("已停用时段闸门", reason)

    def test_ignore_listings_preserves_event_file_fail_safe(self):
        scheduler = MarketSessions.from_files(
            events_path=Path("不存在的-events.yaml"), ignore_listings=True
        )

        allowed, reason = scheduler.should_make_market(
            datetime(2026, 8, 31, 7, 0, tzinfo=UTC)
        )

        self.assertFalse(allowed)
        self.assertIn("事件文件不可用", reason)
        self.assertIn("已停用时段闸门", reason)

    def test_ignore_listings_preserves_earnings_window(self):
        scheduler = self._scheduler(
            "events:\n"
            "  - type: earnings\n"
            "    underlying: ASML\n"
            '    published_at: "2026-08-31T12:00:00Z"\n',
            ignore_listings=True,
        )

        allowed, reason = scheduler.should_make_market(
            datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        )

        self.assertFalse(allowed)
        self.assertIn("财报", reason)
        self.assertIn("已停用时段闸门", reason)

    def test_ignore_listings_preserves_fx_sunday_open_window(self):
        scheduler = self._scheduler(ignore_listings=True)

        allowed, reason = scheduler.should_make_market(
            datetime(2026, 8, 30, 17, 0, tzinfo=NEW_YORK)
        )

        self.assertFalse(allowed)
        self.assertIn("外汇周日开盘", reason)
        self.assertIn("已停用时段闸门", reason)

    def test_default_ignore_listings_false_preserves_listing_gate(self):
        scheduler = self._single_venue_scheduler(0)

        allowed, reason = scheduler.should_make_market(
            datetime(2026, 8, 31, 7, 0, tzinfo=UTC)
        )

        self.assertFalse(allowed)
        self.assertIn("Amsterdam", reason)

    def test_ignore_listings_requires_exact_bool(self):
        config = load_config(Path("config/pools.yaml"))

        for invalid in (1, "true", object()):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                MarketSessions(
                    config.pools[0], config.fx_sunday_open, (),
                    ignore_listings=invalid,
                )

    def test_us_dst_switch_uses_new_york_zone(self):
        scheduler = self._single_venue_scheduler(1)

        before, _ = scheduler.should_make_market(datetime(2026, 3, 9, 13, 29, tzinfo=UTC))
        at_open, reason = scheduler.should_make_market(datetime(2026, 3, 9, 13, 30, tzinfo=UTC))

        self.assertTrue(before)
        self.assertFalse(at_open)
        self.assertIn("NASDAQ", reason)

    def test_europe_stays_standard_time_after_us_switch(self):
        scheduler = self._single_venue_scheduler(0)

        before, _ = scheduler.should_make_market(datetime(2026, 3, 23, 7, 59, tzinfo=UTC))
        at_open, _ = scheduler.should_make_market(datetime(2026, 3, 23, 8, 0, tzinfo=UTC))

        self.assertTrue(before)
        self.assertFalse(at_open)

    def test_europe_dst_switch_uses_amsterdam_zone(self):
        scheduler = self._single_venue_scheduler(0)

        before, _ = scheduler.should_make_market(datetime(2026, 3, 30, 6, 59, tzinfo=UTC))
        at_open, reason = scheduler.should_make_market(datetime(2026, 3, 30, 7, 0, tzinfo=UTC))

        self.assertTrue(before)
        self.assertFalse(at_open)
        self.assertIn("Amsterdam", reason)

    def test_weekend_without_event_makes_market(self):
        scheduler = self._scheduler()

        allowed, reason = scheduler.should_make_market(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))

        self.assertTrue(allowed)
        self.assertIn("均休市", reason)

    def test_locked_beijing_examples(self):
        during_union, during_reason = should_make_market(
            datetime(2026, 8, 25, 20, 0, tzinfo=BEIJING)
        )
        after_both_close, after_reason = should_make_market(
            datetime(2026, 8, 26, 6, 0, tzinfo=BEIJING)
        )

        self.assertFalse(during_union)
        self.assertIn("Amsterdam", during_reason)
        self.assertTrue(after_both_close)
        self.assertIn("均休市", after_reason)

    def test_earnings_window_includes_four_hours_before_and_eighteen_after(self):
        scheduler = self._scheduler(
            "events:\n"
            "  - type: earnings\n"
            "    underlying: ASML\n"
            '    published_at: "2026-08-29T12:00:00Z"\n'
        )

        for current in (
            datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
        ):
            allowed, reason = scheduler.should_make_market(current)
            self.assertFalse(allowed)
            self.assertIn("财报", reason)

        before, _ = scheduler.should_make_market(datetime(2026, 8, 29, 7, 59, tzinfo=UTC))
        after, _ = scheduler.should_make_market(datetime(2026, 8, 30, 6, 1, tzinfo=UTC))
        self.assertTrue(before)
        self.assertTrue(after)

    def test_missing_event_file_fails_safe(self):
        scheduler = MarketSessions.from_files(events_path=Path("不存在的-events.yaml"))

        allowed, reason = scheduler.should_make_market(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))

        self.assertFalse(allowed)
        self.assertIn("事件文件不可用", reason)

    def test_invalid_event_file_fails_safe(self):
        for content in ("events: [", "events:\n  - type: earnings\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "events.yaml"
                path.write_text(content, encoding="utf-8")
                scheduler = MarketSessions.from_files(events_path=path)
                allowed, reason = scheduler.should_make_market(
                    datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
                )
                self.assertFalse(allowed)
                self.assertIn("按有事件处理", reason)

    def test_fx_sunday_open_uses_configured_window(self):
        scheduler = self._scheduler()

        cases = (
            (datetime(2026, 8, 30, 16, 29, tzinfo=NEW_YORK), True),
            (datetime(2026, 8, 30, 16, 30, tzinfo=NEW_YORK), False),
            (datetime(2026, 8, 30, 17, 30, tzinfo=NEW_YORK), False),
            (datetime(2026, 8, 30, 17, 31, tzinfo=NEW_YORK), True),
        )
        for current, expected in cases:
            with self.subTest(current=current):
                allowed, reason = scheduler.should_make_market(current)
                self.assertEqual(allowed, expected)
                if not expected:
                    self.assertIn("外汇周日开盘", reason)


if __name__ == "__main__":
    unittest.main()
