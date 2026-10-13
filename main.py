"""IranStockAI: conservative, source-labelled Iranian market data API.

Market data is never fabricated. Unknown history/fundamentals produce INSUFFICIENT DATA.
"""
import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse
import xml.etree.ElementTree as ET

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_NAME = "IranStockAI — شکارچی فرصت بورس ایران"
VERSION = "1.0.0"
FRESH_SECONDS = max(15, int(os.getenv("MAX_DATA_AGE_SECONDS", "180")))
CACHE_SECONDS = max(5, int(os.getenv("MARKET_CACHE_SECONDS", "20")))
TIMEOUT_SECONDS = max(2, min(15, float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "8"))))
MARKETWATCH_QUERY = "?market=0&paperTypes[0]=1&paperTypes[1]=2&paperTypes[2]=3&paperTypes[3]=4&paperTypes[4]=5&paperTypes[5]=6&paperTypes[6]=7&paperTypes[7]=8&paperTypes[8]=9&withBestLimits=false&hEven=0&RefID=0"
MARKETWATCH_URLS = [
    os.getenv("TSETMC_MARKETWATCH_URL", "https://webgw.tse.ir/InstrumentProvider/api/v1/MarketWatch/MarketWatchCash/fa"),
    "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch" + MARKETWATCH_QUERY,
    "https://cdn10.tsetmc.com/api/ClosingPrice/GetMarketWatch" + MARKETWATCH_QUERY,
]
LEGACY_MARKETWATCH_URL = "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0"
UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,text/csv,text/html,*/*",
    "Referer": "https://www.tsetmc.com/",
    "Origin": "https://www.tsetmc.com",
}
app = FastAPI(title=APP_NAME, version=VERSION, description="Market data API; missing data is explicitly reported.")
app.mount("/static", StaticFiles(directory="static"), name="static")

_cache: dict[str, Any] = {"at": None, "data": None, "source": None, "error": None}
_refresh_lock = asyncio.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick(d: dict, *keys, default=None):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    lower = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def scalar(value):
    if isinstance(value, dict):
        for key in ("value", "Value", "lastValue", "amount"):
            if key in value:
                return value[key]
    return value


def norm_num(value):
    try:
        value = scalar(value)
        if value is None or value == "":
            return None
        number = float(str(value).replace(",", "").strip())
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (ValueError, TypeError, OverflowError):
        return None


def find_rows(obj):
    keys_of_interest = {"symbol", "lval18afc", "inscode", "instrumentid", "isin", "instrumentname", "instrument_name", "companynamepersian"}
    if isinstance(obj, list):
        if obj and any(isinstance(x, dict) and keys_of_interest.intersection(str(k).lower() for k in x) for x in obj[:10]):
            return obj
        for item in obj:
            found = find_rows(item)
            if found:
                return found
    elif isinstance(obj, dict):
        preferred = {"instrumentinfos", "marketwatch", "marketwatchcash", "data", "items", "instruments", "instrumentlist"}
        for key, value in obj.items():
            if str(key).lower() in preferred:
                found = find_rows(value)
                if found:
                    return found
        for value in obj.values():
            found = find_rows(value)
            if found:
                return found
    return []


def normalize(row: dict):
    symbol = scalar(pick(row, "symbol", "lval18aFC", "lval18a", "ticker", "shortName", "instrumentName", "instrument_Name", "instrumentname"))
    if not isinstance(symbol, (str, int)) or not str(symbol).strip():
        return None
    symbol = str(symbol).strip()
    last = norm_num(pick(row, "last_price", "lastPrice", "lastprice", "pDrCotVal", "pl", "last"))
    close = norm_num(pick(row, "adj_closing_price", "closingPrice", "closingprice", "pClosing", "pc", "close"))
    prev = norm_num(pick(row, "yesterday_price", "yesterdayPrice", "yesterdayprice", "priceYesterday", "py"))
    volume = norm_num(pick(row, "volume_of_trans", "volume", "tradeVolume", "tradevolume", "qTotTran5J", "vol"))
    value = norm_num(pick(row, "value_of_trans", "value", "tradeValue", "tradevalue", "qTotCap"))
    eps = norm_num(pick(row, "EPS", "eps", "estimatedEPS"))
    pe = norm_num(pick(row, "P/E", "pe", "pE", "priceEarningRatio"))
    change = ((last / prev) - 1) * 100 if last is not None and prev is not None and last >= 0 and prev > 0 else None
    ib = norm_num(pick(row, "individual_buy_vol", "individualBuyVolume", "vol_individual_buy", "BuyVolumeIndividual"))
    ise = norm_num(pick(row, "individual_sell_vol", "individualSellVolume", "vol_individual_sell", "SellVolumeIndividual"))
    cb = norm_num(pick(row, "corporate_buy_vol", "corporateBuyVolume", "vol_corporate_buy", "BuyVolumeCorporate"))
    cs = norm_num(pick(row, "corporate_sell_vol", "corporateSellVolume", "vol_corporate_sell", "SellVolumeCorporate"))
    name = scalar(pick(row, "name", "lval30", "companyName", "companyNamePersian", default=""))
    return {
        "symbol": symbol, "name": str(name or "").strip(),
        "inscode": str(pick(row, "insCode", "inscode", "instrumentId", "instrumentid", default="") or "").strip() or None,
        "last": last if last is not None and last >= 0 else None,
        "close": close if close is not None and close >= 0 else None,
        "previous_close": prev if prev is not None and prev > 0 else None,
        "change_pct": change, "volume": volume if volume is not None and volume >= 0 else None,
        "value": value if value is not None and value >= 0 else None,
        "eps": eps, "pe": pe,
        "individual_net_volume": ib - ise if ib is not None and ise is not None else None,
        "corporate_net_volume": cb - cs if cb is not None and cs is not None else None,
        "flow_available": all(v is not None for v in (ib, ise, cb, cs)),
        "fundamental_status": "INSUFFICIENT DATA",
        "signal": "INSUFFICIENT DATA",
        "signal_reason": "تاریخچه معتبر و داده بنیادی/جریان معتبر کافی نیست؛ سیگنال صادر نشد.",
    }


def parse_legacy_marketwatch(raw: str) -> list[dict]:
    parts = raw.split("@")
    if len(parts) < 3:
        return []
    result = []
    for line in parts[2].split(";"):
        cols = line.strip().split(",")
        if len(cols) < 15:
            continue
        item = normalize({"symbol": cols[2], "name": cols[3], "pClosing": cols[6],
                          "pDrCotVal": cols[7], "qTotTran5J": cols[9], "qTotCap": cols[10],
                          "priceYesterday": cols[13], "eps": cols[14]})
        if item:
            result.append(item)
    return result


def parse_market_response(url: str, response: httpx.Response) -> list[dict]:
    if url == LEGACY_MARKETWATCH_URL:
        rows = parse_legacy_marketwatch(response.text)
    else:
        payload = response.json()
        raw_rows = find_rows(payload)
        rows = [item for item in (normalize(row) for row in raw_rows if isinstance(row, dict)) if item]
    # A response with a schema mismatch is not a healthy source.
    seen, unique = set(), []
    for item in rows:
        key = item["symbol"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def public_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "UPSTREAM_TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"UPSTREAM_HTTP_{exc.response.status_code}"
    if isinstance(exc, (ValueError, TypeError)):
        return "UPSTREAM_INVALID_RESPONSE"
    if isinstance(exc, httpx.HTTPError):
        return "UPSTREAM_CONNECTION_ERROR"
    return "UPSTREAM_ERROR"


async def get_market_data():
    now = time.time()
    if _cache["data"] is not None and _cache["at"] is not None and now - _cache["at"] < CACHE_SECONDS and not _cache["error"]:
        return _cache["data"], _cache["source"], None, "LIVE"
    async with _refresh_lock:
        now = time.time()
        if _cache["data"] is not None and _cache["at"] is not None and now - _cache["at"] < CACHE_SECONDS and not _cache["error"]:
            age = now - _cache["at"]
            return _cache["data"], _cache["source"], None, "LIVE" if age <= FRESH_SECONDS else "STALE"
        errors = []
        timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=min(4.0, TIMEOUT_SECONDS))
        headers = UPSTREAM_HEADERS
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            for url in [*MARKETWATCH_URLS, LEGACY_MARKETWATCH_URL]:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    items = parse_market_response(url, response)
                    if not items:
                        errors.append("UPSTREAM_EMPTY_OR_UNKNOWN_SCHEMA")
                        continue
                    received_at = utc_now()
                    result = {"items": items, "received_at": received_at, "count": len(items)}
                    _cache.update({"at": time.time(), "data": result, "source": url, "error": None})
                    return result, url, None, "LIVE"
                except Exception as exc:
                    errors.append(public_error(exc))
        # Never pretend the cache is live after a failed refresh.
        _cache["error"] = ";".join(dict.fromkeys(errors)) or "UPSTREAM_UNAVAILABLE"
        if _cache["data"] is not None and _cache["at"] is not None:
            age = max(0, int(time.time() - _cache["at"]))
            return _cache["data"], _cache["source"], _cache["error"], "STALE"
        return None, None, _cache["error"], "OFFLINE"


@app.get("/")
async def home():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    # Liveness only: upstream outages should not cause Render restart loops.
    return {"ok": True, "app": "IranStockAI", "version": VERSION, "utc": utc_now()}


@app.get("/api/status")
async def status():
    age = max(0, int(time.time() - _cache["at"])) if _cache["at"] is not None else None
    data = _cache["data"]
    if data is None:
        state = "OFFLINE"
    elif _cache["error"] or age is None or age > FRESH_SECONDS:
        state = "STALE"
    else:
        state = "LIVE"
    return {
        "status": state, "market_data_available": data is not None,
        "count": data["count"] if data else 0, "source": _cache["source"],
        "age_seconds": age, "max_age_seconds": FRESH_SECONDS,
        "last_received_at": data["received_at"] if data else None,
        "error": _cache["error"], "signal_policy": "fail-closed",
    }


@app.get("/api/diagnostics")
async def diagnostics():
    results = []
    timeout = httpx.Timeout(min(5.0, TIMEOUT_SECONDS), connect=min(3.0, TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
        for url in [*MARKETWATCH_URLS, LEGACY_MARKETWATCH_URL]:
            entry = {"host": urlparse(url).hostname, "ok": False, "http_status": None, "symbols": 0, "error": None}
            try:
                response = await client.get(url)
                entry["http_status"] = response.status_code
                response.raise_for_status()
                items = parse_market_response(url, response)
                entry["symbols"] = len(items)
                entry["ok"] = bool(items)
                if not items:
                    entry["error"] = "EMPTY_OR_UNKNOWN_SCHEMA"
            except Exception as exc:
                entry["error"] = public_error(exc)
            results.append(entry)
    good = next((item for item in results if item["ok"]), None)
    return {"app": "IranStockAI", "checked_at": utc_now(), "market_data_source_working": bool(good),
            "working_source": good["host"] if good else None, "symbol_count": good["symbols"] if good else 0,
            "results": results, "note": "Checks are performed from this server; no sample data is used."}


@app.get("/api/market")
async def market(q: str = Query(default="", max_length=100), limit: int = Query(default=200, ge=1, le=500)):
    data, source, warning, state = await get_market_data()
    if data is None:
        return JSONResponse(status_code=503, content={"status": "OFFLINE", "items": [], "count": 0,
                            "error": warning, "message": "داده زنده دریافت نشد؛ داده ساختگی نمایش داده نمی‌شود."})
    items = data["items"]
    query = q.strip().casefold()
    if query:
        items = [item for item in items if query in item["symbol"].casefold() or query in item["name"].casefold()]
    return {"status": state, "source": urlparse(source).hostname if source else None,
            "received_at": data["received_at"], "count": len(items), "items": items[:limit], "warning": warning}


@app.get("/api/scan")
async def scan():
    data, source, warning, state = await get_market_data()
    if data is None:
        return JSONResponse(status_code=503, content={"status": "OFFLINE", "candidates": [], "count": 0,
                            "message": "اسکن متوقف شد؛ منبع داده در دسترس نیست."})
    candidates = [item for item in data["items"] if item["change_pct"] is not None and -5 <= item["change_pct"] < 0]
    candidates.sort(key=lambda item: item["change_pct"], reverse=True)
    return {"status": state, "source": urlparse(source).hostname if source else None,
            "received_at": data["received_at"], "count": len(candidates), "candidates": candidates[:100],
            "notice": "غربال تغییرات قیمت، سیگنال خرید نیست. تاریخچه کافی و اعتبارسنجی بنیادی موجود نیست.",
            "warning": warning}


@app.get("/api/quote/{symbol}")
async def quote(symbol: str):
    data, source, warning, state = await get_market_data()
    if data is None:
        raise HTTPException(status_code=503, detail={"status": "OFFLINE", "message": "داده بازار در دسترس نیست."})
    match = next((item for item in data["items"] if item["symbol"].casefold() == symbol.strip().casefold()), None)
    if match is None:
        raise HTTPException(status_code=404, detail={"status": "INSUFFICIENT DATA", "message": "نماد در فهرست دریافت‌شده پیدا نشد."})
    return {"status": state, "source": urlparse(source).hostname if source else None,
            "received_at": data["received_at"], "item": match, "warning": warning}



async def request_json(client: httpx.AsyncClient, url: str):
    response = await client.get(url)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        # Codal search sometimes responds as XML even when JSON is requested.
        root = ET.fromstring(response.text)
        def node_value(node):
            children = list(node)
            if not children:
                return (node.text or "").strip()
            out = {}
            for child in children:
                value = node_value(child)
                if child.tag in out:
                    if not isinstance(out[child.tag], list):
                        out[child.tag] = [out[child.tag]]
                    out[child.tag].append(value)
                else:
                    out[child.tag] = value
            return out
        return node_value(root)


async def resolve_instrument(client: httpx.AsyncClient, symbol: str):
    url = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/" + quote(symbol, safe="")
    payload = await request_json(client, url)
    rows = find_rows(payload)
    for row in rows:
        found_symbol = str(pick(row, "lVal18AFC", "symbol", "symbolName", default="") or "").strip()
        inscode = pick(row, "insCode", "inscode", "instrumentId", "instrumentid")
        if inscode and (not found_symbol or found_symbol.casefold() == symbol.casefold()):
            return str(inscode), row
    # Some responses are a single object or use a differently named collection.
    if isinstance(payload, dict):
        for key in ("instrumentSearch", "instrumentsearch", "instrumentInfo", "instrumentinfo"):
            value = payload.get(key)
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        inscode = pick(row, "insCode", "inscode")
                        if inscode:
                            return str(inscode), row
    return None, None


@app.get("/api/codal/{symbol}")
async def codal_reports(symbol: str, limit: int = Query(default=20, ge=1, le=100)):
    """Fetch actual public disclosures; prefer Codal search API, then TSETMC Codal proxy."""
    timeout = httpx.Timeout(min(10.0, TIMEOUT_SECONDS), connect=min(4.0, TIMEOUT_SECONDS))
    errors = []
    headers = {**UPSTREAM_HEADERS, "Accept": "application/json, text/xml, application/xml, */*"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        params = {"Symbol": symbol, "PageNumber": 1, "search": "true"}
        direct = "https://search.codal.ir/api/search/v2/q?" + urlencode(params)
        try:
            payload = await request_json(client, direct)
            if payload:
                return {"status": "LIVE", "source": "search.codal.ir", "symbol": symbol,
                        "received_at": utc_now(), "data": payload, "warning": None}
            errors.append("CODAL_EMPTY_RESPONSE")
        except Exception as exc:
            errors.append(public_error(exc))
        try:
            inscode, instrument = await resolve_instrument(client, symbol)
            if not inscode:
                raise ValueError("INSTRUMENT_NOT_FOUND")
            proxy = f"https://cdn.tsetmc.com/api/Codal/GetPreparedDataByInsCode/{limit}/{quote(inscode, safe='')}"
            payload = await request_json(client, proxy)
            if payload:
                return {"status": "LIVE", "source": "cdn.tsetmc.com/api/Codal", "symbol": symbol,
                        "inscode": inscode, "instrument": instrument, "received_at": utc_now(),
                        "data": payload, "warning": "TSETMC proxy metadata; full report files may require Codal access"}
            errors.append("TSETMC_CODAL_PROXY_EMPTY")
        except Exception as exc:
            errors.append(public_error(exc))
    return JSONResponse(status_code=503, content={"status": "OFFLINE", "symbol": symbol, "data": [],
                        "error": ";".join(dict.fromkeys(errors)), "message": "اطلاعیه‌های واقعی کدال از سرور دریافت نشد؛ داده ساختگی نمایش داده نمی‌شود."})


