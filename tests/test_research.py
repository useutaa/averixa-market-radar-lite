"""Resumable discovery, fair monitoring, bounded storage and atomic snapshots."""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import radar_lite as radar
from research import (ResearchConfig, finish_search, monitoring_plan, prune_candidates,
                      record_run, remember_candidate, requests_in_window, search_task)
from tests.test_radar_lite import Response
from tests import test_radar_lite as existing_tests


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(radar.DEFAULT_CONFIG, keywords=("svg", "png"), search_requests_per_run=2)
        self.time = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).isoformat()

    def test_pagination_resumes_per_niche_and_newest_lane_stays_at_head(self):
        state = {}
        tasks = []
        for _ in range(8):
            task = search_task(state, self.config)
            tasks.append((task["keyword"], task["sort_on"], task["offset"]))
            finish_search(state, self.config, task, 100, 10000, self.time)
        self.assertEqual(tasks, [("svg", "score", 0), ("svg", "created", 0),
                                ("png", "score", 0), ("png", "created", 0),
                                ("svg", "score", 100), ("svg", "created", 0),
                                ("png", "score", 100), ("png", "created", 0)])
        self.assertEqual(state["query_cursor"], 0)

    def test_short_page_resets_just_its_own_cursor(self):
        state = {"queries": {"svg|score": {"next_page": 2}, "png|score": {"next_page": 8}}}
        task = search_task(state, self.config)
        finish_search(state, self.config, task, 10, 210, self.time)
        self.assertEqual(state["queries"]["svg|score"]["next_page"], 0)
        self.assertEqual(state["queries"]["png|score"]["next_page"], 8)

    def test_depth_wraps_at_configured_bound(self):
        state = {"queries": {"svg|score": {"next_page": 19}}}
        task = search_task(state, self.config)
        self.assertEqual(task["offset"], 1900)
        finish_search(state, self.config, task, 100, 100000, self.time)
        self.assertEqual(state["queries"]["svg|score"]["next_page"], 0)

    def test_candidate_dedup_tracks_multiple_discovery_queries_without_samples(self):
        state = {}
        row = {"listing_id": 1, "title": "SVG", "source_query": "svg", "views": 2}
        self.assertTrue(remember_candidate(state, row, self.time))
        self.assertFalse(remember_candidate(state, {**row, "source_query": "png", "views": 3}, self.time))
        self.assertEqual(state["candidates"]["1"]["discovery_queries"], ["svg", "png"])
        self.assertNotIn("samples", state["candidates"]["1"])
        self.assertEqual(state["candidates"]["1"]["views"], 3)

    def test_config_rejects_api_and_capacity_violations(self):
        for change in ({"search_page_size": 101}, {"max_search_pages": 122},
                       {"max_candidates": 10}, {"refresh_per_run": 5001},
                       {"new_tracked_per_run": 1000}, {"search_requests_per_run": True},
                       {"keywords": ("svg", "svg")}, {"history_samples": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(self.config, **change)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(radar.DEFAULT_CONFIG, keywords=("svg",), search_requests_per_run=1,
            max_candidates=8, max_tracked=6, refresh_per_run=3, priority_per_run=1, new_tracked_per_run=1)
        self.time = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.research = {"candidates": {}, "last_run": {}}

    def items(self):
        return {str(i): {"listing_id": i, "title": "SVG", "priority_score": 100 if i == 1 else 0,
                        "admitted_at": (self.time - timedelta(hours=48)).isoformat(),
                        "last_requested_at": (self.time - timedelta(hours=i)).isoformat()}
                for i in range(1, 7)}

    def test_strongest_is_frequent_and_others_are_oldest_first(self):
        items = self.items()
        first = monitoring_plan(items, self.research, self.config, self.time.isoformat())
        second = monitoring_plan(items, self.research, self.config, (self.time + timedelta(hours=1)).isoformat())
        third = monitoring_plan(items, self.research, self.config, (self.time + timedelta(hours=2)).isoformat())
        self.assertEqual(first, [1, 6, 5])
        self.assertEqual(second, [1, 4, 3])
        self.assertEqual(third, [1, 2, 5])
        self.assertEqual(set(first + second + third), set(range(1, 7)))

    def test_missing_response_still_advances_rotation_attempt(self):
        items = self.items()
        monitoring_plan(items, self.research, self.config, self.time.isoformat())
        self.assertEqual(items["6"]["last_requested_at"], self.time.isoformat())
        self.assertNotIn("last_seen_at", items["6"])

    def test_full_pool_can_admit_new_candidate_without_evicting_priority(self):
        items = self.items()
        remember_candidate(self.research, {"listing_id": 99, "title": "PNG", "source_query": "png"}, self.time.isoformat())
        for row in items.values():
            remember_candidate(self.research, row, self.time.isoformat())
        result = monitoring_plan(items, self.research, self.config, self.time.isoformat())
        self.assertIn(99, result)
        self.assertIn("1", items)
        self.assertEqual(len(items), 6)
        retired = set(range(1, 7)) - {int(key) for key in items}
        self.assertEqual(len(retired), 1)
        self.assertIn("previous_tracking", self.research["candidates"][str(retired.pop())])
        self.assertEqual(items["99"]["samples"], [])
        self.assertIsNone(items["99"]["last_seen_at"])

    def test_grace_period_keeps_newly_migrated_history(self):
        items = self.items()
        for row in items.values():
            row.pop("admitted_at")
            row["samples"] = [{"views": 8}]
        remember_candidate(self.research, {"listing_id": 99, "title": "PNG", "source_query": "png"}, self.time.isoformat())
        monitoring_plan(items, self.research, self.config, self.time.isoformat())
        self.assertEqual(set(items), set(map(str, range(1, 7))))
        self.assertTrue(all(row["samples"] == [{"views": 8}] for row in items.values()))

    def test_candidate_bound_prefers_retaining_tracked_rows(self):
        config = replace(self.config, max_candidates=6)
        for i in range(1, 10):
            remember_candidate(self.research, {"listing_id": i, "title": "SVG", "source_query": "svg"},
                               (self.time + timedelta(minutes=i)).isoformat())
        prune_candidates(self.research, {"1": {}}, config)
        self.assertEqual(len(self.research["candidates"]), 6)
        self.assertIn("1", self.research["candidates"])


class ExpandedCollectorTests(unittest.TestCase):
    @patch("radar_lite.time.sleep")
    @patch("radar_lite.urlopen")
    def test_full_5000_pool_refreshes_2000_with_30_calls_and_keeps_discovering(self, urlopen, sleep):
        captured = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).isoformat()
        items = {str(i): {"listing_id": i, "title": f"SVG {i}", "views": 100,
                         "last_seen_at": captured, "source_query": "svg bundle"} for i in range(1, 5001)}
        research = {}
        search_number = 0
        def respond(request, **kwargs):
            nonlocal search_number
            params = parse_qs(urlsplit(request.full_url).query)
            if "/active?" in request.full_url:
                search_number += 1
                rows = [{"listing_id": 10000 + search_number * 100 + i, "title": f"SVG new {i}",
                         "type": "download", "views": 100} for i in range(100)]
                return Response({"count": 100000, "results": rows}, {"x-limit-per-day": "5000", "x-remaining-today": "4000"})
            rows = [{"listing_id": int(i), "title": "SVG", "type": "download", "views": 110}
                    for i in params["listing_ids"][0].split(",")]
            return Response({"results": rows})
        urlopen.side_effect = respond
        budget = radar.RequestBudget()
        result = radar.collect("dummy", "dummy", budget=budget, tracked_items=items,
                               research_state=research, captured_at=captured)
        self.assertEqual(len(result), 2000)
        self.assertEqual(len(items), 5000)
        self.assertEqual(len(research["candidates"]), 6000)
        self.assertEqual(research["last_run"]["new_candidates"], 1000)
        self.assertEqual(budget.requests, 30)
        self.assertTrue(all(row["views_source"] == "listing_batch" for row in result))

    def test_rolling_window_retains_manual_runs_and_expires_old_usage(self):
        moment = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        state = {}
        record_run(state, moment - timedelta(hours=25), 30)
        record_run(state, moment - timedelta(hours=1), 30)
        record_run(state, moment, 20)
        self.assertEqual(requests_in_window(state, moment), 50)
        self.assertEqual(len(state["runs"]), 2)


class ExpandedSnapshotTests(unittest.TestCase):
    setUp = existing_tests.SnapshotTests.setUp
    invoke = existing_tests.SnapshotTests.invoke

    def test_sales_corroboration_does_not_mix_different_rotation_windows(self):
        second = copy.deepcopy(self.original["items"]["1"])
        second["listing_id"] = 2
        second["samples"][0].update({"captured_at": (self.fixed - timedelta(hours=4)).isoformat(), "shop_sales": 102})
        self.original["items"]["2"] = second
        observations = [{**row, "quantity": 9, "views": 110, "shop_sales": 102}
                        for row in self.original["items"].values()]
        result, status = self.invoke(self.original, lambda *args, **kwargs: observations)
        self.assertEqual(result["items"]["1"]["confirmed_sales_24h"], 1)
        self.assertEqual(result["items"]["2"]["confirmed_sales_24h"], 0)
    def test_partial_discovery_quota_pause_rolls_back_cursors_candidates_and_admissions(self):
        self.original["research"] = {"query_cursor": 2, "candidates": {}, "queries": {}}
        def collect(*args, **kwargs):
            kwargs["research_state"].update({"query_cursor": 10, "candidates": {"999": {"title": "uncommitted"}}})
            kwargs["tracked_items"]["999"] = {"listing_id": 999}
            kwargs["budget"].before_request()
            raise radar.QuotaDeferred("test pause")
        result, status = self.invoke(self.original, collect)
        self.assertEqual(result["items"], self.original["items"])
        self.assertEqual(result["research"]["query_cursor"], 2)
        self.assertEqual(result["research"]["candidates"], {})
        self.assertEqual(status["api_usage"]["rolling_daily_requests"], 1)

    def test_local_daily_budget_stops_before_any_network_call(self):
        self.original["research"] = {"runs": [{"captured_at": self.fixed.isoformat(), "requests": 980}]}
        def collect(*args, **kwargs):
            self.fail("Local rolling budget must stop before discovery")
        result, status = self.invoke(self.original, collect)
        self.assertEqual(status["api_usage"]["requests"], 0)
        self.assertEqual(status["collection_status"], "quota_deferred")

    def test_network_failure_preserves_baseline_and_counts_spent_request(self):
        def collect(*args, **kwargs):
            kwargs["budget"].before_request()
            kwargs["research_state"]["query_cursor"] = 99
            raise radar.RadarError("Network failed")
        result, status = self.invoke(self.original, collect)
        self.assertEqual(result["items"], self.original["items"])
        self.assertNotIn("query_cursor", result["research"])
        self.assertEqual(status["collection_status"], "api_error")
        self.assertEqual(status["api_usage"]["rolling_daily_requests"], 1)

    def test_public_export_omits_histories_and_separates_candidate_counts(self):
        self.original["research"] = {"candidates": {"1": {"listing_id": 1}, "9": {"listing_id": 9}}}
        result, status = self.invoke(self.original, lambda *args, **kwargs: [])
        self.assertEqual(status["candidates"], 2)
        self.assertEqual(status["listings"], 1)
        self.assertEqual(status["capacities"]["tracked"], 5000)


if __name__ == "__main__":
    unittest.main()
