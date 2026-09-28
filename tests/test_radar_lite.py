"""Quota and complete-snapshot regression tests; no live credentials needed."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import radar_lite as radar


class Response:
    def __init__(self, data: dict, headers: dict | None = None) -> None:
        self.data = data
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.data).encode("utf-8")


class QuotaTests(unittest.TestCase):
    def test_live_headers_and_daily_reserve(self):
        budget = radar.RequestBudget()
        budget.record({"X-Limit-Per-Day": "1000", "x-remaining-today": "900",
                       "X-Limit-Per-Second": "1", "x-remaining-this-secon": "0"})
        self.assertEqual(budget.daily_limit, 1000)
        self.assertEqual(budget.reserve, 100)
        self.assertEqual(budget.remaining_today, 900)
        self.assertEqual(budget.second_limit, 1)
        self.assertEqual(budget.remaining_second, 0)
        budget.record({"x-remaining-this-second": "1"})
        self.assertEqual(budget.remaining_second, 1)

    def test_unknown_quota_is_not_fabricated(self):
        budget = radar.RequestBudget()
        budget.record({"x-limit-per-day": "invalid", "x-remaining-today": "-1"})
        self.assertIsNone(budget.daily_limit)
        self.assertIsNone(budget.remaining_today)
        self.assertEqual(budget.reserve, 25)

    def test_guard_reserves_room_for_entire_remaining_run(self):
        budget = radar.RequestBudget()
        budget.record({"x-limit-per-day": "1000", "x-remaining-today": "107"})
        with self.assertRaises(radar.QuotaDeferred):
            budget.before_request()
        self.assertEqual(budget.requests, 0)

    @patch("radar_lite.time.monotonic", side_effect=[10.0, 10.4, 11.1])
    @patch("radar_lite.time.sleep")
    def test_requests_are_spaced(self, sleep, monotonic):
        budget = radar.RequestBudget()
        budget.before_request()
        budget.before_request()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.7)
        self.assertEqual(budget.requests, 2)

    @patch("radar_lite.time.sleep")
    @patch("radar_lite.urlopen")
    def test_request_cap_applies_even_without_headers(self, urlopen, sleep):
        urlopen.side_effect = [Response({}), Response({})]
        budget = radar.RequestBudget(max_requests=2)
        radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        with self.assertRaises(radar.QuotaDeferred):
            radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        self.assertEqual(urlopen.call_count, 2)
        self.assertNotIn("dummy-secret", json.dumps(budget.summary()))

    @patch("radar_lite.urlopen")
    def test_low_live_quota_blocks_next_call(self, urlopen):
        urlopen.return_value = Response({}, {"x-limit-per-day": "100", "x-remaining-today": "26"})
        budget = radar.RequestBudget()
        radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        with self.assertRaises(radar.QuotaDeferred):
            radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        self.assertEqual(urlopen.call_count, 1)

    @patch("radar_lite.time.sleep")
    def test_missing_subsequent_headers_decrement_known_quota(self, sleep):
        budget = radar.RequestBudget()
        budget.record({"x-limit-per-day": "1000", "x-remaining-today": "108"})
        budget.before_request()
        budget.record({})
        self.assertEqual(budget.remaining_today, 107)
        budget.before_request()
        self.assertEqual(budget.remaining_today, 106)

    @patch("radar_lite.urlopen")
    def test_429_stores_retry_after_without_retrying(self, urlopen):
        fixed = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        urlopen.side_effect = HTTPError("https://api.etsy.com/", 429, "rate limit",
                                       {"Retry-After": "7200", "x-remaining-today": "0"}, None)
        budget = radar.RequestBudget()
        with patch("radar_lite.now", return_value=fixed), self.assertRaises(radar.QuotaDeferred):
            radar.get("/listings/active", "dummy-key", "dummy-secret", budget=budget)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(budget.retry_at, (fixed + timedelta(hours=2)).isoformat())
        self.assertEqual(budget.remaining_today, 0)

    @patch("radar_lite.time.sleep")
    @patch("radar_lite.urlopen")
    def test_full_search_collection_uses_six_calls(self, urlopen, sleep):
        rows = [{"listing_id": i, "listing_type": "download", "title": f"SVG {i}"}
                for i in range(1, 201)]
        pages = [rows[i:i + 50] for i in range(0, 200, 50)]
        pages += [rows[:100], rows[100:]]
        urlopen.side_effect = [Response({"results": page}, {"x-limit-per-day": "10000",
                              "x-remaining-today": str(9000 - i)}) for i, page in enumerate(pages)]
        budget = radar.RequestBudget()
        observations = radar.collect("dummy-key", "dummy-secret", budget=budget)
        self.assertEqual(len(observations), 200)
        self.assertEqual(budget.requests, 6)
        self.assertEqual(urlopen.call_count, 6)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixed = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.captured_at = (self.fixed - timedelta(hours=24)).isoformat()
        self.original = {
            "version": 1, "last_success_at": self.captured_at,
            "items": {"1": {
                "listing_id": 1, "title": "SVG sample", "source_query": "svg bundle",
                "shop_id": 5, "quantity": 10, "views": 100, "favorites": 3, "shop_sales": 100,
                "last_seen_at": self.captured_at,
                "samples": [{"captured_at": self.captured_at, "quantity": 10, "views": 100,
                             "favorites": 3, "shop_sales": 100}],
            }},
        }

    def invoke(self, state, collect):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            site = Path(directory) / "site"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            argv = ["radar_lite.py", "--state", str(state_path), "--site", str(site)]
            with patch("sys.argv", argv), patch.dict("os.environ", {
                "ETSY_KEYSTRING": "dummy-key", "ETSY_SHARED_SECRET": "dummy-secret"
            }), patch("radar_lite.now", return_value=self.fixed), patch("radar_lite.collect", collect), contextlib.redirect_stdout(io.StringIO()):
                radar.main()
            result = json.loads(state_path.read_text(encoding="utf-8"))
            status = json.loads((site / "data/status.json").read_text(encoding="utf-8"))
            self.assertTrue((site / "index.html").exists())
            self.assertNotIn("dummy-secret", json.dumps(result) + json.dumps(status))
            return result, status

    def test_quota_abort_preserves_complete_snapshot_and_timestamp(self):
        def collect(*args, **kwargs):
            kwargs["budget"].record({"x-limit-per-day": "1000", "x-remaining-today": "50"})
            raise radar.QuotaDeferred("Daily reserve reached.")
        result, status = self.invoke(self.original, collect)
        self.assertEqual(result["items"], self.original["items"])
        self.assertEqual(result["last_success_at"], self.captured_at)
        self.assertEqual(status["generated_at"], self.captured_at)
        self.assertEqual(status["collection_status"], "quota_deferred")
        self.assertEqual(status["api_usage"]["remaining_today"], 50)

    def test_unexpired_retry_after_makes_zero_api_calls(self):
        self.original["api_usage"] = {"retry_at": (self.fixed + timedelta(hours=2)).isoformat()}
        def collect(*args, **kwargs):
            self.fail("A stored quota pause must not call Etsy.")
        result, status = self.invoke(self.original, collect)
        self.assertEqual(result["items"], self.original["items"])
        self.assertEqual(status["api_usage"]["requests"], 0)
        self.assertEqual(status["collection_status"], "quota_deferred")

    def test_successful_snapshot_retains_24_hour_view_and_sales_metrics(self):
        observation = {**self.original["items"]["1"], "quantity": 7, "views": 110, "shop_sales": 103}
        def collect(*args, **kwargs):
            kwargs["budget"].record({"x-limit-per-day": "10000", "x-remaining-today": "9994"})
            return [observation]
        result, status = self.invoke(self.original, collect)
        item = result["items"]["1"]
        self.assertEqual(item["views_24h"], 10)
        self.assertEqual(item["confirmed_sales_24h"], 3)
        self.assertEqual(item["confirmed_sales_view_24h"], 30)
        self.assertEqual(result["last_success_at"], self.fixed.isoformat())
        self.assertEqual(status["collection_status"], "ok")


if __name__ == "__main__":
    unittest.main()
