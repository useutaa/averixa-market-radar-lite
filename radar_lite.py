"""Standalone free Market Radar collector for scheduled GitHub Actions runs.

Only compact public Etsy observations are kept in a JSON file.  API credentials
come exclusively from environment variables and are never serialized.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from research import (ResearchConfig, finish_search, monitoring_plan, prune_candidates,
                      record_run, remember_candidate, requests_in_window, search_task)


API_ROOT = "https://api.etsy.com/v3/application"
CONFIG_PATH = Path(__file__).parent / "config" / "research.json"
DEFAULT_CONFIG = ResearchConfig.from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
KEYWORDS = DEFAULT_CONFIG.keywords
MAX_ITEMS = DEFAULT_CONFIG.max_tracked
KEEP_DAYS = 35
MAX_API_REQUESTS = DEFAULT_CONFIG.max_requests_per_run
MIN_REQUEST_INTERVAL = 1.1


class RadarError(RuntimeError):
    """Safe-to-print error that contains no headers or API secrets."""


class QuotaDeferred(RadarError):
    """A collection was deferred; keep the last complete snapshot."""


class RequestBudget:
    """Pace requests and retain a reserve from Etsy's live quota headers.

    The reserve is 10% of the reported daily limit, with a 25-call minimum.
    Limits belong to the API key, so other applications can consume them too.
    Missing headers leave the daily quota unknown rather than inventing one.
    """

    def __init__(self, max_requests: int = MAX_API_REQUESTS) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.daily_limit: int | None = None
        self.remaining_today: int | None = None
        self.second_limit: int | None = None
        self.remaining_second: int | None = None
        self.last_request_at: float | None = None
        self.retry_at: str | None = None

    @property
    def reserve(self) -> int:
        return max(25, math.ceil((self.daily_limit or 0) * 0.1))

    def before_request(self) -> None:
        if self.requests >= self.max_requests:
            raise QuotaDeferred("Etsy collection request budget exhausted; saved snapshot preserved.")
        # Reserve enough room for the entire remainder of this capped run.
        needed = self.reserve + self.max_requests - self.requests
        if self.remaining_today is not None and self.remaining_today < needed:
            raise QuotaDeferred("Etsy daily quota reserve reached; saved snapshot preserved.")
        if self.last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self.last_request_at)
            if wait > 0:
                time.sleep(wait)
        self.last_request_at = time.monotonic()
        self.requests += 1
        if self.remaining_today is not None:
            self.remaining_today = max(0, self.remaining_today - 1)

    def record(self, headers: Any) -> None:
        # The Etsy guide currently spells one header without the final 'd'.
        values = {str(key).lower(): value for key, value in headers.items()}
        fields = {
            "daily_limit": ("x-limit-per-day",),
            "remaining_today": ("x-remaining-today",),
            "second_limit": ("x-limit-per-second",),
            "remaining_second": ("x-remaining-this-second", "x-remaining-this-secon"),
        }
        for attribute, names in fields.items():
            for name in names:
                try:
                    value = int(values[name])
                except (KeyError, TypeError, ValueError):
                    continue
                if value >= 0:
                    setattr(self, attribute, value)
                    break

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "max_requests_per_run": self.max_requests,
            "daily_limit": self.daily_limit,
            "remaining_today": self.remaining_today,
            "daily_reserve": self.reserve,
            "second_limit": self.second_limit,
            "retry_at": self.retry_at,
        }


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def number(value: Any) -> float | int | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return int(result) if result.is_integer() else round(result, 4)


def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def get(path: str, keystring: str, secret: str, *, budget: RequestBudget | None = None, **params: Any) -> dict[str, Any]:
    if budget is not None:
        budget.before_request()
    request = Request(
        f"{API_ROOT}{path}?{urlencode(params, doseq=True)}",
        headers={
            "x-api-key": f"{keystring}:{secret}",
            "Accept": "application/json",
            "User-Agent": "AverixaMarketRadarLite/1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: fixed HTTPS API root
            if budget is not None:
                budget.record(response.headers)
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise RadarError("Etsy rejected the configured credentials.") from exc
        if exc.code == 429:
            if budget is not None:
                budget.record(exc.headers or {})
                try:
                    delay = max(1, int(exc.headers.get("Retry-After", "3600")))
                except (AttributeError, TypeError, ValueError):
                    delay = 3600
                budget.retry_at = (now() + timedelta(seconds=delay)).isoformat()
            raise QuotaDeferred("Etsy requested a quota pause; saved snapshot preserved.") from exc
        raise RadarError(f"Etsy API returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RadarError("Etsy API network response failed.") from exc


def digital(raw: dict[str, Any]) -> bool:
    listing_type = str(raw.get("type") or raw.get("listing_type") or "").lower().strip()
    if listing_type:
        return listing_type in {"download", "digital", "both"}
    text = " ".join((str(raw.get("title") or ""), str(raw.get("description") or ""))).lower()
    return any(word in text for word in ("svg", "png", "digital", "printable", "template"))


def normalise(raw: dict[str, Any], keyword: str, rank: int) -> dict[str, Any] | None:
    if not raw.get("listing_id") or not digital(raw):
        return None
    image = next(iter(raw.get("images") or raw.get("Images") or []), {})
    price = raw.get("price")
    if isinstance(price, dict):
        amount, divisor = number(price.get("amount")), number(price.get("divisor")) or 100
        price_value = round(float(amount) / float(divisor), 2) if amount is not None else None
        currency = price.get("currency_code") or "USD"
    else:
        price_value, currency = number(price), raw.get("currency_code") or "USD"
    shop = raw.get("shop") or raw.get("Shop") or {}
    return {
        "listing_id": int(raw["listing_id"]),
        "title": html.unescape(str(raw.get("title") or f"Listing {raw['listing_id']}")).strip(),
        "url": raw.get("url") or f"https://www.etsy.com/listing/{raw['listing_id']}",
        "image_url": image.get("url_570xN") if isinstance(image, dict) else None,
        "price": price_value,
        "currency": currency,
        "source_query": keyword,
        "search_rank": rank,
        "shop_id": number(shop.get("shop_id") if isinstance(shop, dict) and shop.get("shop_id") is not None else raw.get("shop_id")),
        "quantity": number(raw.get("quantity")),
        "views": number(raw.get("views")),
        "views_source": raw.get("_views_source") if number(raw.get("views")) is not None else None,
        "favorites": number(raw.get("num_favorers") if raw.get("num_favorers") is not None else raw.get("favorites")),
        "shop_sales": number(shop.get("transaction_sold_count") if shop.get("transaction_sold_count") is not None else shop.get("sales")),
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "items": {}, "last_success_at": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RadarError("Saved Lite state could not be read; refusing to overwrite it.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise RadarError("Saved Lite state has an invalid format; refusing to overwrite it.")
    return state


def save_state(state: dict[str, Any], path: Path) -> None:
    """Replace a complete state atomically on Windows and Linux."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as output:
        json.dump(state, output, ensure_ascii=False, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
        temporary_path = Path(output.name)
    temporary_path.replace(path)


def _comparison(samples: list[dict[str, Any]]) -> tuple[dict[str, Any], float] | None:
    """Choose the observation closest to 24 hours before the latest sample.

    The hourly job needs at least two hours of observations before comparing.
    Shorter windows are transparently normalised to a 24-hour rate. Once a
    24-hour sample exists it wins over the shorter comparison.
    """
    if len(samples) < 2:
        return None
    current_time = parse_time(samples[-1].get("captured_at"))
    if not current_time:
        return None
    choices: list[tuple[dict[str, Any], float]] = []
    for sample in samples[:-1]:
        before_time = parse_time(sample.get("captured_at"))
        if not before_time:
            continue
        hours = (current_time - before_time).total_seconds() / 3600
        if 2 <= hours <= 30:
            choices.append((sample, hours))
    return min(choices, key=lambda choice: abs(choice[1] - 24)) if choices else None


def recalculate_metrics(state: dict[str, Any], captured_at: str) -> None:
    """Apply the Railway-style public-signal scoring to the current batch.

    "Onaylı" remains deliberately conservative: a tracked listing's quantity
    decrease must be corroborated by the public shop sales-counter increase in
    the same interval.  It is never represented as private Etsy order data.
    """
    intervals: dict[str, dict[str, Any]] = {}
    for item in state["items"].values():
        refreshed = item.get("last_seen_at") == captured_at
        item["updated_in_latest"] = refreshed
        found = _comparison(list(item.get("samples") or []))
        if not refreshed or not found:
            item.update({
                "metrics_ready": False,
                "comparison_hours": None,
                "quantity_delta": None,
                "stock_movement_24h": None,
                "confirmed_sales_24h": None,
                "estimated_sales_24h": None,
                "views_24h": None,
                "view_delta_observed": None,
                "view_status": "stale" if not refreshed else "unavailable" if item.get("views") is None else "waiting",
                "favorites_24h": None,
                "shop_sales_24h": None,
                "confirmed_sales_view_24h": None,
                "estimated_sales_view_24h": None,
                "confidence": 0,
                "basis": "Bu taramada güncellenmedi" if not refreshed else "Karşılaştırma bekleniyor",
                "hot_score": 0.0,
            })
            continue
        before, hours = found
        current_sample = item["samples"][-1]
        def delta(field: str) -> float:
            latest, previous = number(current_sample.get(field)), number(before.get(field))
            return max(float(latest) - float(previous), 0.0) if latest is not None and previous is not None else 0.0
        latest_views, previous_views = number(current_sample.get("views")), number(before.get("views"))
        view_delta: float | None = None
        if latest_views is None or previous_views is None:
            view_status = "unavailable"
        elif latest_views < previous_views:
            view_status = "counter_decreased"
        else:
            view_delta = float(latest_views) - float(previous_views)
            view_status = "increased" if view_delta > 0 else "unchanged"
        latest_stock, previous_stock = number(current_sample.get("quantity")), number(before.get("quantity"))
        stock = max(float(previous_stock) - float(latest_stock), 0.0) if latest_stock is not None and previous_stock is not None else 0.0
        intervals[str(item["listing_id"])] = {
            "item": item,
            "baseline_at": str(before.get("captured_at")),
            "hours": hours,
            "stock": stock,
            "views": view_delta or 0.0,
            "view_delta": view_delta,
            "view_status": view_status,
            "favorites": delta("favorites"),
            "shop_sales": delta("shop_sales"),
        }

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for interval in intervals.values():
        shop_id = interval["item"].get("shop_id")
        if shop_id is not None:
            groups.setdefault((str(shop_id), interval["baseline_at"]), []).append(interval)

    for interval in intervals.values():
        item, hours = interval["item"], interval["hours"]
        # Rotating rows can have different baselines. Never use a shop's
        # 24-hour increase to corroborate a listing's four-hour stock change.
        group = groups.get((str(item.get("shop_id")), interval["baseline_at"]), []) if item.get("shop_id") is not None else []
        group_stock = sum(float(row["stock"]) for row in group)
        group_shop_sales = max((float(row["shop_sales"]) for row in group), default=0.0)
        corroborated = bool(group and group_stock > 0 and group_shop_sales >= group_stock)
        confirmed = float(interval["stock"]) if corroborated else 0.0
        unconfirmed = max(float(interval["stock"]) - confirmed, 0.0)
        momentum = 0.15 + float(interval["views"]) * 0.02 + float(interval["favorites"]) + (101 - min(max(float(item.get("search_rank") or 100), 1), 100)) / 220
        group_momentum = sum(
            0.15 + float(row["views"]) * 0.02 + float(row["favorites"]) + (101 - min(max(float(row["item"].get("search_rank") or 100), 1), 100)) / 220
            for row in group
        )
        residual = max(group_shop_sales - group_stock, 0.0)
        coverage = min(len(group) / (len(group) + 18), 0.45) if group else 0.0
        allocated = residual * momentum / group_momentum * coverage if group_momentum else 0.0
        estimated = confirmed + unconfirmed * 0.35 + allocated
        scale = 24 / hours
        views_24h = round(float(interval["view_delta"]) * scale, 2) if interval["view_delta"] is not None else None
        confirmed_24h = round(confirmed * scale, 2)
        estimated_24h = round(estimated * scale, 2)
        stock_24h = round(float(interval["stock"]) * scale, 2)
        ratio_ready = views_24h is not None and views_24h >= 5
        confidence = min(100, int(40 + (20 if interval["views"] > 0 else 0) + (10 if interval["favorites"] > 0 else 0) + (30 if corroborated else 0) + (10 if interval["stock"] > 0 else 0)))
        if corroborated and interval["views"] > 0:
            basis = "Onaylı: stok + mağaza + view"
        elif corroborated:
            basis = "Onaylı: stok + mağaza"
        elif interval["stock"] > 0:
            basis = "Stok hareketi, kısmi doğrulama"
        elif interval["shop_sales"] > 0 and interval["views"] > 0:
            basis = "Mağaza satışı + view dağılımı"
        else:
            basis = "Ölçüm var, satış sinyali yok"
        item.update({
            "metrics_ready": True,
            "comparison_hours": round(hours, 2),
            "quantity_delta": round(float(interval["stock"]), 2),
            "stock_movement_24h": stock_24h,
            "confirmed_sales_24h": confirmed_24h,
            "estimated_sales_24h": estimated_24h,
            "views_24h": views_24h,
            "view_delta_observed": interval["view_delta"],
            "view_status": interval["view_status"],
            "favorites_24h": round(float(interval["favorites"]) * scale, 2),
            "shop_sales_24h": round(float(interval["shop_sales"]) * scale, 2),
            "confirmed_sales_view_24h": round(confirmed_24h / views_24h * 100, 2) if ratio_ready else None,
            "estimated_sales_view_24h": round(estimated_24h / views_24h * 100, 2) if ratio_ready else None,
            "confidence": confidence,
            "basis": basis,
            "hot_score": round(confirmed_24h * 12 + estimated_24h * 4 + ((views_24h or 0) + 1) ** 0.5 + float(interval["favorites"]) * scale * 0.8 + confidence / 25, 2),
        })


def merge(state: dict[str, Any], observation: dict[str, Any], captured_at: str, *, history_samples: int = DEFAULT_CONFIG.history_samples) -> None:
    key = str(observation["listing_id"])
    old = state["items"].get(key, {})
    samples = list(old.get("samples") or [])
    snapshot = {"captured_at": captured_at, **{field: observation.get(field) for field in ("quantity", "views", "views_source", "favorites", "shop_sales")}}
    if samples and samples[-1].get("captured_at") == captured_at:
        samples[-1] = snapshot
    else:
        samples.append(snapshot)
    cutoff = now() - timedelta(days=KEEP_DAYS)
    samples = [item for item in samples if (parse_time(item.get("captured_at")) or now()) >= cutoff][-history_samples:]
    state["items"][key] = {**old, **observation, "samples": samples, "last_seen_at": captured_at}


def prune(state: dict[str, Any], *, max_items: int = MAX_ITEMS) -> None:
    cutoff = now() - timedelta(days=KEEP_DAYS)
    current = [item for item in state["items"].values() if (parse_time(item.get("last_seen_at")) or cutoff - timedelta(seconds=1)) >= cutoff]
    current.sort(key=lambda item: (float(item.get("hot_score") or 0), float(item.get("views_24h") or 0)), reverse=True)
    # Candidates awaiting their first successful detail response have no
    # last_seen_at yet. Keep them for the bounded admission grace period.
    pending = [item for item in state["items"].values() if not item.get("last_seen_at")
               and (parse_time(item.get("admitted_at")) or cutoff - timedelta(seconds=1)) >= cutoff]
    state["items"] = {str(item["listing_id"]): item for item in (current + pending)[:max_items]}


def collect(keystring: str, secret: str, *, budget: RequestBudget | None = None,
            tracked_items: dict[str, Any] | None = None, research_state: dict[str, Any] | None = None,
            config: ResearchConfig = DEFAULT_CONFIG, captured_at: str | None = None) -> list[dict[str, Any]]:
    """Discover across resumable niches/pages, then refresh a measured shortlist.

    Mutations target a working copy committed by main only after all requests
    succeed. Cached/search values never become fabricated monitored samples.
    """
    budget = budget if budget is not None else RequestBudget(config.max_requests_per_run)
    tracked_items = tracked_items if tracked_items is not None else {}
    research_state = research_state if research_state is not None else {}
    captured_at = captured_at or now().isoformat()
    research_state["last_run"] = {"search_requests": 0, "search_results_seen": 0,
        "digital_results_seen": 0, "new_candidates": 0, "queries": []}
    for key, old in tracked_items.items():
        if key not in research_state.setdefault("candidates", {}):
            remember_candidate(research_state, old, old.get("last_seen_at") or captured_at)
    found: dict[int, tuple[dict[str, Any], str, int]] = {}
    for _ in range(config.search_requests_per_run):
        task = search_task(research_state, config)
        keyword = task["keyword"]
        response = get("/listings/active", keystring, secret, budget=budget, keywords=keyword,
                       limit=config.search_page_size, offset=task["offset"], sort_on=task["sort_on"],
                       sort_order="desc", is_safe="true", currency="USD")
        results = response.get("results", [])
        summary = research_state["last_run"]
        summary["search_requests"] += 1
        summary["search_results_seen"] += len(results)
        summary["queries"].append({"keyword": keyword, "sort_on": task["sort_on"], "page": task["page"] + 1})
        for rank, item in enumerate(results, start=task["offset"] + 1):
            row = normalise(item, keyword, rank)
            if row:
                summary["digital_results_seen"] += 1
                summary["new_candidates"] += int(remember_candidate(research_state, row, captured_at))
                found.setdefault(int(item["listing_id"]), (dict(item), keyword, rank))
        finish_search(research_state, config, task, len(results), response.get("count"), captured_at)
    research_state["last_run"]["unique_candidates_seen"] = len(found)
    details: dict[int, dict[str, Any]] = {}
    ids = monitoring_plan(tracked_items, research_state, config, captured_at)
    for start in range(0, len(ids), 100):
        response = get("/listings/batch", keystring, secret, budget=budget, listing_ids=",".join(map(str, ids[start:start + 100])), includes="Shop,Images", currency="USD")
        details.update({int(item["listing_id"]): item for item in response.get("results", []) if item.get("listing_id")})
    output: list[dict[str, Any]] = []
    for listing_id in ids:
        old = tracked_items.get(str(listing_id), {})
        search_row, keyword, rank = found.get(listing_id, ({}, str(old.get("source_query") or "tracked"), int(old.get("search_rank") or 100)))
        detail_row = details.get(listing_id, {})
        if not search_row and not detail_row:
            continue
        raw = {**search_row, **detail_row}
        if number(detail_row.get("views")) is not None:
            raw["_views_source"] = "listing_batch"
        elif number(search_row.get("views")) is not None:
            raw["views"] = search_row["views"]
            raw["_views_source"] = "listing_search"
        else:
            raw["views"] = None
            raw["_views_source"] = None
        row = normalise(raw, keyword, rank)
        if row:
            output.append(row)
    prune_candidates(research_state, tracked_items, config)
    return output


HTML = r"""<!doctype html>
<html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EtsyPulse</title>
<style>
[hidden]{display:none!important}
:root{--bg:#090d16;--panel:#121a2a;--line:#283650;--text:#f3f7ff;--muted:#a7b4cf;--green:#6ee7b7;--blue:#8bd3ff;--yellow:#fcd34d;--pink:#f9a8d4}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#152445 0,var(--bg) 40%);color:var(--text);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif}main{max-width:1500px;margin:auto;padding:30px 18px 42px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.brand h1{margin:0;font-size:29px}.brand p,.muted{color:var(--muted)}.pill{display:inline-block;border:1px solid #35606d;background:#11313a;color:var(--green);padding:5px 9px;border-radius:999px;font-weight:700;font-size:12px}.cards{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:10px;margin:22px 0 14px}.card,.panel{background:rgba(18,26,42,.94);border:1px solid var(--line);border-radius:13px}.card{padding:14px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.value{font-size:25px;font-weight:800;color:var(--green);margin-top:4px}.panel{padding:15px}.tabs{display:flex;gap:8px;margin:14px 0}.tab,button,select,input{border:1px solid var(--line);background:#0c1321;color:var(--text);border-radius:8px;padding:9px 11px}.tab{cursor:pointer}.tab.active{background:#1b5360;border-color:#2998a8}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px}.filters input{min-width:260px;flex:1}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;min-width:1180px}th,td{padding:10px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;color:var(--muted);background:#101827;position:sticky;top:0;z-index:1}tr:hover td{background:#172339}.product{min-width:300px}.product a,a{color:var(--blue)}.sub{font-size:11px;color:var(--muted);margin-top:3px}.confirmed{color:var(--green);font-weight:800}.estimated{color:var(--yellow);font-weight:700}.ratio{color:var(--pink);font-weight:800}.badge{display:inline-block;padding:3px 6px;border:1px solid #3a4a69;border-radius:999px;font-size:11px;white-space:nowrap}.niche-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.niche{padding:15px}.niche h3{margin:0 0 4px;font-size:16px}.niche-metrics{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.niche-metrics b{display:block;color:var(--green);font-size:20px}.foot{margin-top:15px;color:var(--muted);font-size:12px}.view{display:none}.view.active{display:block}@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.niche-grid{grid-template-columns:1fr}}@media(max-width:600px){main{padding:20px 12px}.top{flex-direction:column}.cards{grid-template-columns:repeat(2,1fr)}.filters input{min-width:100%}}
</style>
<main><div class="top"><div class="brand"><h1>EtsyPulse</h1><p>Averixa market intelligence · public Etsy signals · <span id="updated">yükleniyor…</span></p></div><span class="pill">PC kapalıyken de çalışır</span></div>
<section class="cards" id="cards"></section>
<div class="panel"><div class="tabs"><button class="tab active" data-view="products">Ölçüm takibi</button><button class="tab" data-view="discovery">Keşfedilen adaylar</button><button class="tab" data-view="niches">Niş özeti</button></div>
<section id="products" class="view active"><div class="filters"><input id="q" placeholder="Ürün veya anahtar kelime ara"><select id="sort"><option value="confirmed_sales_view_24h">Onaylı S/View</option><option value="confirmed_sales_24h">Onaylı satış</option><option value="estimated_sales_24h">Tahmini satış</option><option value="hot_score">Fırsat skoru</option><option value="views_24h">View artışı</option></select><select id="quality"><option value="all">Tüm kayıtlar</option><option value="ready">Ölçümü olanlar</option><option value="confirmed">Onaylı sinyaller</option><option value="views">View artışı olanlar</option></select><button id="download-csv" type="button">Tüm ilanları CSV indir</button></div><div class="sub">Toplam view = ilan sayacı · View artışı / 24s = belirtilen ölçüm aralığındaki artışın 24 saate uyarlanmış hızı · — = geçerli karşılaştırma yok · CSV tüm takip ilanlarını indirir</div><div id="product-rows"></div></section>
<div class="filters" id="product-pager"><button id="product-prev">Önceki sayfa</button><span class="muted" id="product-page"></span><button id="product-next">Sonraki sayfa</button></div>
<section id="discovery" class="view"><p class="muted" id="coverage"></p><div class="filters"><input id="cq" placeholder="Keşfedilen adaylarda ara"><button id="download-candidates">Tüm adayları CSV indir</button></div><div id="candidate-rows"></div><div class="filters"><button id="candidate-prev">Önceki aday sayfası</button><span class="muted" id="candidate-page"></span><button id="candidate-next">Sonraki aday sayfası</button></div></section>
<section id="niches" class="view"><div class="filters"><input id="nq" placeholder="Niş ara"></div><div class="niche-grid" id="niche-rows"></div></section></div>
<p class="foot" id="quota"></p><p class="foot"><b>Kapsam</b>: yapılandırılmış dijital nişlerde dönen aramalar; Etsy'nin tüm dijital kataloğunun eksiksiz kopyası değildir. <b>Onaylı</b>: aynı ölçüm aralığında stok düşüşü, public mağaza satış sayacıyla desteklenmiş sinyal. <b>Tahmini</b>: public stok/view/favori sinyallerinden ihtiyatlı tahmin. Rakiplerin özel Etsy sipariş verisi değildir.</p></main>
<script>
const n=v=>Number(v||0), f=v=>v==null?'—':n(v).toLocaleString(undefined,{maximumFractionDigits:2}), pct=v=>v==null?'—':f(v)+'%', esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let products=[], niches=[], candidates=[], productPage=0, candidatePage=0;
function metric(v,cls=''){return `<span class="${cls}">${f(v)}</span>`}
function viewCell(r){const labels={increased:'Artış var',unchanged:'Sayaç değişmedi',waiting:'Karşılaştırma bekleniyor',unavailable:'View verisi eksik',counter_decreased:'Sayaç azaldı',stale:'Güncel ölçüm yok'};return `<span title="Ölçülen artış: ${f(r.view_delta_observed)} · pencere: ${f(r.comparison_hours)} saat">${f(r.views_24h)}</span><div class="sub">${esc(labels[r.view_status]||'Karşılaştırma bekleniyor')}</div>`}
function drawProducts(){
 const q=document.querySelector('#q').value.toLowerCase(),sort=document.querySelector('#sort').value,quality=document.querySelector('#quality').value;
 const matched=products.filter(r=>(r.title+' '+r.source_query).toLowerCase().includes(q)).filter(r=>quality==='all'||(quality==='ready'&&r.metrics_ready)||(quality==='confirmed'&&n(r.confirmed_sales_24h)>0)||(quality==='views'&&n(r.views_24h)>0)).sort((a,b)=>n(b[sort])-n(a[sort]));
 productPage=Math.min(productPage,Math.max(0,Math.ceil(matched.length/100)-1));const rows=matched.slice(productPage*100,(productPage+1)*100);
 document.querySelector('#product-page').textContent=`Sayfa ${productPage+1} / ${Math.max(1,Math.ceil(matched.length/100))} · ${matched.length} ilan`;document.querySelector('#product-prev').disabled=productPage===0;document.querySelector('#product-next').disabled=(productPage+1)*100>=matched.length;
 document.querySelector('#product-rows').innerHTML=rows.length?`<div class="table-wrap"><table><tr><th>Ürün</th><th>Onaylı<br>24s</th><th>Tahmini<br>24s</th><th>İlanın toplam<br>view’ı</th><th>View artışı<br>/ 24s</th><th>Onaylı<br>S/View</th><th>Tahmini<br>S/View</th><th>Stok<br>hareketi</th><th>Fiyat</th><th>Güven</th><th>Dayanak</th></tr>${rows.map(r=>`<tr><td class="product"><a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.title)}</a><div class="sub"><span class="badge">${esc(r.source_query)}</span> · ${r.metrics_ready?f(r.comparison_hours)+'s ölçüm':'karşılaştırma bekleniyor'}</div><div class="sub">Son kontrol: ${r.last_seen_at?esc(new Date(r.last_seen_at).toLocaleString('tr-TR')):'—'}</div></td><td>${metric(r.confirmed_sales_24h,'confirmed')}</td><td>${metric(r.estimated_sales_24h,'estimated')}</td><td>${f(r.views)}</td><td>${viewCell(r)}</td><td class="ratio">${pct(r.confirmed_sales_view_24h)}</td><td class="ratio">${pct(r.estimated_sales_view_24h)}</td><td>${f(r.stock_movement_24h)}</td><td>${esc(r.currency||'')} ${f(r.price)}</td><td>${r.metrics_ready?'%'+f(r.confidence):'—'}</td><td><span class="badge">${esc(r.basis||'—')}</span></td></tr>`).join('')}</table></div>`:'<p class="muted">Bu filtrede kayıt yok.</p>';
}
function downloadCsv(){
 const head=['listing_id','title','url','keyword','price','currency','search_rank','quantity','total_views','view_delta_24h','view_delta_observed','view_status','comparison_hours','last_seen_at','updated_in_latest','favorites','shop_sales','confirmed_sales_24h','estimated_sales_24h','stock_movement_24h','confidence','basis','tracking_tier'];
 const allRows=[...products].sort((a,b)=>n(b.hot_score)-n(a.hot_score));
 const body=allRows.map(r=>[r.listing_id,r.title,r.url,r.source_query,r.price,r.currency,r.search_rank,r.quantity,r.views,r.views_24h,r.view_delta_observed,r.view_status,r.comparison_hours,r.last_seen_at,r.updated_in_latest,r.favorites,r.shop_sales,r.confirmed_sales_24h,r.estimated_sales_24h,r.stock_movement_24h,r.confidence,r.basis,r.tracking_tier]);
 saveCsv(head,body,'etsypulse-all-listings-');
}
function saveCsv(head,body,prefix){
 // Quoting alone does not stop spreadsheet formula injection from titles.
 const quote=v=>'"'+String(typeof v==='string'&&/^[=+\-@\t\r]/.test(v)?"'"+v:v??'').replaceAll('"','""')+'"';
 const csv='\ufeff'+[head,...body].map(row=>row.map(quote).join(';')).join('\r\n'),file=new Blob([csv],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(file),link=document.createElement('a');
 link.href=url;link.download=prefix+new Date().toISOString().slice(0,10)+'.csv';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function drawCandidates(){
 const q=document.querySelector('#cq').value.toLowerCase(),matched=candidates.filter(r=>(r.title+' '+r.discovery_queries.join(' ')).toLowerCase().includes(q));candidatePage=Math.min(candidatePage,Math.max(0,Math.ceil(matched.length/100)-1));const rows=matched.slice(candidatePage*100,(candidatePage+1)*100);
 document.querySelector('#candidate-page').textContent=`Sayfa ${candidatePage+1} / ${Math.max(1,Math.ceil(matched.length/100))} · ${matched.length} aday`;document.querySelector('#candidate-prev').disabled=candidatePage===0;document.querySelector('#candidate-next').disabled=(candidatePage+1)*100>=matched.length;
 document.querySelector('#candidate-rows').innerHTML=rows.length?`<div class="table-wrap"><table><tr><th>Dijital ürün adayı</th><th>Nişler</th><th>Fiyat</th><th>Son keşifte toplam view</th><th>Son keşif</th><th>Ölçüm durumu</th></tr>${rows.map(r=>`<tr><td class="product"><a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.title)}</a></td><td>${esc(r.discovery_queries.join(', '))}</td><td>${esc(r.currency)} ${f(r.price)}</td><td>${f(r.views)}</td><td>${esc(new Date(r.last_discovered_at).toLocaleString('tr-TR'))}</td><td>${r.is_tracked?'Ölçüm takibinde':'Aday havuzunda; satış/view karşılaştırması yok'}</td></tr>`).join('')}</table></div>`:'<p class="muted">Bu filtrede aday yok.</p>';
}
function downloadCandidates(){const fields=['listing_id','title','url','source_query','price','currency','views','discovered_at','last_discovered_at','is_tracked'];saveCsv([...fields,'discovery_queries'],candidates.map(r=>[...fields.map(k=>r[k]),r.discovery_queries.join(' | ')]),'etsypulse-all-candidates-')}
function drawNiches(){const q=document.querySelector('#nq').value.toLowerCase(),rows=niches.filter(x=>x.niche_name.toLowerCase().includes(q));document.querySelector('#niche-rows').innerHTML=rows.length?rows.map(x=>`<article class="panel niche"><h3>${esc(x.niche_name)}</h3><div class="muted">${f(x.comparable)} / ${f(x.observed_listing_count)} ölçülebilir ilan · örnek: <a href="${esc(x.sample_url||'#')}" target="_blank" rel="noreferrer">${esc(x.sample_title||'—')}</a></div><div class="niche-metrics"><div><span class="label">Onaylı 24s</span><b>${f(x.confirmed_sales_24h)}</b></div><div><span class="label">Tahmini 24s</span><b>${f(x.estimated_sales_24h)}</b></div><div><span class="label">S/View</span><b>${pct(x.confirmed_sales_view_24h)}</b></div><div><span class="label">Fırsat</span><b>${f(x.opportunity_score)}</b></div></div></article>`).join(''):'<p class="muted">Bu filtrede niş yok.</p>'}
const cacheBust='?v='+Date.now();Promise.all(['data/status.json','data/radar.json','data/niches.json','data/candidates.json'].map(x=>fetch(x+cacheBust,{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('Data unavailable');return r.json()}))).then(([s,r,ns,cs])=>{products=r;niches=ns;candidates=cs;document.querySelector('#updated').textContent=(s.generated_at?'son ölçüm: '+new Date(s.generated_at).toLocaleString('tr-TR'):'henüz ölçüm yok')+(s.collection_message?' · '+s.collection_message:'');document.querySelector('#cards').innerHTML=[['Keşfedilen aday',s.candidates],['Takipteki ilan',s.listings],['Bu taramada yenilenen',s.updated_listings],['Karşılaştırılabilen',s.comparable],['View artışı olan',s.view_increase_listings],['Bu taramada yeni aday',s.discovery.new_candidates||0],['Onaylı satış · 24s',s.confirmed_sales_24h],['Tahmini satış · 24s',s.estimated_sales_24h],['En iyi onaylı S/View',pct(s.top_confirmed_sales_view_24h)],['Yapılandırılmış niş',s.keywords],['Taranmış niş',s.scanned_keywords],['API isteği / tarama',s.api_usage.requests||0]].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${typeof v==='string'?esc(v):f(v)}</div></div>`).join('');document.querySelector('#coverage').textContent=`${s.scanned_keywords}/${s.keywords} niş ziyaret edildi. Aday kapasitesi ${f(s.capacities.candidates)}, ölçüm kapasitesi ${f(s.capacities.tracked)}. Her nişte yeni ilanlar ve sırayla daha derin sonuç sayfaları taranır. Son tur: ${(s.discovery.queries||[]).map(x=>x.keyword+' · '+(x.sort_on==='score'?'ilgi sırası':'en yeni')+' · sayfa '+x.page).join(' / ')}. Aday sayacı anlık ölçüm ya da satış doğrulaması değildir.`;document.querySelector('#quota').textContent=`Kota koruması: bu tur ${f(s.api_usage.requests||0)} / ${f(s.api_usage.max_requests_per_run)} istek · Etsy kalan ${f(s.api_usage.remaining_today)} / ${f(s.api_usage.daily_limit)} · EtsyPulse son 24 saat ${f(s.api_usage.rolling_daily_requests)} / ${f(s.api_usage.max_daily_requests)} istek.`;drawProducts();drawNiches();drawCandidates();document.querySelector('#q').oninput=()=>{productPage=0;drawProducts()};document.querySelector('#sort').onchange=()=>{productPage=0;drawProducts()};document.querySelector('#quality').onchange=()=>{productPage=0;drawProducts()};document.querySelector('#download-csv').onclick=downloadCsv;document.querySelector('#download-candidates').onclick=downloadCandidates;document.querySelector('#cq').oninput=()=>{candidatePage=0;drawCandidates()};document.querySelector('#product-prev').onclick=()=>{productPage--;drawProducts()};document.querySelector('#product-next').onclick=()=>{productPage++;drawProducts()};document.querySelector('#candidate-prev').onclick=()=>{candidatePage--;drawCandidates()};document.querySelector('#candidate-next').onclick=()=>{candidatePage++;drawCandidates()};document.querySelector('#nq').oninput=drawNiches;document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelector('#'+b.dataset.view).classList.add('active');document.querySelector('#product-pager').hidden=b.dataset.view!=='products'})}).catch(()=>{document.querySelector('#updated').textContent='Veriler yüklenemedi; sayfayı yenileyin.'});
</script></html>"""


def build_niches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("source_query") or "Other"), []).append(row)
    output: list[dict[str, Any]] = []
    for name, group in grouped.items():
        confirmed = round(sum(float(row.get("confirmed_sales_24h") or 0) for row in group), 2)
        estimated = round(sum(float(row.get("estimated_sales_24h") or 0) for row in group), 2)
        views = round(sum(float(row.get("views_24h") or 0) for row in group), 2)
        comparable = sum(bool(row.get("metrics_ready")) for row in group)
        sample = max(group, key=lambda row: float(row.get("hot_score") or 0))
        prices = [float(row["price"]) for row in group if number(row.get("price")) is not None]
        output.append({
            "niche_name": name,
            "observed_listing_count": len(group),
            "comparable": comparable,
            "confirmed_sales_24h": confirmed,
            "estimated_sales_24h": estimated,
            "views_24h": views,
            "confirmed_sales_view_24h": round(confirmed / views * 100, 2) if views >= 5 else None,
            "estimated_sales_view_24h": round(estimated / views * 100, 2) if views >= 5 else None,
            "median_price": round(statistics.median(prices), 2) if prices else None,
            "opportunity_score": round(sum(float(row.get("hot_score") or 0) for row in group), 2),
            "sample_title": sample.get("title"),
            "sample_url": sample.get("url"),
        })
    return sorted(output, key=lambda row: float(row["opportunity_score"]), reverse=True)


