import os, time
from datetime import datetime, timezone
from typing import Any
import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

APP_NAME = "شکارچی فرصت بورس ایران"
MARKETWATCH_URLS = [
    os.getenv("TSETMC_MARKETWATCH_URL", "https://webgw.tse.ir/InstrumentProvider/api/v1/MarketWatch/MarketWatchCash/fa"),
    "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch?market=0&paperTypes[0]=1&paperTypes[1]=2&paperTypes[2]=3&paperTypes[3]=4&paperTypes[4]=5&paperTypes[5]=6&paperTypes[6]=7&paperTypes[7]=8&paperTypes[8]=9&withBestLimits=false&hEven=0&RefID=0"
]
FRESH_SECONDS = int(os.getenv("MAX_DATA_AGE_SECONDS", "180"))
app = FastAPI(title=APP_NAME, version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
_cache: dict[str, Any] = {"at": 0, "data": None, "source": None, "error": None}

def pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return default

def find_rows(obj):
    """Find plausible instrument arrays without assuming one upstream JSON schema."""
    if isinstance(obj, list):
        if obj and any(isinstance(x, dict) and any(k in {str(a).lower() for a in x} for k in ("symbol","lval18afc","inscode","instrumentid","isin","instrumentname","instrument_name","companynamepersian")) for x in obj[:10]):
            return obj
        for item in obj:
            found = find_rows(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for key in ("instrumentinfos", "marketwatch", "marketwatchcash", "data", "items", "instruments", "instrumentlist"):
            if key in {str(k).lower() for k in obj}:
                for k,v in obj.items():
                    if str(k).lower() == key:
                        found = find_rows(v)
                        if found: return found
        for v in obj.values():
            found = find_rows(v)
            if found: return found
    return []

def scalar(v):
    # TSE gateway often wraps numbers as {"value": ..., "state": ...}.
    if isinstance(v, dict):
        for key in ("value", "Value", "lastValue", "amount"):
            if key in v:
                return v[key]
    return v

def norm_num(v):
    try:
        v = scalar(v)
        if v is None or v == "": return None
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None

def normalize(row):
    sym = pick(row, "symbol", "lval18aFC", "lval18a", "ticker", "shortName", "Symbol", "instrumentName", "instrument_Name", "instrumentname")
    if isinstance(sym, dict): sym = scalar(sym)
    if not sym: return None
    last = norm_num(pick(row, "last_price", "lastPrice", "lastprice", "pDrCotVal", "pl", "last"))
    close = norm_num(pick(row, "adj_closing_price", "closingPrice", "closingprice", "pClosing", "pc", "close"))
    prev = norm_num(pick(row, "yesterday_price", "yesterdayPrice", "yesterdayprice", "priceYesterday", "py"))
    vol = norm_num(pick(row, "volume_of_trans", "volume", "tradeVolume", "tradevolume", "qTotTran5J", "vol"))
    value = norm_num(pick(row, "value_of_trans", "value", "tradeValue", "tradevalue", "qTotCap"))
    eps = norm_num(pick(row, "EPS", "eps", "estimatedEPS"))
    pe = norm_num(pick(row, "P/E", "pe", "pE", "priceEarningRatio"))
    change = ((last / prev) - 1) * 100 if last and prev else None
    # Flow fields are included only if upstream actually supplies them.
    ind_buy = norm_num(pick(row, "individual_buy_vol", "individualBuyVolume", "vol_individual_buy", "BuyVolumeIndividual"))
    ind_sell = norm_num(pick(row, "individual_sell_vol", "individualSellVolume", "vol_individual_sell", "SellVolumeIndividual"))
    corp_buy = norm_num(pick(row, "corporate_buy_vol", "corporateBuyVolume", "vol_corporate_buy", "BuyVolumeCorporate"))
    corp_sell = norm_num(pick(row, "corporate_sell_vol", "corporateSellVolume", "vol_corporate_sell", "SellVolumeCorporate"))
    return {
        "symbol": str(sym).strip(), "name": scalar(pick(row, "name", "lval30", "companyName", "companyNamePersian", default="")),
        "last": last, "close": close, "change_pct": change, "volume": vol, "value": value,
        "eps": eps, "pe": pe,
        "individual_net_volume": (ind_buy - ind_sell) if ind_buy is not None and ind_sell is not None else None,
        "corporate_net_volume": (corp_buy - corp_sell) if corp_buy is not None and corp_sell is not None else None,
        "flow_available": all(x is not None for x in (ind_buy, ind_sell, corp_buy, corp_sell)),
        "fundamental_status": "INSUFFICIENT DATA",
        "signal": "INSUFFICIENT DATA",
        "signal_reason": "برای سیگنال معتبر، تاریخچه قیمت، جریان حقیقی/حقوقی و صورت‌های مالی معتبر باید هم‌زمان تأیید شوند."
    }

async def get_market_data():
    now = time.time()
    if _cache["data"] and now - _cache["at"] < 20:
        return _cache["data"], _cache["source"], None
    errors = []
    timeout = httpx.Timeout(8.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36","Accept":"application/json"}) as client:
        for url in MARKETWATCH_URLS:
            try:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                rows = find_rows(payload)
                normalized = [x for x in (normalize(r) for r in rows if isinstance(r, dict)) if x]
                if not normalized:
                    errors.append(f"{url}: پاسخ دریافت شد اما ساختار داده قابل تشخیص نبود")
                    continue
                result = {"items": normalized, "received_at": datetime.now(timezone.utc).isoformat(), "count": len(normalized)}
                _cache.update({"at": now, "data": result, "source": url, "error": None})
                return result, url, None
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {str(exc)[:180]}")
    _cache["error"] = " | ".join(errors)
    if _cache["data"]:
        age = int(now - _cache["at"])
        return _cache["data"], _cache["source"], f"منبع زنده در دسترس نیست؛ داده ذخیره‌شده {age} ثانیه عمر دارد."
    return None, None, _cache["error"]

@app.get("/")
async def home():
    return FileResponse("static/index.html")

@app.get("/health")
async def health():
    return {"ok": True, "app": APP_NAME, "version": "0.1.0", "utc": datetime.now(timezone.utc).isoformat()}

@app.get("/api/status")
async def status():
    # Do not trigger a second upstream market request just to render the status card.
    # /api/market and /api/scan are responsible for refreshing market data.
    age = int(time.time() - _cache["at"]) if _cache["at"] else None
    data = _cache["data"]
    error = _cache["error"]
    live = bool(data and age is not None and age <= FRESH_SECONDS and not error)
    state = "LIVE" if live else ("STALE" if data else "OFFLINE")
    return {
        "status": state,
        "market_data_available": bool(data), "count": data["count"] if data else 0,
        "source": _cache["source"], "age_seconds": age, "max_age_seconds": FRESH_SECONDS,
        "last_received_at": data["received_at"] if data else None,
        "error": error, "signal_policy": "fail-closed: no BUY signals without validated history, flow and fundamentals"
    }

@app.get("/api/market")
async def market(q: str = "", limit: int = 500):
    data, source, error = await get_market_data()
    if not data:
        return {"status":"OFFLINE","items":[],"error":error,"message":"داده زنده دریافت نشد؛ هیچ داده ساختگی نمایش داده نمی‌شود."}
    items = data["items"]
    if q:
        q = q.strip().lower()
        items = [x for x in items if q in x["symbol"].lower() or q in x["name"].lower()]
    return {"status":"LIVE" if not error else "STALE","source":source,"received_at":data["received_at"],"count":len(items),"items":items[:max(1,min(limit,1000))],"warning":error}

@app.get("/api/scan")
async def scan():
    data, source, error = await get_market_data()
    if not data:
        return {"status":"OFFLINE","candidates":[],"message":"اسکن متوقف شد؛ منبع داده در دسترس نیست."}
    # A negative price change is only a candidate filter, never a BUY signal.
    candidates = [x for x in data["items"] if x["change_pct"] is not None and -5.0 <= x["change_pct"] < 0]
    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"status":"LIVE" if not error else "STALE","source":source,"received_at":data["received_at"],"count":len(candidates),"candidates":candidates[:100],"notice":"این فهرست فقط نمادهای منفی را غربال می‌کند؛ تا زمانی که تاریخچه تکنیکال، جریان حقیقی/حقوقی و داده بنیادی معتبر به نماد وصل نشده، سیگنال خرید صادر نمی‌شود.","warning":error}
