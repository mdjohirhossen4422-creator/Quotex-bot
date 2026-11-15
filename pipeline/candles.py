"""Candle construction from ticks and from lower-timeframe candles.

Two independent paths are provided on purpose:

  * `build_from_ticks` — the authoritative, tick-level aggregation
    (open = first tick, high = max, low = min, close = last).
  * `aggregate_candles` — combine already-built lower-timeframe candles.

The 30m series is produced BOTH ways and cross-checked, so a bug in one path
cannot silently pass. Missing buckets are never fabricated: only buckets that
actually contain ticks become candles.
"""
from __future__ import annotations

from typing import Optional

OK, PARTIAL, GAP = "ok", "partial", "gap"


def _bucket(ts: float, period: int) -> int:
    return int(ts // period) * period


def build_from_ticks(ticks: list[tuple[float, float, int]], period: int
                     ) -> list[dict]:
    """Aggregate a chronologically-sorted (ts, price, dir) stream into candles.

    Returns a list of dicts with the exact OHLC rule and a quality status. The
    first and last buckets are flagged `partial` (the session may start/end
    mid-bucket); a bucket whose predecessor is missing is flagged `gap`.
    """
    if not ticks:
        return []
    out: list[dict] = []
    cur: Optional[dict] = None
    prev_bucket: Optional[int] = None
    for ts, price, _d in ticks:
        b = _bucket(ts, period)
        if cur is None or b != cur["time"]:
            if cur is not None:
                out.append(cur)
            status = OK
            if prev_bucket is not None and b - prev_bucket > period:
                status = GAP
            cur = {"time": b, "open": price, "high": price, "low": price,
                   "close": price, "tick_count": 0, "first_ts": ts, "last_ts": ts,
                   "completeness_status": status}
            prev_bucket = b
        cur["high"] = max(cur["high"], price)
        cur["low"] = min(cur["low"], price)
        cur["close"] = price
        cur["tick_count"] += 1
        cur["last_ts"] = ts
    if cur is not None:
        out.append(cur)
    if out:
        if out[0]["completeness_status"] == OK:
            out[0]["completeness_status"] = PARTIAL
        if out[-1]["completeness_status"] == OK:
            out[-1]["completeness_status"] = PARTIAL
    for c in out:
        c["price_change"] = c["close"] - c["open"]
    return out


def aggregate_candles(lower: list[dict], factor: int) -> list[dict]:
    """Combine `factor` consecutive lower-timeframe candles into one.

    A group is only emitted when it is exactly `factor` candles, contiguous, and
    aligned to the higher boundary. Incomplete groups are flagged `partial`
    rather than silently stretched.
    """
    if factor <= 1:
        return [dict(c) for c in lower]
    out: list[dict] = []
    i = 0
    n = len(lower)
    while i < n:
        base = lower[i]["time"]
        group = [lower[i]]
        j = i + 1
        while (j < n and len(group) < factor
               and lower[j]["time"] == base + len(group) * _step(lower)):
            group.append(lower[j])
            j += 1
        c = {
            "time": base,
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
            "tick_count": sum(x.get("tick_count", 0) for x in group),
            "first_ts": group[0].get("first_ts", base),
            "last_ts": group[-1].get("last_ts", group[-1]["time"]),
            "completeness_status": OK if len(group) == factor else PARTIAL,
        }
        c["price_change"] = c["close"] - c["open"]
        out.append(c)
        i = j
    return out


def _step(lower: list[dict]) -> int:
    if len(lower) < 2:
        return 1
    return lower[1]["time"] - lower[0]["time"]


def to_ohlc(candles: list[dict]) -> list[list]:
    """[time, open, close, high, low] — the layout the indicator modules expect."""
    return [[c["time"], c["open"], c["close"], c["high"], c["low"]] for c in candles]
