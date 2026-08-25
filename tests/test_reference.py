import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okxlp.market.reference import NullReference, YahooFxAdrReference


UTC = timezone.utc
NOW = datetime(2026, 8, 25, 12, 24, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def yahoo_payload(price, observed_at=NOW):
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": price,
                            "regularMarketTime": int(observed_at.timestamp()),
                        }
                    }
                ]
            }
        }
    ).encode()


class ReferenceTest(unittest.TestCase):
    def test_multiplies_local_price_by_fx_with_user_agent(self):
        requests = []
        payloads = iter((yahoo_payload(1513.6), yahoo_payload(1.1666)))

        def urlopen(request, **_kwargs):
            requests.append(request)
            return FakeResponse(next(payloads))

        reference = YahooFxAdrReference("ASML.AS", "EURUSD=X", urlopen=urlopen)

        price = reference.get_price(NOW)

        self.assertEqual(price, Decimal("1765.76576"))
        self.assertEqual(len(requests), 2)
        self.assertIn("ASML.AS", requests[0].full_url)
        self.assertIn("EURUSD=X", requests[1].full_url)
        for request in requests:
            self.assertTrue(request.get_header("User-agent"))

    def test_caches_success_and_failure_until_ttl_expires(self):
        requests = []
        monotonic = [100.0]
        payloads = iter(
            (
                yahoo_payload(1513.6),
                yahoo_payload(1.1666),
                yahoo_payload(1514.0),
                yahoo_payload(1.1670),
            )
        )

        def urlopen(_request, **_kwargs):
            requests.append(True)
            return FakeResponse(next(payloads))

        reference = YahooFxAdrReference(
            "ASML.AS",
            "EURUSD=X",
            cache_ttl_seconds=60,
            urlopen=urlopen,
            monotonic=lambda: monotonic[0],
        )

        first = reference.get_price(NOW)
        second = reference.get_price(NOW)
        monotonic[0] += 61
        third = reference.get_price(NOW)

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(requests), 4)

        failed_calls = []

        def failing_urlopen(_request, **_kwargs):
            failed_calls.append(True)
            raise OSError("断网")

        failed = YahooFxAdrReference("ASML.AS", "EURUSD=X", urlopen=failing_urlopen)
        self.assertIsNone(failed.get_price(NOW))
        self.assertIsNone(failed.get_price(NOW))
        self.assertEqual(len(failed_calls), 1)

    def test_stale_or_invalid_yahoo_data_returns_none(self):
        stale = yahoo_payload(1513.6, NOW - timedelta(seconds=1801))
        invalid_cases = (
            (stale, yahoo_payload(1.1666)),
            (b"not-json", yahoo_payload(1.1666)),
            (json.dumps({"chart": {"result": []}}).encode(), yahoo_payload(1.1666)),
            (yahoo_payload(-1), yahoo_payload(1.1666)),
        )
        for payload_pair in invalid_cases:
            with self.subTest(payload_pair=payload_pair):
                payloads = iter(payload_pair)
                reference = YahooFxAdrReference(
                    "ASML.AS",
                    "EURUSD=X",
                    urlopen=lambda _request, **_kwargs: FakeResponse(next(payloads)),
                )
                self.assertIsNone(reference.get_price(NOW))

    def test_cache_does_not_outlive_quote_freshness(self):
        monotonic = [100.0]
        near_stale = NOW - timedelta(seconds=1790)
        payloads = iter(
            (
                yahoo_payload(1513.6, near_stale),
                yahoo_payload(1.1666, near_stale),
                yahoo_payload(1513.6, near_stale),
            )
        )
        calls = []

        def urlopen(_request, **_kwargs):
            calls.append(True)
            return FakeResponse(next(payloads))

        reference = YahooFxAdrReference(
            "ASML.AS", "EURUSD=X", urlopen=urlopen, monotonic=lambda: monotonic[0]
        )

        self.assertIsNotNone(reference.get_price(NOW))
        monotonic[0] += 11
        self.assertIsNone(reference.get_price(NOW + timedelta(seconds=11)))
        self.assertEqual(len(calls), 3)

    def test_null_reference_is_always_unavailable(self):
        self.assertIsNone(NullReference().get_price(NOW))


if __name__ == "__main__":
    unittest.main()