def write_site(state: dict[str, Any], site: Path) -> None:
    data = site / "data"
    data.mkdir(parents=True, exist_ok=True)
    rows = sorted(({key: value for key, value in item.items() if key != "samples"} for item in state["items"].values()),
                  key=lambda item: (float(item.get("hot_score") or 0), float(item.get("estimated_sales_24h") or 0)), reverse=True)
    research = state.get("research", {})
    config = state.get("research_config", {})
    candidates = [{**row, "is_tracked": key in state["items"]}
                  for key, row in research.get("candidates", {}).items()]
    candidates.sort(key=lambda row: str(row.get("last_discovered_at") or ""), reverse=True)
    ready = [row for row in rows if row.get("metrics_ready")]
    ratios = [float(row["confirmed_sales_view_24h"]) for row in ready if row.get("confirmed_sales_view_24h") is not None]
    status = {
        "generated_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "collection_status": state.get("collection_status", "ok"),
        "collection_message": state.get("collection_message"),
        "api_usage": state.get("api_usage", {}),
        "listings": len(rows),
        "updated_listings": sum(bool(state.get("last_success_at")) and row.get("last_seen_at") == state.get("last_success_at") for row in rows),
        "comparable": len(ready),
        "view_increase_listings": sum(float(row.get("views_24h") or 0) > 0 for row in ready),
        "views_unavailable_listings": sum(row.get("view_status") == "unavailable" for row in rows),
        "keywords": len(config.get("keywords", KEYWORDS)),
        "scanned_keywords": len({query["keyword"] for query in research.get("queries", {}).values()
                                if query.get("last_scanned_at") and query.get("keyword") in config.get("keywords", KEYWORDS)}),
        "candidates": len(candidates),
        "discovery": research.get("last_run", {}),
        "search_progress": list(research.get("queries", {}).values()),
        "capacities": {"candidates": config.get("max_candidates", DEFAULT_CONFIG.max_candidates),
                       "tracked": config.get("max_tracked", DEFAULT_CONFIG.max_tracked),
                       "refresh_per_run": config.get("refresh_per_run", DEFAULT_CONFIG.refresh_per_run)},
        "confirmed_sales_24h": round(sum(float(row.get("confirmed_sales_24h") or 0) for row in ready), 2),
        "estimated_sales_24h": round(sum(float(row.get("estimated_sales_24h") or 0) for row in ready), 2),
        "total_views": round(sum(float(row.get("views") or 0) for row in rows), 2),
        "views_24h": round(sum(float(row.get("views_24h") or 0) for row in ready), 2),
        "top_confirmed_sales_view_24h": max(ratios) if ratios else None,
    }
    (site / "index.html").write_text(HTML, encoding="utf-8")
    (data / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (data / "radar.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (data / "niches.json").write_text(json.dumps(build_niches(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (data / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the free static Averixa Market Radar")
    parser.add_argument("--state", type=Path, default=Path("state/radar_state.json"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--render-only", action="store_true", help="Render saved public data without credentials or API requests")
    args = parser.parse_args()
    try:
        config = ResearchConfig.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit("Research configuration is missing or invalid; no Etsy requests made.") from exc
    if args.render_only:
        write_site(load_state(args.state), args.site)
        return
    keystring, secret = os.getenv("ETSY_KEYSTRING", "").strip(), os.getenv("ETSY_SHARED_SECRET", "").strip()
    if not keystring or not secret:
        raise SystemExit("ETSY_KEYSTRING and ETSY_SHARED_SECRET GitHub secrets are required.")
    state, captured_at = load_state(args.state), now().isoformat()
    used = requests_in_window(state.get("research", {}), now())
    budget = RequestBudget(config.max_requests_per_run)
    retry_at = parse_time(state.get("api_usage", {}).get("retry_at"))
    state["last_attempt_at"] = captured_at
    try:
        if retry_at is not None and retry_at > now():
            budget.retry_at = retry_at.isoformat()
            raise QuotaDeferred("Etsy quota pause still active; saved snapshot preserved.")
        if used + config.max_requests_per_run > config.max_daily_requests:
            raise QuotaDeferred("EtsyPulse rolling daily request budget reached; saved snapshot preserved.")
        working = copy.deepcopy(state)
        observations = collect(keystring, secret, budget=budget, tracked_items=working["items"],
                               research_state=working.setdefault("research", {}), config=config, captured_at=captured_at)
    except QuotaDeferred as exc:
        state["collection_status"] = "quota_deferred"
        state["collection_message"] = "Kota nedeniyle tarama ertelendi; son başarılı ölçüm gösteriliyor."
        print(str(exc))
    except RadarError as exc:
        state["collection_status"] = "api_error"
        state["collection_message"] = "Etsy yanıtı alınamadı; son başarılı ölçüm korunuyor."
        print(str(exc))
    else:
        state = working
        for row in observations:
            merge(state, row, captured_at, history_samples=config.history_samples)
        recalculate_metrics(state, captured_at)
        for row in state["items"].values():
            if row.get("updated_in_latest"):
                row["priority_score"] = float(row.get("hot_score") or 0)
        state["version"] = 2
        state["last_success_at"] = captured_at
        state["collection_status"] = "ok"
        state["collection_message"] = None
        prune(state, max_items=config.max_tracked)
        state["research_config"] = {"keywords": list(config.keywords), "max_candidates": config.max_candidates,
            "max_tracked": config.max_tracked, "refresh_per_run": config.refresh_per_run,
            "priority_per_run": config.priority_per_run, "max_search_pages": config.max_search_pages,
            "search_page_size": config.search_page_size, "max_daily_requests": config.max_daily_requests}
    record_run(state.setdefault("research", {}), now(), budget.requests)
    state["api_usage"] = budget.summary()
    state["api_usage"]["rolling_daily_requests"] = requests_in_window(state["research"], now())
    state["api_usage"]["max_daily_requests"] = config.max_daily_requests
    # Compact state keeps bounded histories safely below GitHub's file-size
    # limit. Public dashboard exports omit sample arrays entirely.
    save_state(state, args.state)
    write_site(state, args.site)
    print(json.dumps({"listings": len(state["items"]), "generated_at": state["last_success_at"], "collection_status": state["collection_status"], "api_usage": state["api_usage"]}))


if __name__ == "__main__":
    main()
