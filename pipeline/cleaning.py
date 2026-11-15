"""Data cleaning and the quality report (Task 2).

Rules, in line with the brief:

* never fabricate a price or a candle,
* never delete a legitimate price jump — flag it,
* keep raw data untouched (this module works on copies),
* preserve the broker direction flag as its own feature, never as BUY/SELL.

Rows are only *dropped* when they are genuinely unusable (non-positive price,
high < low, misaligned timestamp, duplicate key). Everything else is flagged so
downstream code can decide, and the counts are reported.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

SPIKE_K = 10.0            # robust (MAD) threshold for an abnormal jump
SESSION_GAP_MULT = 20     # a gap > this many periods is a session gap, not loss


@dataclass
class CleanReport:
    asset: str
    period: int
    n_in: int = 0
    n_out: int = 0
    dropped_invalid_price: int = 0
    dropped_bad_ohlc: int = 0
    dropped_misaligned: int = 0
    dropped_duplicate: int = 0
    flagged_spike: int = 0
    gaps: int = 0
    session_gaps: int = 0
    partial: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _median_mad(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    return med, mad


def clean_candles(asset: str, period: int, candles: list[dict],
                  spike_k: float = SPIKE_K) -> tuple[list[dict], CleanReport]:
    """Validate one series. Returns (clean candles, report). Input is not mutated."""
    rep = CleanReport(asset=asset, period=period, n_in=len(candles))
    clean: list[dict] = []
    seen: set[int] = set()

    for c in candles:
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        # 1. invalid prices
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                   for v in (o, h, l, cl)):
            rep.dropped_invalid_price += 1
            continue
        # 2. broken OHLC geometry
        if h < l or h < max(o, cl) or l > min(o, cl):
            rep.dropped_bad_ohlc += 1
            continue
        # 3. timestamp alignment
        if not isinstance(c["time"], int) or c["time"] <= 0 or c["time"] % period != 0:
            rep.dropped_misaligned += 1
            continue
        # 4. duplicate key
        if c["time"] in seen:
            rep.dropped_duplicate += 1
            continue
        seen.add(c["time"])
        clean.append(dict(c))

    clean.sort(key=lambda x: x["time"])

    # 5. gaps (missing candles) and session gaps
    for i in range(1, len(clean)):
        d = clean[i]["time"] - clean[i - 1]["time"]
        if d > period:
            if d > SESSION_GAP_MULT * period:
                rep.session_gaps += 1
            else:
                rep.gaps += 1

    # 6. incomplete candles
    rep.partial = sum(1 for c in clean if c.get("completeness_status") == "partial")

    # 7. abnormal jumps — flagged, never removed
    rets = [math.log(clean[i]["close"] / clean[i - 1]["close"])
            for i in range(1, len(clean)) if clean[i - 1]["close"] > 0]
    med, mad = _median_mad(rets)
    thr = spike_k * 1.4826 * mad if mad > 0 else float("inf")
    for i, c in enumerate(clean):
        c["spike"] = False
        if i > 0 and thr != float("inf"):
            r = math.log(c["close"] / clean[i - 1]["close"]) \
                if clean[i - 1]["close"] > 0 else 0.0
            if abs(r - med) > thr:
                c["spike"] = True
                rep.flagged_spike += 1

    rep.n_out = len(clean)
    return clean, rep


def coverage_ok(clean: list[dict], min_candles: int) -> bool:
    """Enough history to compute the longest indicator (EMA200) and train."""
    return len(clean) >= min_candles


def merge_reports(reports: list[CleanReport]) -> dict:
    keys = ["n_in", "n_out", "dropped_invalid_price", "dropped_bad_ohlc",
            "dropped_misaligned", "dropped_duplicate", "flagged_spike", "gaps",
            "session_gaps", "partial"]
    out = {k: sum(getattr(r, k) for r in reports) for k in keys}
    out["series"] = len(reports)
    return out
