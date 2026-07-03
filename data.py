"""Market-data collection v3: Lighter (venue yang DITRADINGKAN) + Bybit V5 (positioning
mainnet riil) + alternative.me (Fear&Greed). Menggantikan Binance sepenuhnya (Bug #1).

Sumber per komponen:
  - Candles OHLCV : Lighter GET /api/v1/candles            [path & param diverifikasi ke SDK resmi]
  - Funding       : Lighter GET /api/v1/funding-rates      [respons: funding_rates[].{market_id,exchange,rate}]
  - Open Interest : Bybit  GET /v5/market/open-interest    (category=linear)
  - Long/Short    : Bybit  GET /v5/market/account-ratio
  - Taker buy/sell: Bybit  GET /v5/market/taker-buy-sell-vol  [BELUM TERVERIFIKASI -> degradasi ke None]
  - Fear & Greed  : alternative.me (tetap)

Prinsip fail-safe: SATU sumber gagal tidak boleh membunuh siklus. Fetch yang gagal
menghasilkan None dan dicatat di snapshot["data_gaps"] supaya LLM menurunkan confidence,
bukan mengarang angka. Funding testnet Lighter = sinyal LEMAH (bukan crowd riil) — diberi label.
"""
import time
import datetime
import httpx
from config import CONFIG

BYBIT = "https://api.bybit.com"
FNG = "https://api.alternative.me/fng/?limit=1"
_HEADERS = {"User-Agent": "Mozilla/5.0 (zupin-bot)"}

_RES_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400}
_BYBIT_ITV = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


async def _get(client, url, params=None):
    r = await client.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def _try(client, url, params=None):
    """Fetch yang tidak boleh meledak: gagal -> None (dicatat sebagai gap)."""
    try:
        return await _get(client, url, params)
    except Exception:
        return None


def _bybit_list(resp):
    """Bybit V5 membungkus data di {retCode, result:{list:[...]}}; retCode!=0 = gagal."""
    if not isinstance(resp, dict) or resp.get("retCode") not in (0, "0"):
        return None
    lst = ((resp.get("result") or {}).get("list")) or []
    # Bybit mengembalikan terbaru-dulu; urutkan naik berdasarkan timestamp agar first/last konsisten.
    try:
        lst = sorted(lst, key=lambda x: int(x.get("timestamp") or 0))
    except Exception:
        pass
    return lst or None


async def collect_market_data():
    res = CONFIG.interval if CONFIG.interval in _RES_SEC else "1h"
    now = int(time.time())
    span = _RES_SEC[res] * 210  # 200 candle + buffer
    itv = _BYBIT_ITV.get(res, "1h")
    async with httpx.AsyncClient(headers=_HEADERS) as c:
        candles = await _try(c, f"{CONFIG.lighter_base_url}/api/v1/candles", {
            "market_id": CONFIG.market_index, "resolution": res,
            "start_timestamp": now - span, "end_timestamp": now, "count_back": 200,
        })
        funding = await _try(c, f"{CONFIG.lighter_base_url}/api/v1/funding-rates",
                             {"market_id": CONFIG.market_index})
        oi = await _try(c, f"{BYBIT}/v5/market/open-interest", {
            "category": "linear", "symbol": CONFIG.bybit_symbol, "intervalTime": itv, "limit": 96,
        })
        ls = await _try(c, f"{BYBIT}/v5/market/account-ratio", {
            "category": "linear", "symbol": CONFIG.bybit_symbol, "period": itv, "limit": 24,
        })
        taker = await _try(c, f"{BYBIT}/v5/market/taker-buy-sell-vol", {  # [UNVERIFIED endpoint]
            "category": "linear", "symbol": CONFIG.bybit_symbol, "period": itv, "limit": 24,
        })
        fng = await _try(c, FNG)
    return {"candles": candles, "funding": funding, "oi": oi, "ls": ls, "taker": taker, "fng": fng}