@app.get("/api/analysis/{symbol}")
async def analysis_data(symbol: str, history_limit: int = Query(default=250, ge=20, le=5000)):
    """Fetch raw market history, investor flow and order-book data for downstream analysis."""
    timeout = httpx.Timeout(min(12.0, TIMEOUT_SECONDS), connect=min(4.0, TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
        try:
            inscode, instrument = await resolve_instrument(client, symbol)
        except Exception as exc:
            return JSONResponse(status_code=503, content={"status": "OFFLINE", "symbol": symbol,
                                "error": public_error(exc), "message": "نماد از منبع واقعی بازار شناسایی نشد."})
        if not inscode:
            return JSONResponse(status_code=404, content={"status": "INSUFFICIENT DATA", "symbol": symbol,
                                "message": "کد یکتای نماد از منبع بازار دریافت نشد."})
        urls = {
            "history": f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{quote(inscode, safe='')}/{history_limit}",
            "investor_flow": f"https://cdn.tsetmc.com/api/ClientType/GetClientTypeHistory/{quote(inscode, safe='')}",
            "order_book": f"https://cdn.tsetmc.com/api/BestLimits/{quote(inscode, safe='')}",
            "instrument_info": f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/{quote(inscode, safe='')}",
        }
        async def fetch_one(url):
            try:
                return {"ok": True, "data": await request_json(client, url), "error": None}
            except Exception as exc:
                return {"ok": False, "data": None, "error": public_error(exc)}
        names = list(urls)
        results = await asyncio.gather(*(fetch_one(urls[name]) for name in names))
        parts = dict(zip(names, results))
        ok_count = sum(1 for item in results if item["ok"])
        return {"status": "LIVE" if ok_count == len(results) else ("PARTIAL" if ok_count else "OFFLINE"),
                "symbol": symbol, "inscode": inscode, "instrument": instrument, "source": "cdn.tsetmc.com",
                "received_at": utc_now(), "history_limit": history_limit, "data": parts,
                "available_components": [name for name, item in parts.items() if item["ok"]],
                "unavailable_components": {name: item["error"] for name, item in parts.items() if not item["ok"]},
                "note": "Raw source data only; indicators/signals must not be claimed unless computed and validated from these records."}


@app.get("/api/data-sources")
async def data_sources():
    """Advertise data connectors and their current verification status without claiming they are live."""
    return {"app": "IranStockAI", "checked_at": utc_now(), "connectors": [
        {"name": "TSETMC MarketWatch", "kind": "market prices", "endpoint": "/api/market", "status": "verified only by live diagnostics"},
        {"name": "TSETMC historical prices, investor flow, order book, instrument info", "kind": "analysis inputs", "endpoint": "/api/analysis/{symbol}", "status": "tested on request; may be geo-blocked"},
        {"name": "Codal public disclosures", "kind": "company filings", "endpoint": "/api/codal/{symbol}", "status": "tested on request; may be geo-blocked"},
        {"name": "Mofid brokerage", "kind": "broker account", "status": "not connected; no official API credentials supplied"},
    ], "policy": "No fabricated data; unavailable sources return OFFLINE or INSUFFICIENT DATA."}


@app.get("/api/history/{symbol}")
async def history(symbol: str):
    return {"symbol": symbol, "status": "INSUFFICIENT DATA", "items": [],
            "message": "منبع تاریخچه معتبر به این نماد متصل نشده است؛ نمودار و شاخص تاریخی تولید نمی‌شود."}


@app.get("/api/fundamentals/{symbol}")
async def fundamentals(symbol: str):
    return {"symbol": symbol, "status": "INSUFFICIENT DATA", "source": None, "items": [],
            "message": "اتصال و اعتبارسنجی داده بنیادی کدال هنوز تأیید نشده است؛ مقدار بنیادی فرضی ارائه نمی‌شود."}


@app.get("/api/signals/{symbol}")
async def signals(symbol: str):
    data, source, warning, state = await get_market_data()
    item = next((x for x in data["items"] if x["symbol"].casefold() == symbol.strip().casefold()), None) if data else None
    if item is None:
        return {"symbol": symbol, "status": "INSUFFICIENT DATA" if data else "OFFLINE",
                "signal": "INSUFFICIENT DATA", "reason": "قیمت مرجع معتبر برای این نماد موجود نیست.",
                "generated_at": utc_now(), "reference_price": None, "invalidation": None, "source": None}
    return {"symbol": item["symbol"], "status": "INSUFFICIENT DATA", "signal": "INSUFFICIENT DATA",
            "reason": "داده روزانه به‌تنهایی برای سیگنال کافی نیست؛ تاریخچه معتبر و آزمون‌شده در دسترس نیست.",
            "generated_at": utc_now(), "reference_price": item["last"], "reference_received_at": data["received_at"],
            "source": urlparse(source).hostname if source else None, "invalidation": None, "warning": warning}


@app.on_event("startup")
async def validate_static_assets():
    # Keep startup failure explicit if the app package is incomplete.
    if not os.path.isfile(os.path.join("static", "index.html")):
        raise RuntimeError("Required static/index.html is missing")
