#!/usr/bin/env python3
"""Historical market data: download, clean, and cache OHLC candles.

The live market-qx feed carries no public history (the `history/*` events need
an authenticated SSID), so a model that must be trained *now* needs an external
source. This module pulls candles from Yahoo Finance's public chart endpoint —
no API key, no login — for any forex pair, metal, index or crypto, cleans them,
and caches them on disk so repeat calls are instant.

Everything is offline-safe: a network failure raises `DataError` with a clear
message rather than a stack trace, and the cleaned result is cached so a trained
model can be rebuilt without re-downloading.

Usage
-----
    from market_data import fetch, PAIRS, ML_TIMEFRAMES
    candles, quality = fetch("EURUSD", "1h")     # -> [[t,o,c,h,l], ...]
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DATA_DIR = os.environ.get("HISTORY_DIR", "data/history")
CACHE_TTL = int(os.environ.get("HISTORY_TTL", "1800"))  # seconds

_UAS = (
    "Mozilla/5.0",           # simplest UA is the one Yahoo reliably serves
    "Mozilla/5.0 (compatible; research)",
    "curl/8.5.0",
)

# Yahoo throttles bursts hard (~1 request per few seconds from one IP), so every
# call goes through a global gate that enforces a minimum spacing. This is what
# makes "download for any pair" reliable instead of a coin flip on 429s.
_RATE_LOCK = None
_LAST_CALL = 0.0
_MIN_INTERVAL = float(os.environ.get("HISTORY_MIN_INTERVAL", "6.0"))


def _rate_gate() -> None:
    global _RATE_LOCK, _LAST_CALL
    if _RATE_LOCK is None:
        import threading
        _RATE_LOCK = threading.Lock()
    with _RATE_LOCK:
        wait = _MIN_INTERVAL - (time.time() - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.time()


class DataError(RuntimeError):
    """Raised when historical data cannot be downloaded or is unusable."""


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

# display name -> (yahoo symbol, kind). Display names are what the UI shows.
PAIRS: dict[str, tuple[str, str]] = {
    # majors
    "EURUSD": ("EURUSD=X", "forex"),
    "GBPUSD": ("GBPUSD=X", "forex"),
    "USDJPY": ("USDJPY=X", "forex"),
    "USDCHF": ("USDCHF=X", "forex"),
    "AUDUSD": ("AUDUSD=X", "forex"),
    "USDCAD": ("USDCAD=X", "forex"),
    "NZDUSD": ("NZDUSD=X", "forex"),
    # crosses
    "EURGBP": ("EURGBP=X", "forex"),
    "EURJPY": ("EURJPY=X", "forex"),
    "GBPJPY": ("GBPJPY=X", "forex"),
    "EURCHF": ("EURCHF=X", "forex"),
    "EURAUD": ("EURAUD=X", "forex"),
    "EURCAD": ("EURCAD=X", "forex"),
    "EURNZD": ("EURNZD=X", "forex"),
    "GBPCHF": ("GBPCHF=X", "forex"),
    "GBPAUD": ("GBPAUD=X", "forex"),
    "GBPCAD": ("GBPCAD=X", "forex"),
    "GBPNZD": ("GBPNZD=X", "forex"),
    "AUDJPY": ("AUDJPY=X", "forex"),
    "AUDCAD": ("AUDCAD=X", "forex"),
    "AUDCHF": ("AUDCHF=X", "forex"),
    "AUDNZD": ("AUDNZD=X", "forex"),
    "CADJPY": ("CADJPY=X", "forex"),
    "CADCHF": ("CADCHF=X", "forex"),
    "CHFJPY": ("CHFJPY=X", "forex"),
    "NZDJPY": ("NZDJPY=X", "forex"),
    "NZDCAD": ("NZDCAD=X", "forex"),
    "NZDCHF": ("NZDCHF=X", "forex"),
    "USDSEK": ("USDSEK=X", "forex"),
    "USDNOK": ("USDNOK=X", "forex"),
    "USDMXN": ("USDMXN=X", "forex"),
    "USDTRY": ("USDTRY=X", "forex"),
    "USDZAR": ("USDZAR=X", "forex"),
    "USDSGD": ("USDSGD=X", "forex"),
    "USDCNH": ("USDCNH=X", "forex"),
    # metals
    "XAUUSD": ("GC=F", "metal"),
    "XAGUSD": ("SI=F", "metal"),
    # energy
    "WTIUSD": ("CL=F", "energy"),
    # crypto
    "BTCUSD": ("BTC-USD", "crypto"),
    "ETHUSD": ("ETH-USD", "crypto"),
    # indices
    "SP500": ("^GSPC", "index"),
    "NASDAQ": ("^IXIC", "index"),
    "DOW30": ("^DJI", "index"),
    "FTSE100": ("^FTSE", "index"),
    "DAX40": ("^GDAXI", "index"),
    "NIKKEI": ("^N225", "index"),
}

# UI timeframe label -> (base interval, range, aggregate seconds).
#
# Yahoo's *native* FX daily bars are structurally broken: on ~40% of them
# open == close, which fabricates a fake 80% "edge" (a model that predicts the
# next day's direction from the current bar's wick). Hourly bars do not have
# that defect, so the daily series is built by aggregating hourly bars instead
# of trusting the native daily ones. `aggregate=None` means use the bars as-is.
TIMEFRAME_PLAN: dict[str, tuple[str, str, int | None]] = {TIMEFRAME_PLAN: dict[str, tuple[str, str, int | None]] = {
    "1m": ("1m", "7d", None),
    "5m": ("5m", "60d", None),
    "15m": ("15m", "60d", None),
    "30m": ("30m", "60d", None),
    "1h": ("1h", "2y", None),
    "1d": ("1h", "730d", 86400),
    "1wk": ("1wk", "10y", None),
}
ML_TIMEFRAMES = {k: (v[0], v[1]) for k, v in TIMEFRAME_PLAN.items()}
ALL_TF = "all"


def catalog() -> list[dict]:
    """Grouped instrument list for the UI dropdown."""
    groups: dict[str, list[str]] = {}
    for name, (_, kind) in PAIRS.items():
        groups.setdefault(kind, []).append(name)
    order = ["forex", "metal", "energy", "crypto", "index"]
    return [{"type": k, "symbols": sorted(groups[k])}
            for k in order if k in groups]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _yahoo_chart(symbol: str, interval: str, rng: str, timeout: float = 20.0,
                 retries: int = 4) -> dict:
    hosts = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
    last: Exception | None = None
    for attempt in range(retries):
        host = hosts[attempt % len(hosts)]
        ua = _UAS[attempt % len(_UAS)]
        url = (f"https://{host}/v8/finance/chart/"
               f"{urllib.parse.quote(symbol, safe='=^-.')}?range={rng}&interval={interval}")
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            _rate_gate()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            last = DataError(f"source returned HTTP {exc.code} for {symbol}")
            if exc.code not in (429, 500, 502, 503):
                raise last from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = DataError(f"network error fetching {symbol}: {exc}")
        except json.JSONDecodeError as exc:
            last = DataError(f"bad response for {symbol}")
        time.sleep(2.0 * (attempt + 1))
    raise last or DataError(f"failed to fetch {symbol}")


def aggregate(rows: list[list], seconds: int) -> list[list]:
    """Roll bars up to `seconds` buckets (open=first, close=last, hi/lo=extremes)."""
    buckets: dict[int, list] = {}
    for r in rows:
        k = r[0] // seconds * seconds
        b = buckets.get(k)
        if b is None:
            buckets[k] = [k, r[1], r[2], r[3], r[4]]
        else:
            b[2] = r[2]
            b[3] = max(b[3], r[3])
            b[4] = min(b[4], r[4])
    return [buckets[k] for k in sorted(buckets)]


def download(name: str, tf: str) -> list[list]:
    """Raw OHLC rows [[t,o,c,h,l], ...] for a display name + timeframe label."""
    if name not in PAIRS:
        raise DataError(f"unknown instrument {name!r}")
    if tf not in TIMEFRAME_PLAN:
        raise DataError(f"unknown timeframe {tf!r}")
    symbol, _ = PAIRS[name]
    interval, rng, agg_sec = TIMEFRAME_PLAN[tf]
    data = _yahoo_chart(symbol, interval, rng)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        err = (data.get("chart") or {}).get("error")
        raise DataError(f"no data for {name} {tf}" + (f": {err}" if err else ""))
    res = result[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens, closes = q.get("open") or [], q.get("close") or []
    highs, lows = q.get("high") or [], q.get("low") or []
    rows = []
    for i in range(len(ts)):
        try:
            o, c, h, lo = opens[i], closes[i], highs[i], lows[i]
        except IndexError:
            break
        if None in (o, c, h, lo):
            continue
        rows.append([int(ts[i]), float(o), float(c), float(h), float(lo)])
    if not rows:
        raise DataError(f"no usable bars for {name} {tf}")
    if agg_sec:
        rows = aggregate(rows, agg_sec)
    return rows


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

@dataclass
class Quality:
    bars: int = 0
    dropped: int = 0
    flat_ratio: float = 0.0
    gap_ratio: float = 0.0
    spike_ratio: float = 0.0
    oc_equal_ratio: float = 0.0   # bars where open == close (a data defect)
    score: float = 0.0
    ok: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"bars": self.bars, "dropped": self.dropped,
                "flat_ratio": round(self.flat_ratio, 3),
                "gap_ratio": round(self.gap_ratio, 3),
                "spike_ratio": round(self.spike_ratio, 3),
                "oc_equal_ratio": round(self.oc_equal_ratio, 3),
                "score": round(self.score, 3), "ok": self.ok, "notes": self.notes}


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def clean(rows: list[list], expected_gap: int | None = None) -> tuple[list[list], Quality]:
    """Sort, dedupe and filter raw bars; return (clean_candles, Quality)."""
    q = Quality()
    raw_n = len(rows)
    rows = sorted(rows, key=lambda r: r[0])
    out: list[list] = []
    seen: set[int] = set()
    bad = 0
    for r in rows:
        t, o, c, h, lo = r
        if t in seen:
            bad += 1
            continue
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (o, c, h, lo)):
            bad += 1
            continue
        if h < lo or min(o, c, h, lo) <= 0:
            bad += 1
            continue
        seen.add(t)
        out.append([int(t), float(o), float(c), float(h), float(lo)])
    q.dropped = bad

    # flat bars (open==high==low==close) carry no information
    flats = sum(1 for r in out if r[1] == r[2] == r[3] == r[4])
    q.flat_ratio = flats / len(out) if out else 1.0

    # gaps: consecutive bars further apart than the expected step
    if expected_gap is None and len(out) > 2:
        diffs = [out[i + 1][0] - out[i][0] for i in range(len(out) - 1)]
        expected_gap = _median(diffs)
    if expected_gap and len(out) > 2:
        gaps = sum(1 for i in range(len(out) - 1)
                   if out[i + 1][0] - out[i][0] > expected_gap * 1.5)
        q.gap_ratio = gaps / (len(out) - 1)

    # robust spike filter on log-returns (median / MAD), dropped as outliers
    if len(out) > 20:
        rets = []
        for i in range(1, len(out)):
            prev = out[i - 1][2]
            rets.append(math.log(out[i][2] / prev) if prev > 0 and out[i][2] > 0 else 0.0)
        med = _median(rets)
        mad = _median([abs(x - med) for x in rets]) or 1e-9
        keep = []
        spikes = 0
        for i, r in enumerate(out):
            if i == 0:
                keep.append(r)
                continue
            z = abs(rets[i - 1] - med) / (1.4826 * mad)
            if z > 12:  # >12 robust sigmas: a data glitch, not a market move
                spikes += 1
                continue
            keep.append(r)
        q.spike_ratio = spikes / len(out) if out else 0.0
        out = keep

    q.bars = len(out)

    # Structural integrity: a bar whose open equals its close is almost always a
    # source defect (a real FX bar rarely opens exactly at its close), and a high
    # rate of them fabricates a spurious edge. Flag it so it is never trained on.
    q.oc_equal_ratio = (sum(1 for r in out if abs(r[1] - r[2]) < 1e-12) / len(out)) if out else 0.0

    # data-quality score: 1 = pristine, 0 = unusable
    q.score = max(0.0, 1.0 - (q.flat_ratio * 0.6 + q.gap_ratio * 0.5
                              + q.spike_ratio * 4.0 + q.oc_equal_ratio * 0.5
                              + (q.dropped / raw_n if raw_n else 0)))
    if q.bars < 200:
        q.ok = False
        q.notes.append(f"only {q.bars} bars after cleaning")
    if q.flat_ratio > 0.5:
        q.ok = False
        q.notes.append(f"{q.flat_ratio:.0%} of bars are flat (market likely closed)")
    if q.oc_equal_ratio > 0.45:
        q.ok = False
        q.notes.append(f"{q.oc_equal_ratio:.0%} of bars have open==close "
                       f"(broken source bars)")
    if q.spike_ratio > 0.02:
        q.notes.append(f"{q.spike_ratio:.1%} of bars were spikes")
    return out, q


# ---------------------------------------------------------------------------
# Cache + public fetch
# ---------------------------------------------------------------------------

def _cache_path(name: str, tf: str) -> str:
    safe = name.replace("/", "")
    return os.path.join(DATA_DIR, f"{safe}_{tf}.csv")


def _read_cache(path: str) -> list[list]:
    rows = []
    with open(path) as f:
        for row in csv.reader(f):
            if not row or not row[0].strip().lstrip("-").isdigit():
                continue
            try:
                rows.append([int(float(row[0])), float(row[1]), float(row[2]),
                             float(row[3]), float(row[4])])
            except ValueError:
                continue
    return rows


def _write_cache(path: str, rows: list[list]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "close", "high", "low"])
        w.writerows(rows)
    os.replace(tmp, path)


def fetch(name: str, tf: str, *, use_cache: bool = True,
          max_age: int = CACHE_TTL) -> tuple[list[list], Quality]:
    """Download (or load cached) and clean candles for `name` at timeframe `tf`.

    Returns `(candles, quality)`. Raises `DataError` if nothing usable is
    available. When the network fails but a cache exists, the cache is used.
    """
    path = _cache_path(name, tf)
    fresh = use_cache and os.path.exists(path) and (time.time() - os.path.getmtime(path) < max_age)
    if fresh:
        rows = _read_cache(path)
        if len(rows) >= 200:
            candles, q = clean(rows)
            q.notes.append("cached")
            return candles, q
    try:
        rows = download(name, tf)
    except DataError:
        if os.path.exists(path):
            rows = _read_cache(path)
            if len(rows) >= 200:
                candles, q = clean(rows)
                q.notes.append("stale cache (network unavailable)")
                return candles, q
        raise
    candles, q = clean(rows)
    if use_cache and candles:
        _write_cache(path, candles)
    return candles, q


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download + clean historical candles")
    ap.add_argument("pair", nargs="?", default="EURUSD")
    ap.add_argument("tf", nargs="?", default="1h")
    args = ap.parse_args()
    cs, q = fetch(args.pair, args.tf, use_cache=False)
    print(f"{args.pair} {args.tf}: {len(cs)} clean candles")
    print(f"quality: {q.as_dict()}")
    if cs:
        t0 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(cs[0][0]))
        t1 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(cs[-1][0]))
        print(f"range: {t0} -> {t1}  last close={cs[-1][2]}")
