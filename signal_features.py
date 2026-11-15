#!/usr/bin/env python3
"""Feature contract for the AI Signal Engine (v3).

Three causal blocks, concatenated in a fixed order so training and live
inference agree without a schema lookup:

  * BASE — the 26 candle/indicator features from `features.py` (reused as-is),
  * TICK — 6 order-flow features read from the per-candle tick summaries that
    `live_collector.py --no-ticks` preserves (`tick_summary.py`). All are
    zero-filled when a row has no summary, and the caller can instead *require*
    a summary via `require_summary=True`,
  * MTF  — higher-timeframe context. Only the last higher-TF candle that had
    *fully closed* at or before the row's timestamp is used, so a still-forming
    higher candle never leaks.

Causality is the whole point: every value at row `i` is computed from candles
`[0..i]` and summaries of candles `[0..i]`. `test_signal_features.py` asserts a
future change cannot alter a past row.
"""
from __future__ import annotations

import math
from typing import Optional

import indicators as ta
import features as base_features

BASE_FEATURES = list(base_features.FEATURES)

# Order-flow features (from tick_summary columns). Kept small and normalised.
TICK_FEATURES = [
    "tk_count_log", "tk_dir_ratio", "tk_momentum",
    "tk_short_vol", "tk_range", "tk_broker_dir",
]

MTF_FEATURES = ["mtf_trend", "mtf_ret", "mtf_dist"]

FEATURES = BASE_FEATURES + TICK_FEATURES + MTF_FEATURES

# Which summary columns feed which TICK feature.
_TICK_SOURCE = {
    "tk_count_log": "tick_count",
    "tk_dir_ratio": "tick_dir_ratio",
    "tk_momentum": "tick_momentum",
    "tk_short_vol": "tick_short_vol",
    "tk_range": "tick_range",
    "tk_broker_dir": "broker_dir_ratio",
}


def _tick_block(summary: Optional[dict]) -> dict:
    out = {}
    for name in TICK_FEATURES:
        col = _TICK_SOURCE[name]
        val = 0.0
        if summary is not None:
            raw = summary.get(col)
            if raw is not None:
                val = float(raw)
        if name == "tk_count_log":
            val = math.log1p(max(val, 0.0))
        out[name] = val
    return out


class MTFContext:
    """Causal higher-timeframe context built once per (asset, timeframe)."""

    def __init__(self, higher_candles: list[list], higher_period: int):
        self.period = higher_period
        self.candles = higher_candles
        closes = [c[2] for c in higher_candles]
        self.ema_fast = ta.ema(closes, 9) if closes else []

    def features(self, t: float, price: float) -> dict:
        """Features from the last higher candle that closed at or before `t`."""
        if not self.candles:
            return {k: 0.0 for k in MTF_FEATURES}
        # binary search for the last index with time + period <= t (fully closed)
        lo, hi, idx = 0, len(self.candles) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.candles[mid][0] + self.period <= t:
                idx, lo = mid, mid + 1
            else:
                hi = mid - 1
        if idx < 0:
            return {k: 0.0 for k in MTF_FEATURES}
        hc = self.candles[idx][2]
        ema = self.ema_fast[idx] if idx < len(self.ema_fast) else None
        prev = self.candles[idx - 1][2] if idx >= 1 else hc
        trend = ((hc - ema) / price) if (ema and price) else 0.0
        ret = (hc - prev) / prev if prev else 0.0
        dist = (price - hc) / price if price else 0.0
        return {"mtf_trend": trend, "mtf_ret": ret, "mtf_dist": dist}


def compute_row(candles: list[list], i: int,
                tick_summary: Optional[dict] = None,
                mtf: Optional[MTFContext] = None,
                require_summary: bool = False) -> Optional[dict]:
    """Feature dict for candle `i`, or None if there is not enough history."""
    base = base_features.compute_features(candles, i)
    if base is None:
        return None
    if require_summary and tick_summary is None:
        return None
    row = dict(base)
    row.update(_tick_block(tick_summary))
    price = candles[i][2]
    if mtf is not None:
        row.update(mtf.features(candles[i][0], price))
    else:
        row.update({k: 0.0 for k in MTF_FEATURES})
    return row


def to_vector(row: dict) -> list[float]:
    return [float(row[k]) for k in FEATURES]


def build_dataset(candles: list[list],
                  summaries: Optional[list[Optional[dict]]] = None,
                  mtf: Optional[MTFContext] = None,
                  require_summary: bool = False,
                  min_index: int = 50):
    """(X, y, idx, has_summary) for a single series.

    Label = did the NEXT candle close higher (1) or lower (0); a flat next
    candle is dropped, never guessed. `idx` are the source candle indices so a
    caller can split by time without recomputing features.
    """
    X, y, idx, has = [], [], [], []
    n = len(candles)
    for i in range(min_index, n - 1):
        summ = summaries[i] if summaries is not None else None
        row = compute_row(candles, i, summ, mtf, require_summary)
        if row is None:
            continue
        nxt = candles[i + 1][2]
        cur = candles[i][2]
        if nxt > cur:
            label = 1
        elif nxt < cur:
            label = 0
        else:
            continue
        X.append(to_vector(row))
        y.append(label)
        idx.append(i)
        has.append(summ is not None)
    return X, y, idx, has
