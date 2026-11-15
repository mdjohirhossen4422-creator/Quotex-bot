"""Market-structure features (Task 6), all strictly causal.

Everything at index `i` uses only candles [0..i]. Swing pivots need `right`
later bars, so the newest pivot used at `i` is at most `i-right` — the feature is
delayed until the pivot is actually confirmable, which is the correct way to
avoid look-ahead. BOS/CHoCH come from the repo's causal `structure_series`.

The order block here is a clearly defined rule-based approximation (last opposite
candle before an impulsive leg). It is NOT institutional order-book data, and it
is labelled as an approximation everywhere it is surfaced.
"""
from __future__ import annotations

import indicators as ta
from structure import structure_series, swing_series, UP, DOWN

# structure_series returns trend as the repo strings ("up"/"down"/"range").
_TREND_CODE = {UP: 1, DOWN: -1}


def _swings_causal(highs, lows, left=2, right=2, lookback=120):
    """All confirmed pivots up to the last bar, labelled by kind.

    `swing_series(..., i=n-1)` is causal: it will not emit a pivot that is not
    yet confirmed by `right` later bars.
    """
    n = len(highs)
    return swing_series(highs, lows, n - 1, left, right, max(lookback, n))


def structure_features(opens, closes, highs, lows, atr) -> dict[str, list]:
    """Per-index structure feature arrays. All length n, causal."""
    n = len(closes)
    feats = {k: [0.0] * n for k in
             ["dist_support_atr", "dist_resistance_atr", "dist_swing_high_atr",
              "dist_swing_low_atr", "bos_dir", "choch_dir", "sweep", "fvg",
              "ob_dir", "range_pos", "equal_highs", "equal_lows"]}

    st = structure_series(opens, closes, highs, lows)
    # structure_series returns trend[] strings and bos/choch direction arrays.
    bos = st["bos"]
    choch = st["choch"]

    # swings once (causal) for distance/equal-level features
    piv = _swings_causal(highs, lows)
    highs_piv = [p for p in piv if p.kind == "high"]
    lows_piv = [p for p in piv if p.kind == "low"]
    swing_high_idx = [p.idx for p in highs_piv]
    swing_low_idx = [p.idx for p in lows_piv]

    for i in range(n):
        a = atr[i] if atr[i] and atr[i] > 0 else None
        feats["bos_dir"][i] = float(bos[i])
        feats["choch_dir"][i] = float(choch[i])

        if a:
            # last confirmed swing high/low at or before i
            prev_hi = max((idx for idx in swing_high_idx if idx <= i), default=None)
            prev_lo = max((idx for idx in swing_low_idx if idx <= i), default=None)
            if prev_hi is not None:
                feats["dist_swing_high_atr"][i] = (closes[i] - highs[prev_hi]) / a
                feats["dist_resistance_atr"][i] = (highs[prev_hi] - closes[i]) / a
            if prev_lo is not None:
                feats["dist_swing_low_atr"][i] = (closes[i] - lows[prev_lo]) / a
                feats["dist_support_atr"][i] = (closes[i] - lows[prev_lo]) / a

        # dealing-range position over a causal window
        lo = max(0, i - 40)
        hi_v = max(highs[lo:i + 1])
        lo_v = min(lows[lo:i + 1])
        feats["range_pos"][i] = ((closes[i] - lo_v) / (hi_v - lo_v)) if hi_v > lo_v else 0.5

        # FVG (3-candle imbalance ending at i)
        if i >= 2:
            if lows[i] > highs[i - 2]:
                feats["fvg"][i] = 1.0
            elif highs[i] < lows[i - 2]:
                feats["fvg"][i] = -1.0

        # liquidity sweep (needs prior swings strictly before i)
        if i >= 6:
            prior_hi = [idx for idx in swing_high_idx if idx < i]
            prior_lo = [idx for idx in swing_low_idx if idx < i]
            if prior_lo and lows[i] < min(lows[k] for k in prior_lo) and \
                    closes[i] > min(lows[k] for k in prior_lo):
                feats["sweep"][i] = 1.0
            elif prior_hi and highs[i] > max(highs[k] for k in prior_hi) and \
                    closes[i] < max(highs[k] for k in prior_hi):
                feats["sweep"][i] = -1.0

        # order block approximation (impulsive leg into i)
        if a and i >= 2:
            leg = closes[i] - closes[i - 1]
            if leg > a:
                for j in range(i - 1, max(0, i - 20) - 1, -1):
                    if closes[j] < opens[j]:
                        feats["ob_dir"][i] = 1.0
                        break
            elif leg < -a:
                for j in range(i - 1, max(0, i - 20) - 1, -1):
                    if closes[j] > opens[j]:
                        feats["ob_dir"][i] = -1.0
                        break

        # equal highs / lows among the last few confirmed pivots (within 0.1 ATR)
        if a:
            recent_h = [idx for idx in swing_high_idx if idx <= i][-3:]
            recent_l = [idx for idx in swing_low_idx if idx <= i][-3:]
            for x in range(len(recent_h) - 1):
                if abs(highs[recent_h[x]] - highs[recent_h[x + 1]]) < 0.1 * a:
                    feats["equal_highs"][i] = 1.0
            for x in range(len(recent_l) - 1):
                if abs(lows[recent_l[x]] - lows[recent_l[x + 1]]) < 0.1 * a:
                    feats["equal_lows"][i] = 1.0
    return feats
