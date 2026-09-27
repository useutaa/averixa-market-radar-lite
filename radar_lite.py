"""Standalone free Market Radar collector for scheduled GitHub Actions runs.

Only compact public Etsy observations are kept in a JSON file.  API credentials
come exclusively from environment variables and are never serialized.
"""
from __future__ import annotations

import argparse
import html
import json
import os
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


class RadarError(RuntimeError):
    """Safe-to-print error that contains no headers or API secrets."""


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


def get(path: str, keystring: str, secret: str, **params: Any) -> dict[str, Any]:
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
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RadarError("Etsy rejected the configured credentials.") from exc
        if exc.code == 429:
            raise RadarError("Etsy quota/rate limit reached; a later scheduled run will retry.") from exc
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


def merge(state: dict[str, Any], observation: dict[str, Any], captured_at: str) -> None:
    key = str(observation["listing_id"])
    old = state["items"].get(key, {})
    samples = list(old.get("samples") or [])
    quantity_delta = 0
    estimate = 0.0
    if samples:
        previous = samples[-1]
        before, after = number(previous.get("quantity")), number(observation.get("quantity"))
        before_at, after_at = parse_time(previous.get("captured_at")), parse_time(captured_at)
        if before is not None and after is not None and before_at and after_at:
            hours = (after_at - before_at).total_seconds() / 3600
            if 2 <= hours <= 36:
                quantity_delta = max(int(before - after), 0)
                estimate = round(quantity_delta * 24 / hours, 2)
    samples.append({"captured_at": captured_at, **{field: observation.get(field) for field in ("quantity", "views", "favorites", "shop_sales")}})
    cutoff = now() - timedelta(days=KEEP_DAYS)
    samples = [item for item in samples if (parse_time(item.get("captured_at")) or now()) >= cutoff][-75:]
    state["items"][key] = {**old, **observation, "samples": samples, "last_seen_at": captured_at, "quantity_delta": quantity_delta, "estimated_sales_24h": estimate}


def prune(state: dict[str, Any]) -> None:
    cutoff = now() - timedelta(days=KEEP_DAYS)
    current = [item for item in state["items"].values() if (parse_time(item.get("last_seen_at")) or cutoff - timedelta(seconds=1)) >= cutoff]
    current.sort(key=lambda item: (float(item.get("estimated_sales_24h") or 0), float(item.get("views") or 0)), reverse=True)
    state["items"] = {str(item["listing_id"]): item for item in current[:MAX_ITEMS]}


def collect(keystring: str, secret: str) -> list[dict[str, Any]]:
    found: dict[int, tuple[dict[str, Any], str, int]] = {}
    for keyword in KEYWORDS:
        response = get("/listings/active", keystring, secret, keywords=keyword, limit=50, sort_on="score", sort_order="desc", is_safe="true", currency="USD")
        for rank, item in enumerate(response.get("results", []), start=1):
            if item.get("listing_id"):
                found.setdefault(int(item["listing_id"]), (dict(item), keyword, rank))
        time.sleep(0.25)
    details: dict[int, dict[str, Any]] = {}
    ids = list(found)[:MAX_ITEMS]
    for start in range(0, len(ids), 100):
        response = get("/listings/batch", keystring, secret, listing_ids=",".join(map(str, ids[start:start + 100])), includes="Shop,Images", currency="USD")
        details.update({int(item["listing_id"]): item for item in response.get("results", []) if item.get("listing_id")})
        time.sleep(0.25)
    output: list[dict[str, Any]] = []
    for listing_id, (search_row, keyword, rank) in found.items():
        row = normalise({**search_row, **details.get(listing_id, {})}, keyword, rank)
        if row:
            output.append(row)
    return output


