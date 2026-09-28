"""Standalone free Market Radar collector for scheduled GitHub Actions runs.

Only compact public Etsy observations are kept in a JSON file.  API credentials
come exclusively from environment variables and are never serialized.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.etsy.com/v3/application"
KEYWORDS = ("svg bundle", "sublimation png", "canva template", "printable planner")
MAX_ITEMS = 400
KEEP_DAYS = 35
# Four search calls and up to four 100-listing detail batches. This cap also
# covers future growth of the tracked pool without an unbounded API loop.
MAX_API_REQUESTS = len(KEYWORDS) + math.ceil(MAX_ITEMS / 100)
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
    current: list[dict[str, Any]] = [
        item for item in state["items"].values() if item.get("last_seen_at") == captured_at
    ]
    intervals: dict[str, dict[str, Any]] = {}
    for item in current:
        found = _comparison(list(item.get("samples") or []))
        if not found:
            item.update({
                "metrics_ready": False,
                "quantity_delta": 0,
                "stock_movement_24h": 0.0,
                "confirmed_sales_24h": 0.0,
                "estimated_sales_24h": 0.0,
                "views_24h": 0.0,
                "favorites_24h": 0.0,
                "shop_sales_24h": 0.0,
                "confirmed_sales_view_24h": None,
                "estimated_sales_view_24h": None,
                "confidence": 0,
                "basis": "İlk ölçüm · karşılaştırma bekleniyor",
                "hot_score": 0.0,
            })
            continue
        before, hours = found
        current_sample = item["samples"][-1]
        delta = lambda field: max((number(current_sample.get(field)) or 0) - (number(before.get(field)) or 0), 0)
        stock = max((number(before.get("quantity")) or 0) - (number(current_sample.get("quantity")) or 0), 0)
        intervals[str(item["listing_id"])] = {
            "item": item,
            "hours": hours,
            "stock": stock,
            "views": delta("views"),
            "favorites": delta("favorites"),
            "shop_sales": delta("shop_sales"),
        }

    groups: dict[str, list[dict[str, Any]]] = {}
    for interval in intervals.values():
        shop_id = interval["item"].get("shop_id")
        if shop_id is not None:
            groups.setdefault(str(shop_id), []).append(interval)

    for interval in intervals.values():
        item, hours = interval["item"], interval["hours"]
        group = groups.get(str(item.get("shop_id")), []) if item.get("shop_id") is not None else []
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
        views_24h = round(float(interval["views"]) * scale, 2)
        confirmed_24h = round(confirmed * scale, 2)
        estimated_24h = round(estimated * scale, 2)
        stock_24h = round(float(interval["stock"]) * scale, 2)
        ratio_ready = views_24h >= 5
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
            "favorites_24h": round(float(interval["favorites"]) * scale, 2),
            "shop_sales_24h": round(float(interval["shop_sales"]) * scale, 2),
            "confirmed_sales_view_24h": round(confirmed_24h / views_24h * 100, 2) if ratio_ready else None,
            "estimated_sales_view_24h": round(estimated_24h / views_24h * 100, 2) if ratio_ready else None,
            "confidence": confidence,
            "basis": basis,
            "hot_score": round(confirmed_24h * 12 + estimated_24h * 4 + (views_24h + 1) ** 0.5 + float(interval["favorites"]) * scale * 0.8 + confidence / 25, 2),
        })


def merge(state: dict[str, Any], observation: dict[str, Any], captured_at: str) -> None:
    key = str(observation["listing_id"])
    old = state["items"].get(key, {})
    samples = list(old.get("samples") or [])
    snapshot = {"captured_at": captured_at, **{field: observation.get(field) for field in ("quantity", "views", "favorites", "shop_sales")}}
    if samples and samples[-1].get("captured_at") == captured_at:
        samples[-1] = snapshot
    else:
        samples.append(snapshot)
    cutoff = now() - timedelta(days=KEEP_DAYS)
    samples = [item for item in samples if (parse_time(item.get("captured_at")) or now()) >= cutoff][-75:]
    state["items"][key] = {**old, **observation, "samples": samples, "last_seen_at": captured_at}
    recalculate_metrics(state, captured_at)


def prune(state: dict[str, Any]) -> None:
    cutoff = now() - timedelta(days=KEEP_DAYS)
    current = [item for item in state["items"].values() if (parse_time(item.get("last_seen_at")) or cutoff - timedelta(seconds=1)) >= cutoff]
    current.sort(key=lambda item: (float(item.get("hot_score") or 0), float(item.get("views_24h") or 0)), reverse=True)
    state["items"] = {str(item["listing_id"]): item for item in current[:MAX_ITEMS]}


def collect(keystring: str, secret: str, *, budget: RequestBudget | None = None) -> list[dict[str, Any]]:
    budget = budget if budget is not None else RequestBudget()
    found: dict[int, tuple[dict[str, Any], str, int]] = {}
    for keyword in KEYWORDS:
        response = get("/listings/active", keystring, secret, budget=budget, keywords=keyword, limit=50, sort_on="score", sort_order="desc", is_safe="true", currency="USD")
        for rank, item in enumerate(response.get("results", []), start=1):
            if item.get("listing_id"):
                found.setdefault(int(item["listing_id"]), (dict(item), keyword, rank))
    details: dict[int, dict[str, Any]] = {}
    ids = list(found)[:MAX_ITEMS]
    for start in range(0, len(ids), 100):
        response = get("/listings/batch", keystring, secret, budget=budget, listing_ids=",".join(map(str, ids[start:start + 100])), includes="Shop,Images", currency="USD")
        details.update({int(item["listing_id"]): item for item in response.get("results", []) if item.get("listing_id")})
    output: list[dict[str, Any]] = []
    for listing_id, (search_row, keyword, rank) in found.items():
        row = normalise({**search_row, **details.get(listing_id, {})}, keyword, rank)
        if row:
            output.append(row)
    return output


HTML = r"""<!doctype html>
<html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EtsyPulse</title>
<style>
:root{--bg:#090d16;--panel:#121a2a;--line:#283650;--text:#f3f7ff;--muted:#a7b4cf;--green:#6ee7b7;--blue:#8bd3ff;--yellow:#fcd34d;--pink:#f9a8d4}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#152445 0,var(--bg) 40%);color:var(--text);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif}main{max-width:1500px;margin:auto;padding:30px 18px 42px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.brand h1{margin:0;font-size:29px}.brand p,.muted{color:var(--muted)}.pill{display:inline-block;border:1px solid #35606d;background:#11313a;color:var(--green);padding:5px 9px;border-radius:999px;font-weight:700;font-size:12px}.cards{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:10px;margin:22px 0 14px}.card,.panel{background:rgba(18,26,42,.94);border:1px solid var(--line);border-radius:13px}.card{padding:14px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.value{font-size:25px;font-weight:800;color:var(--green);margin-top:4px}.panel{padding:15px}.tabs{display:flex;gap:8px;margin:14px 0}.tab,button,select,input{border:1px solid var(--line);background:#0c1321;color:var(--text);border-radius:8px;padding:9px 11px}.tab{cursor:pointer}.tab.active{background:#1b5360;border-color:#2998a8}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px}.filters input{min-width:260px;flex:1}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;min-width:1180px}th,td{padding:10px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;color:var(--muted);background:#101827;position:sticky;top:0;z-index:1}tr:hover td{background:#172339}.product{min-width:300px}.product a,a{color:var(--blue)}.sub{font-size:11px;color:var(--muted);margin-top:3px}.confirmed{color:var(--green);font-weight:800}.estimated{color:var(--yellow);font-weight:700}.ratio{color:var(--pink);font-weight:800}.badge{display:inline-block;padding:3px 6px;border:1px solid #3a4a69;border-radius:999px;font-size:11px;white-space:nowrap}.niche-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.niche{padding:15px}.niche h3{margin:0 0 4px;font-size:16px}.niche-metrics{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.niche-metrics b{display:block;color:var(--green);font-size:20px}.foot{margin-top:15px;color:var(--muted);font-size:12px}.view{display:none}.view.active{display:block}@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.niche-grid{grid-template-columns:1fr}}@media(max-width:600px){main{padding:20px 12px}.top{flex-direction:column}.cards{grid-template-columns:repeat(2,1fr)}.filters input{min-width:100%}}
</style>
<main><div class="top"><div class="brand"><h1>EtsyPulse</h1><p>Averixa market intelligence · public Etsy signals · <span id="updated">yükleniyor…</span></p></div><span class="pill">PC kapalıyken de çalışır</span></div>
<section class="cards" id="cards"></section>
<div class="panel"><div class="tabs"><button class="tab active" data-view="products">Ürün sinyalleri</button><button class="tab" data-view="niches">Niş özeti</button></div>
<section id="products" class="view active"><div class="filters"><input id="q" placeholder="Ürün veya anahtar kelime ara"><select id="sort"><option value="confirmed_sales_view_24h">Onaylı S/View</option><option value="confirmed_sales_24h">Onaylı satış</option><option value="estimated_sales_24h">Tahmini satış</option><option value="hot_score">Fırsat skoru</option><option value="views_24h">View artışı</option></select><select id="quality"><option value="all">Tüm kayıtlar</option><option value="ready">Ölçümü olanlar</option><option value="confirmed">Onaylı sinyaller</option></select><button id="download-csv" type="button">Tüm ilanları CSV indir</button></div><div class="sub">Toplam view = Etsy’deki ilan sayacı · View Δ = son iki tarama arasındaki artış · CSV filtrelerden bağımsız tüm takip ilanlarını indirir</div><div id="product-rows"></div></section>
<section id="niches" class="view"><div class="filters"><input id="nq" placeholder="Niş ara"></div><div class="niche-grid" id="niche-rows"></div></section></div>
<p class="foot"><b>Onaylı</b>: aynı ölçüm aralığında stok düşüşü, public mağaza satış sayacıyla desteklenmiş sinyal. <b>Tahmini</b>: public stok/view/favori sinyallerinden ihtiyatlı tahmin. Rakiplerin özel Etsy sipariş verisi değildir.</p></main>
<script>
const n=v=>Number(v||0), f=v=>v==null?'—':n(v).toLocaleString(undefined,{maximumFractionDigits:2}), pct=v=>v==null?'—':f(v)+'%', esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let products=[], niches=[];
function metric(v,cls=''){return `<span class="${cls}">${f(v)}</span>`}
function drawProducts(){const q=document.querySelector('#q').value.toLowerCase(),sort=document.querySelector('#sort').value,quality=document.querySelector('#quality').value;let rows=products.filter(r=>(r.title+' '+r.source_query).toLowerCase().includes(q)).filter(r=>quality==='all'||(quality==='ready'&&r.metrics_ready)||(quality==='confirmed'&&n(r.confirmed_sales_24h)>0)).sort((a,b)=>n(b[sort])-n(a[sort])).slice(0,150);document.querySelector('#product-rows').innerHTML=rows.length?`<div class="table-wrap"><table><tr><th>Ürün</th><th>Onaylı<br>24s</th><th>Tahmini<br>24s</th><th>İlanın toplam<br>view’ı</th><th>Son tarama<br>view Δ</th><th>Onaylı<br>S/View</th><th>Tahmini<br>S/View</th><th>Stok<br>hareketi</th><th>Fiyat</th><th>Güven</th><th>Dayanak</th></tr>${rows.map(r=>`<tr><td class="product"><a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.title)}</a><div class="sub"><span class="badge">${esc(r.source_query)}</span> · ${r.metrics_ready?f(r.comparison_hours)+'s ölçüm':'ilk ölçüm'}</div></td><td>${metric(r.confirmed_sales_24h,'confirmed')}</td><td>${metric(r.estimated_sales_24h,'estimated')}</td><td>${f(r.views)}</td><td>${f(r.views_24h)}</td><td class="ratio">${pct(r.confirmed_sales_view_24h)}</td><td class="ratio">${pct(r.estimated_sales_view_24h)}</td><td>${f(r.stock_movement_24h)}</td><td>${esc(r.currency||'')} ${f(r.price)}</td><td>${r.metrics_ready?'%'+f(r.confidence):'—'}</td><td><span class="badge">${esc(r.basis||'—')}</span></td></tr>`).join('')}</table></div>`:'<p class="muted">Bu filtrede kayıt yok.</p>'}
function downloadCsv(){const head=['listing_id','title','url','keyword','price','currency','search_rank','quantity','total_views','view_delta_24h','favorites','shop_sales','confirmed_sales_24h','estimated_sales_24h','stock_movement_24h','confidence','basis'];const quote=v=>'"'+String(v??'').replaceAll('"','""')+'"';const allRows=[...products].sort((a,b)=>n(b.hot_score)-n(a.hot_score));const body=allRows.map(r=>[r.listing_id,r.title,r.url,r.source_query,r.price,r.currency,r.search_rank,r.quantity,r.views,r.views_24h,r.favorites,r.shop_sales,r.confirmed_sales_24h,r.estimated_sales_24h,r.stock_movement_24h,r.confidence,r.basis]);const csv='\ufeff'+[head,...body].map(row=>row.map(quote).join(';')).join('\r\n');const file=new Blob([csv],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(file),link=document.createElement('a');link.href=url;link.download='etsypulse-all-listings-'+new Date().toISOString().slice(0,10)+'.csv';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function drawNiches(){const q=document.querySelector('#nq').value.toLowerCase(),rows=niches.filter(x=>x.niche_name.toLowerCase().includes(q));document.querySelector('#niche-rows').innerHTML=rows.length?rows.map(x=>`<article class="panel niche"><h3>${esc(x.niche_name)}</h3><div class="muted">${f(x.comparable)} / ${f(x.observed_listing_count)} ölçülebilir ilan · örnek: <a href="${esc(x.sample_url||'#')}" target="_blank" rel="noreferrer">${esc(x.sample_title||'—')}</a></div><div class="niche-metrics"><div><span class="label">Onaylı 24s</span><b>${f(x.confirmed_sales_24h)}</b></div><div><span class="label">Tahmini 24s</span><b>${f(x.estimated_sales_24h)}</b></div><div><span class="label">S/View</span><b>${pct(x.confirmed_sales_view_24h)}</b></div><div><span class="label">Fırsat</span><b>${f(x.opportunity_score)}</b></div></div></article>`).join(''):'<p class="muted">Bu filtrede niş yok.</p>'}
const cacheBust='?v='+Date.now();Promise.all(['data/status.json','data/radar.json','data/niches.json'].map(x=>fetch(x+cacheBust,{cache:'no-store'}).then(r=>r.json()))).then(([s,r,ns])=>{products=r;niches=ns;document.querySelector('#updated').textContent=(s.generated_at?'son ölçüm: '+new Date(s.generated_at).toLocaleString('tr-TR'):'henüz ölçüm yok')+(s.collection_message?' · '+s.collection_message:'');document.querySelector('#cards').innerHTML=[['Takipteki ilan',s.listings],['Ölçümü olan',s.comparable],['Toplam view',s.total_views],['Onaylı satış · 24s',s.confirmed_sales_24h],['Tahmini satış · 24s',s.estimated_sales_24h],['En iyi onaylı S/View',pct(s.top_confirmed_sales_view_24h)],['Niş',s.keywords]].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${v}</div></div>`).join('');drawProducts();drawNiches();document.querySelector('#q').oninput=drawProducts;document.querySelector('#sort').onchange=drawProducts;document.querySelector('#quality').onchange=drawProducts;document.querySelector('#download-csv').onclick=downloadCsv;document.querySelector('#nq').oninput=drawNiches;document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelector('#'+b.dataset.view).classList.add('active')})});
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
    rows = sorted(state["items"].values(), key=lambda item: (float(item.get("hot_score") or 0), float(item.get("estimated_sales_24h") or 0)), reverse=True)
    ready = [row for row in rows if row.get("metrics_ready")]
    ratios = [float(row["confirmed_sales_view_24h"]) for row in ready if row.get("confirmed_sales_view_24h") is not None]
    status = {
        "generated_at": state.get("last_success_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "collection_status": state.get("collection_status", "ok"),
        "collection_message": state.get("collection_message"),
        "api_usage": state.get("api_usage", {}),
        "listings": len(rows),
        "comparable": len(ready),
        "keywords": len(KEYWORDS),
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the free static Averixa Market Radar")
    parser.add_argument("--state", type=Path, default=Path("state/radar_state.json"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args()
    keystring, secret = os.getenv("ETSY_KEYSTRING", "").strip(), os.getenv("ETSY_SHARED_SECRET", "").strip()
    if not keystring or not secret:
        raise SystemExit("ETSY_KEYSTRING and ETSY_SHARED_SECRET GitHub secrets are required.")
    state, captured_at = load_state(args.state), now().isoformat()
    budget = RequestBudget()
    retry_at = parse_time(state.get("api_usage", {}).get("retry_at"))
    state["last_attempt_at"] = captured_at
    try:
        if retry_at is not None and retry_at > now():
            budget.retry_at = retry_at.isoformat()
            raise QuotaDeferred("Etsy quota pause still active; saved snapshot preserved.")
        observations = collect(keystring, secret, budget=budget)
    except QuotaDeferred as exc:
        state["collection_status"] = "quota_deferred"
        state["collection_message"] = "Kota nedeniyle tarama ertelendi; son başarılı ölçüm gösteriliyor."
        print(str(exc))
    else:
        for row in observations:
            merge(state, row, captured_at)
        recalculate_metrics(state, captured_at)
        state["last_success_at"] = captured_at
        state["collection_status"] = "ok"
        state["collection_message"] = None
        prune(state)
    state["api_usage"] = budget.summary()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_site(state, args.site)
    print(json.dumps({"listings": len(state["items"]), "generated_at": state["last_success_at"], "collection_status": state["collection_status"], "api_usage": state["api_usage"]}))


if __name__ == "__main__":
    main()