def build_snapshot(raw, account):
    gaps = []

    # ---- Lighter candles: {"code":..,"c":[{t,o,h,l,c,v}...]} (field names dari SDK model) ----
    kl = (raw.get("candles") or {}).get("c") or []
    try:
        kl = sorted(kl, key=lambda k: int(k.get("t") or 0))
    except Exception:
        pass
    closes = [v for v in (_num(k.get("c")) for k in kl) if v is not None]
    highs = [v for v in (_num(k.get("h")) for k in kl) if v is not None]
    lows = [v for v in (_num(k.get("l")) for k in kl) if v is not None]
    vols = [v for v in (_num(k.get("v")) for k in kl) if v is not None]
    if not closes:
        gaps.append("lighter_candles")

    last = closes[-1] if closes else None
    per_day = max(1, int(86400 / _RES_SEC.get(CONFIG.interval, 3600)))
    n24 = min(per_day, len(closes)) if closes else 0
    h24 = max(highs[-n24:]) if n24 and highs else None
    l24 = min(lows[-n24:]) if n24 and lows else None
    c24 = closes[-n24 - 1] if len(closes) > n24 else (closes[0] if closes else None)
    chg24 = ((last - c24) / c24 * 100) if (last is not None and c24) else None
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    rng = ((last - l24) / (h24 - l24) * 100) if (last is not None and h24 and l24 and h24 > l24) else None
    trend = "mixed"
    if last is not None and sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "up"
        elif last < sma20 < sma50:
            trend = "down"
    vol_now = sum(vols[-n24:]) if n24 and vols else None
    vol_prev = sum(vols[-2 * n24:-n24]) if vols and len(vols) >= 2 * n24 else None
    volchg = ((vol_now - vol_prev) / vol_prev * 100) if (vol_now is not None and vol_prev) else None

    # ---- Lighter funding: {"funding_rates":[{market_id,exchange,symbol,rate}...]} ----
    frate = None
    for e in ((raw.get("funding") or {}).get("funding_rates") or []):
        if str(e.get("market_id")) == str(CONFIG.market_index):
            frate = _num(e.get("rate"))
            if str(e.get("exchange", "")).lower() == "lighter":
                break  # prefer entry lighter sendiri
    if frate is None:
        gaps.append("lighter_funding")

    # ---- Bybit OI ----
    oi_list = _bybit_list(raw.get("oi"))
    oi_last = _num(oi_list[-1].get("openInterest")) if oi_list else None
    oi_first = _num(oi_list[0].get("openInterest")) if oi_list else None
    oichg = ((oi_last - oi_first) / oi_first * 100) if (oi_last is not None and oi_first) else None
    if oi_list is None:
        gaps.append("bybit_open_interest")

    # ---- Bybit long/short account ratio ----
    ls_list = _bybit_list(raw.get("ls"))
    buy = _num(ls_list[-1].get("buyRatio")) if ls_list else None
    sell = _num(ls_list[-1].get("sellRatio")) if ls_list else None
    ls_ratio = (buy / sell) if (buy and sell) else None
    if ls_list is None:
        gaps.append("bybit_long_short_ratio")

    # ---- Bybit taker buy/sell (endpoint belum terverifikasi -> boleh None) ----
    tk_list = _bybit_list(raw.get("taker"))
    taker_ratio = None
    if tk_list:
        t = tk_list[-1]
        tb = _num(t.get("buyVolume")) or _num(t.get("buyVol"))
        ts = _num(t.get("sellVolume")) or _num(t.get("sellVol"))
        taker_ratio = (tb / ts) if (tb and ts) else None
    if taker_ratio is None:
        gaps.append("taker_buy_sell")

    fng = ((raw.get("fng") or {}).get("data") or [{}])[0]
    if _num(fng.get("value")) is None:
        gaps.append("fear_greed")

    return {
        "symbol": f"BTC-PERP (Lighter testnet, market_id={CONFIG.market_index})",
        "interval": CONFIG.interval,
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data_sources": {
            "price_candles": "lighter_testnet", "funding": "lighter_testnet (sinyal lemah, bukan crowd riil)",
            "open_interest": "bybit_mainnet", "long_short": "bybit_mainnet", "taker": "bybit_mainnet[unverified]",
        },
        "data_gaps": gaps,
        "price": {
            "last": last, "change_24h_pct": chg24, "high_24h": h24, "low_24h": l24,
            "range_pos_pct": rng, "sma20": sma20, "sma50": sma50, "trend": trend,
            "volume_24h_base": vol_now, "volume_change_pct": volchg,
        },
        "funding": {"rate_raw": frate,
                    "rate_pct_if_fraction": (frate * 100) if frate is not None else None,
                    "note": "funding testnet; verifikasi unit sebelum dipercaya"},
        "open_interest": {"current": oi_last, "change_window_pct": oichg, "source": "bybit"},
        "long_short": {"account_ratio": ls_ratio,
                       "long_pct": (buy * 100) if buy is not None else None,
                       "taker_buy_sell_ratio": taker_ratio},
        "sentiment": {"fear_greed": _num(fng.get("value")), "label": fng.get("value_classification")},
        "account": account,
    }