HTML = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Averixa Market Radar</title><style>body{margin:0;background:#0a0d16;color:#f4f7ff;font:15px system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:32px 18px}.sub,.muted{color:#aab5cf}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0}.card,.panel{background:#121827;border:1px solid #28334d;border-radius:14px;padding:16px}.value{font-size:28px;color:#6ee7b7;font-weight:700}.label{color:#aab5cf;font-size:12px;text-transform:uppercase}table{width:100%;border-collapse:collapse;min-width:720px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #28334d}th{color:#aab5cf;font-size:12px}.panel{overflow:auto}a{color:#8bd3ff}input{padding:10px;width:min(420px,100%);background:#0b1120;color:white;border:1px solid #28334d;border-radius:8px}@media(max-width:650px){.cards{grid-template-columns:1fr}}</style><main><h1>Averixa Market Radar</h1><p class="sub">Free Lite Cloud · periodic public-market snapshot · <span id="updated">Loading…</span></p><section class="cards" id="cards"></section><section class="panel"><h2>Top observed opportunities</h2><input id="q" placeholder="Filter title or keyword"><div id="rows"></div></section><p class="muted">Quantity movement is an estimate from public listing observations; it is not private Etsy order data.</p></main><script>const n=v=>Number(v||0),f=v=>n(v).toLocaleString(undefined,{maximumFractionDigits:2}),e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let rows=[];function draw(){let q=document.querySelector('#q').value.toLowerCase(),x=rows.filter(r=>(r.title+' '+r.source_query).toLowerCase().includes(q)).slice(0,120);document.querySelector('#rows').innerHTML=x.length?`<table><tr><th>Listing</th><th>Keyword</th><th>Est. quantity movement / 24h</th><th>Views</th><th>Price</th></tr>${x.map(r=>`<tr><td><a href="${e(r.url)}" target="_blank" rel="noreferrer">${e(r.title)}</a></td><td>${e(r.source_query)}</td><td>${f(r.estimated_sales_24h)}</td><td>${f(r.views)}</td><td>${e(r.currency||'')} ${f(r.price)}</td></tr>`).join('')}</table>`:'<p class="muted">No comparable observations yet. The first run builds the baseline.</p>'}Promise.all(['data/status.json','data/radar.json'].map(x=>fetch(x).then(r=>r.json()))).then(([s,r])=>{rows=r;document.querySelector('#updated').textContent='Last run: '+new Date(s.generated_at).toLocaleString();document.querySelector('#cards').innerHTML=[['Tracked listings',s.listings],['Comparable listings',s.comparable],['Keywords',s.keywords]].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${f(v)}</div></div>`).join('');document.querySelector('#q').oninput=draw;draw()})</script></html>"""


def write_site(state: dict[str, Any], site: Path) -> None:
    data = site / "data"
    data.mkdir(parents=True, exist_ok=True)
    rows = sorted(state["items"].values(), key=lambda item: (float(item.get("estimated_sales_24h") or 0), float(item.get("views") or 0)), reverse=True)
    status = {"generated_at": state.get("last_success_at"), "listings": len(rows), "comparable": sum(bool(row.get("estimated_sales_24h")) for row in rows), "keywords": len(KEYWORDS)}
    (site / "index.html").write_text(HTML, encoding="utf-8")
    (data / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (data / "radar.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the free static Averixa Market Radar")
    parser.add_argument("--state", type=Path, default=Path("state/radar_state.json"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args()
    keystring, secret = os.getenv("ETSY_KEYSTRING", "").strip(), os.getenv("ETSY_SHARED_SECRET", "").strip()
    if not keystring or not secret:
        raise SystemExit("ETSY_KEYSTRING and ETSY_SHARED_SECRET GitHub secrets are required.")
    state, captured_at = load_state(args.state), now().isoformat()
    for row in collect(keystring, secret):
        merge(state, row, captured_at)
    state["last_success_at"] = captured_at
    prune(state)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_site(state, args.site)
    print(json.dumps({"listings": len(state["items"]), "generated_at": captured_at}))


if __name__ == "__main__":
    main()
