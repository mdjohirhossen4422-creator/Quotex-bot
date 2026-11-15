"""Assemble per-(asset, timeframe) feature matrices, labels, and MTF context.

Causality rules enforced here:

* features at row i use candles [0..i] only,
* a higher-timeframe trend is read from the last higher-TF candle that had
  *fully closed* at or before the row's timestamp (`b + period <= t`), so a
  still-forming higher candle never leaks,
* labels describe the NEXT candle and are stored in separate columns, never in
  the feature matrix.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

import indicators as ta
from . import features as feat
from . import structurefeat
from . import tickfeat
from .config import SIDEWAYS_ATR_MULT, MTF_FEATURES, TICK_FEATURES

DOWN, SIDE, UP_L = 0, 1, 2


def trend_code(closes: list[float], fast: int = 9, slow: int = 21) -> list[int]:
    """+1 / -1 / 0 trend per candle from the EMA fast/slow sign. Causal."""
    ef = ta.ema(closes, fast)
    es = ta.ema(closes, slow)
    out = []
    for i in range(len(closes)):
        if ef[i] is None or es[i] is None:
            out.append(0)
        elif ef[i] > es[i]:
            out.append(1)
        elif ef[i] < es[i]:
            out.append(-1)
        else:
            out.append(0)
    return out


def _last_closed_index(higher_times: list[int], period: int, t: int) -> Optional[int]:
    """Index of the last higher-TF candle whose close (b+period) is <= t."""
    lo, hi, ans = 0, len(higher_times) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if higher_times[mid] + period <= t:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def mtf_trend_features(row_times: list[int], tf_period: int,
                       higher: dict[str, tuple[list[int], list[int]]]
                       ) -> dict[str, list]:
    """For each row time, the last *closed* higher-TF trend of each timeframe.

    `higher` maps tf-name -> (times, trend_codes) for higher timeframes only.
    """
    out = {f"trend_{name}": [0] * len(row_times) for name in higher}
    for name, (times, codes) in higher.items():
        period = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}[name]
        key = f"trend_{name}"
        for i, t in enumerate(row_times):
            j = _last_closed_index(times, period, t)
            out[key][i] = codes[j] if j is not None else 0
    return out


def build_labels(candles: list[list], atr: list, mult: float = SIDEWAYS_ATR_MULT
                 ) -> dict[str, list]:
    """Three-class next-candle labels + the raw future quantities.

    The last row has no next candle and gets label None (dropped later).
    """
    n = len(candles)
    label = [None] * n
    next_ret = [None] * n
    next_range = [None] * n
    next_dir = [None] * n
    label_ts = [None] * n
    for i in range(n - 1):
        nxt = candles[i + 1]
        n_open, n_close, n_high, n_low = nxt[1], nxt[2], nxt[3], nxt[4]
        move = n_close - n_open
        thr = (atr[i] * mult) if (atr[i] is not None and atr[i] > 0) else None
        next_ret[i] = (move / n_open) if n_open else None
        next_range[i] = n_high - n_low
        next_dir[i] = 1 if move > 0 else (-1 if move < 0 else 0)
        label_ts[i] = nxt[0]
        if thr is None:
            label[i] = None
        elif move > thr:
            label[i] = UP_L
        elif move < -thr:
            label[i] = DOWN
        else:
            label[i] = SIDE
    return {"label": label, "next_ret": next_ret, "next_range": next_range,
            "next_dir": next_dir, "label_ts": label_ts}


def build_matrix(asset: str, market: str, tf_name: str, period: int,
                 candles: list[dict],
                 ticks: Optional[list[tuple[float, float, int]]] = None,
                 higher: Optional[dict] = None) -> dict:
    """One dataset for one (asset, timeframe). Returns columns as lists."""
    ohlc = [[c["time"], c["open"], c["close"], c["high"], c["low"]] for c in candles]
    o = [c[1] for c in ohlc]
    cl = [c[2] for c in ohlc]
    hi = [c[3] for c in ohlc]
    lo = [c[4] for c in ohlc]
    times = [c[0] for c in ohlc]
    n = len(ohlc)

    cols: dict[str, list] = {}
    cols.update(feat.build_features(ohlc))
    atr = ta.atr(hi, lo, cl, 14)   # raw ATR for structure normalisation

    if ticks:
        tf_map = tickfeat.tick_features(ticks, period)
        for key in TICK_FEATURES:
            if key == "tick_activity":
                continue
            cols[key] = [tf_map.get(t, {}).get(key, 0.0) for t in times]
        counts = [int(tf_map.get(t, {}).get("tick_count", 0)) for t in times]
        cols["tick_activity"] = tickfeat.tick_activity(counts)
    else:
        for key in TICK_FEATURES:
            cols[key] = [0.0] * n

    cols.update(structurefeat.structure_features(o, cl, hi, lo, atr))

    # Multi-timeframe trend context. Every MTF column always exists (0 = no
    # higher TF available); the computed ones overwrite the default.
    for name in MTF_FEATURES:
        cols[name] = [0] * n
    if higher:
        cols.update(mtf_trend_features(times, period, higher))

    labels = build_labels(ohlc, atr)

    meta = {
        "asset": [asset] * n,
        "market_type": [market] * n,
        "timeframe": [tf_name] * n,
        "time": times,
        "open": o, "high": hi, "low": lo, "close": cl,
        "tick_count": [c.get("tick_count", 0) for c in candles],
        "price_change": [c.get("price_change", c["close"] - c["open"]) for c in candles],
        "source": [market] * n,
        "completeness_status": [c.get("completeness_status", "ok") for c in candles],
    }
    return {"meta": meta, "features": cols, "labels": labels, "n": n}
