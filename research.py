"""Bounded discovery and monitoring plans; pure Python, no credentials or I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class ResearchConfig:
    keywords: tuple[str, ...]
    search_requests_per_run: int = 10
    search_page_size: int = 100
    max_search_pages: int = 20
    max_candidates: int = 20000
    max_tracked: int = 5000
    refresh_per_run: int = 2000
    priority_per_run: int = 1000
    new_tracked_per_run: int = 500
    minimum_tracking_hours: int = 36
    history_samples: int = 48
    max_daily_requests: int = 1000

    def __post_init__(self) -> None:
        if not self.keywords or any(not isinstance(word, str) or not word.strip() for word in self.keywords) or len(set(self.keywords)) != len(self.keywords):
            raise ValueError("keywords must contain unique, non-empty search phrases")
        for name in self.__dataclass_fields__:
            if name != "keywords" and (type(getattr(self, name)) is not int or getattr(self, name) <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if self.search_page_size > 100 or (self.max_search_pages - 1) * self.search_page_size > 12000:
            raise ValueError("Etsy search permits at most 100 results and offset 12000")
        if self.max_candidates < self.max_tracked or self.refresh_per_run > self.max_tracked:
            raise ValueError("candidate >= tracked >= per-run refresh capacities are required")
        if self.priority_per_run + self.new_tracked_per_run >= self.refresh_per_run:
            raise ValueError("refresh capacity must leave room for rotating non-priority listings")
        if self.history_samples < 26 or self.max_daily_requests < self.max_requests_per_run:
            raise ValueError("history or daily request capacity is too small")

    @property
    def max_requests_per_run(self) -> int:
        return self.search_requests_per_run + math.ceil(self.refresh_per_run / 100)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResearchConfig:
        return cls(**{**value, "keywords": tuple(value["keywords"])})


def timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def score(item: dict[str, Any]) -> float:
    """Last genuinely measured momentum plus a small public popularity prior."""
    return float(item.get("priority_score") or item.get("hot_score") or 0) + math.log1p(max(float(item.get("views") or 0), 0))


def search_task(research: dict[str, Any], config: ResearchConfig) -> dict[str, Any]:
    # Each niche has a paginated relevance lane and a newest-first head lane.
    # A short/empty final page resets only that niche, not every other cursor.
    cursor = int(research.get("query_cursor") or 0) % (len(config.keywords) * 2)
    keyword, lane = config.keywords[cursor // 2], "score" if cursor % 2 == 0 else "created"
    key = keyword + "|" + lane
    progress = research.setdefault("queries", {}).get(key, {})
    page = int(progress.get("next_page") or 0) % config.max_search_pages if lane == "score" else 0
    return {"key": key, "keyword": keyword, "sort_on": lane, "page": page,
            "offset": page * config.search_page_size, "cursor": cursor}


def finish_search(research: dict[str, Any], config: ResearchConfig, task: dict[str, Any], raw_count: int, total: Any, captured_at: str) -> None:
    next_page = (task["page"] + 1) % config.max_search_pages if task["sort_on"] == "score" else 0
    if raw_count < config.search_page_size or (isinstance(total, (int, float)) and total <= task["offset"] + raw_count):
        next_page = 0
    previous = research.setdefault("queries", {}).get(task["key"], {})
    research["queries"][task["key"]] = {"keyword": task["keyword"], "sort_on": task["sort_on"],
        "next_page": next_page, "last_page": task["page"], "last_scanned_at": captured_at,
        "scans": int(previous.get("scans") or 0) + 1, "reported_result_count": total}
    research["query_cursor"] = (task["cursor"] + 1) % (len(config.keywords) * 2)


def remember_candidate(research: dict[str, Any], row: dict[str, Any], captured_at: str) -> bool:
    candidates = research.setdefault("candidates", {})
    key = str(row["listing_id"])
    previous = candidates.get(key, {})
    queries = list(dict.fromkeys([*previous.get("discovery_queries", []), row.get("source_query")]))[-8:]
    # No counter histories in the discovery pool. Search is not a substitute
    # for a fresh monitored detail request or a valid two-snapshot comparison.
    fields = ("listing_id", "title", "url", "image_url", "price", "currency", "source_query",
              "search_rank", "shop_id", "views", "favorites", "quantity")
    candidates[key] = {**previous, **{field: row.get(field) for field in fields},
        "discovered_at": previous.get("discovered_at") or captured_at,
        "last_discovered_at": captured_at, "discovery_queries": queries}
    return not bool(previous)


def candidate_shortlist(candidates: dict[str, Any], excluded: set[str], count: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, row in candidates.items():
        if key not in excluded:
            groups.setdefault(str(row.get("source_query") or "other"), []).append(row)
    for group in groups.values():
        group.sort(key=lambda row: (str(row.get("last_discovered_at") or ""), score(row)), reverse=True)
    # Round robin across niches so one broad SVG query cannot own all slots.
    output: list[dict[str, Any]] = []
    ordered = sorted(groups)
    depth = 0
    while len(output) < count:
        available = [groups[name][depth] for name in ordered if depth < len(groups[name])]
        if not available:
            break
        output.extend(available[:count - len(output)])
        depth += 1
    return output


def monitoring_plan(items: dict[str, Any], research: dict[str, Any], config: ResearchConfig, captured_at: str) -> list[int]:
    moment = timestamp(captured_at)
    assert moment is not None
    for row in items.values():
        row.setdefault("admitted_at", row.get("first_tracked_at") or captured_at)
    priority = sorted(items, key=lambda key: (-score(items[key]), key))[:config.priority_per_run]
    protected = set(priority)
    # A full measured pool can admit genuinely new candidates by retiring its
    # weakest mature non-priority rows. Their last summary remains discoverable.
    replaceable = sorted((key for key, row in items.items() if key not in protected
        and moment - (timestamp(row.get("admitted_at")) or moment) >= timedelta(hours=config.minimum_tracking_hours)),
        key=lambda key: (score(items[key]), str(items[key].get("last_requested_at") or ""), key))
    capacity = max(0, config.max_tracked - len(items)) + len(replaceable)
    candidates = research.setdefault("candidates", {})
    cooling = {key for key, row in candidates.items() if timestamp(row.get("retired_at")) is not None
               and moment - timestamp(row["retired_at"]) < timedelta(hours=config.minimum_tracking_hours)}
    new_rows = candidate_shortlist(candidates, set(items) | cooling, min(config.new_tracked_per_run, capacity))
    retire_count = max(0, len(items) + len(new_rows) - config.max_tracked)
    for key in replaceable[:retire_count]:
        old = items.pop(key)
        if key in research["candidates"]:
            research["candidates"][key]["retired_at"] = captured_at
            research["candidates"][key]["previous_tracking"] = {field: old.get(field) for field in
                ("last_seen_at", "views_24h", "confirmed_sales_24h", "estimated_sales_24h", "priority_score")}
    new_ids = []
    for row in new_rows:
        key = str(row["listing_id"])
        # An unmeasured candidate must not display search counters as a sample.
        items[key] = {**row, "admitted_at": captured_at, "samples": [], "last_seen_at": None,
                      "metrics_ready": False, "view_status": "waiting"}
        new_ids.append(key)
    rotation = sorted((key for key in items if key not in protected and key not in new_ids),
        key=lambda key: (str(items[key].get("last_requested_at") or items[key].get("last_seen_at") or ""), key))
    selected = priority + new_ids + rotation[:max(0, config.refresh_per_run - len(priority) - len(new_ids))]
    for key in selected:
        items[key]["last_requested_at"] = captured_at
        items[key]["tracking_tier"] = "priority" if key in protected else "new" if key in new_ids else "rotation"
    research.setdefault("last_run", {}).update({"admitted_listings": len(new_ids), "retired_listings": retire_count,
        "requested_listings": len(selected), "priority_requested": len(priority),
        "rotation_requested": len(selected) - len(priority) - len(new_ids)})
    return [int(key) for key in selected]


def prune_candidates(research: dict[str, Any], tracked: dict[str, Any], config: ResearchConfig) -> None:
    candidates = research.setdefault("candidates", {})
    ordered = sorted(candidates, key=lambda key: (key in tracked, str(candidates[key].get("last_discovered_at") or ""), score(candidates[key])), reverse=True)
    research["candidates"] = {key: candidates[key] for key in ordered[:config.max_candidates]}


def requests_in_window(research: dict[str, Any], moment: datetime) -> int:
    cutoff = moment - timedelta(hours=24)
    return sum(int(run.get("requests") or 0) for run in research.get("runs", [])
               if (timestamp(run.get("captured_at")) or cutoff) > cutoff)


def record_run(research: dict[str, Any], moment: datetime, requests: int) -> None:
    cutoff = moment - timedelta(hours=24)
    runs = [run for run in research.get("runs", []) if (timestamp(run.get("captured_at")) or cutoff) > cutoff]
    if requests:
        runs.append({"captured_at": moment.isoformat(), "requests": requests})
    research["runs"] = runs
