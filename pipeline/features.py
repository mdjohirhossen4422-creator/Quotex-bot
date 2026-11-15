"""Technical indicators + candle geometry (Task 4).

Indicator maths reuse the repo's tested `indicators.py` (pure Python, causal,
left-padded with None) so Phase 2 cannot silently diverge from the rest of the
project. The genuinely vectorised bits (rolling std, normalised volatility,
StochRSI) use numpy. Every output is length n and uses only candles [0..i].
"""
from __future__ import annotations

import numpy as np

import indicators as ta


def _arr(xs, dtype=float) -> np.ndarray:
    return np.asarray(xs, dtype=dtype)


def _safe_div(a, b, eps=1e-12):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    return a / np.where(np.abs(b) < eps, np.nan, b)


def stochrsi(closes: list[float], period: int = 14, smooth: int = 3
             ) -> tuple[list, list]:
    """Stochastic RSI: position of RSI within its own recent range, smoothed."""
    r = np.asarray(ta.rsi(closes, period), dtype=float)
    n = len(r)
    k = np.full(n, np.nan)
    for i in range(period, n):
        window = r[i - period + 1:i + 1]
        window = window[~np.isnan(window)]
        if len(window) < period:
            continue
        lo, hi = window.min(), window.max()
        k[i] = 50.0 if hi == lo else (r[i] - lo) / (hi - lo) * 100.0
    d = _sma_np(k, smooth)
    return _none(k), _none(d)


def _none(arr) -> list:
    """NaN -> None, matching indicators.py's convention (and making rows
    reproducible: NaN != NaN would break equality checks)."""
    return [None if (v is None or (isinstance(v, float) and v != v)) else float(v)
            for v in arr]


def _sma_np(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if period <= 0:
        return out
    valid = ~np.isnan(x)
    c = np.cumsum(np.where(valid, x, 0.0))
    for i in range(period - 1, len(x)):
        seg = valid[i - period + 1:i + 1]
        if seg.all():
            s = c[i] - (c[i - period] if i - period >= 0 else 0.0)
            out[i] = s / period
    return out


def rolling_std(values: list[float], period: int = 20) -> list:
    x = _arr(values)
    out = np.full(len(x), np.nan)
    for i in range(period - 1, len(x)):
        out[i] = x[i - period + 1:i + 1].std()
    return _none(out)


def build_features(candles: list[list]) -> dict[str, list]:
    """All Task-4 features for one series. `candles` = [t,o,c,h,l] chronological."""
    o = [c[1] for c in candles]
    cl = [c[2] for c in candles]
    hi = [c[3] for c in candles]
    lo = [c[4] for c in candles]
    n = len(candles)

    f: dict[str, list] = {}
    ema9 = ta.ema(cl, 9)
    ema21 = ta.ema(cl, 21)
    ema50 = ta.ema(cl, 50)
    ema200 = ta.ema(cl, 200)
    sma20 = ta.sma(cl, 20)
    sma50 = ta.sma(cl, 50)
    f["ema_9_pct"] = _pct(ema9, cl)
    f["ema_21_pct"] = _pct(ema21, cl)
    f["ema_50_pct"] = _pct(ema50, cl)
    f["ema_200_pct"] = _pct(ema200, cl)
    f["sma_20_pct"] = _pct(sma20, cl)
    f["sma_50_pct"] = _pct(sma50, cl)

    # trend ratios/slopes (scale-free)
    f["ema9_over_ema21"] = _ratio(ema9, ema21)
    f["ema21_over_ema50"] = _ratio(ema21, ema50)
    f["ema9_slope"] = _slope(ema9, cl)
    f["ema21_slope"] = _slope(ema21, cl)

    # momentum
    f["rsi_7"] = ta.rsi(cl, 7)
    f["rsi_14"] = ta.rsi(cl, 14)
    macd, sig, hist = ta.macd(cl)
    f["macd_pct"] = _pct(macd, cl)
    f["macd_signal_pct"] = _pct(sig, cl)
    f["macd_hist_pct"] = _pct(hist, cl)
    k, d = stochrsi(cl)
    f["stochrsi_k"], f["stochrsi_d"] = k, d

    # volatility (all relative to close)
    atr = ta.atr(hi, lo, cl, 14)
    f["atr_pct"] = _pct(atr, cl)
    bu, bm, bl = ta.bollinger(cl, 20, 2.0)
    f["bb_width"] = [ (bu[i] - bl[i]) / bm[i] if (bu[i] is not None and bm[i]) else None
                      for i in range(n)]
    f["bb_pctb"] = [ (cl[i] - bl[i]) / (bu[i] - bl[i])
                     if (bu[i] is not None and bu[i] != bl[i]) else None
                     for i in range(n)]
    f["roll_std_pct"] = _pct(rolling_std(cl, 20), cl)

    # candle geometry (all relative to close)
    rng = [hi[i] - lo[i] for i in range(n)]
    body = [abs(cl[i] - o[i]) for i in range(n)]
    up_wick = [hi[i] - max(o[i], cl[i]) for i in range(n)]
    lo_wick = [min(o[i], cl[i]) - lo[i] for i in range(n)]
    f["candle_range_pct"] = [ (rng[i] / cl[i]) if cl[i] else None for i in range(n)]
    f["body_pct"] = [ (body[i] / cl[i]) if cl[i] else None for i in range(n)]
    f["upper_wick_pct"] = [ (up_wick[i] / cl[i]) if cl[i] else None for i in range(n)]
    f["lower_wick_pct"] = [ (lo_wick[i] / cl[i]) if cl[i] else None for i in range(n)]
    f["body_to_range"] = [ (body[i] / rng[i]) if rng[i] else 0.0 for i in range(n)]
    f["close_pos"] = [ (cl[i] - lo[i]) / rng[i] if rng[i] else 0.5 for i in range(n)]
    f["ret_1"] = _ret(cl, 1)
    f["ret_2"] = _ret(cl, 2)
    f["ret_3"] = _ret(cl, 3)
    f["bull_streak"] = _streak(o, cl, up=True)
    f["bear_streak"] = _streak(o, cl, up=False)
    return f


def _pct(vals, closes):
    """(value - close) / close — keeps a price-level indicator scale-free."""
    return [ (vals[i] - closes[i]) / closes[i]
             if (vals[i] is not None and closes[i]) else None
             for i in range(len(closes))]


def _ratio(a, b):
    return [ (a[i] / b[i]) if (a[i] is not None and b[i]) else None for i in range(len(a))]


def _slope(ema_vals, closes):
    out = []
    for i in range(len(ema_vals)):
        if ema_vals[i] is None or closes[i] in (None, 0):
            out.append(None)
        else:
            out.append((ema_vals[i] - closes[i]) / closes[i])
    return out


def _ret(closes, k):
    n = len(closes)
    out = [None] * n
    for i in range(k, n):
        if closes[i - k]:
            out[i] = (closes[i] - closes[i - k]) / closes[i - k]
    return out


def _streak(opens, closes, up: bool):
    n = len(closes)
    out = [0] * n
    run = 0
    for i in range(n):
        bull = closes[i] > opens[i]
        if (bull and up) or ((not bull) and (not up)):
            run += 1
        else:
            run = 0
        out[i] = run
    return out
