#!/usr/bin/env python3
"""On-demand signal analysis for a chosen (asset, timeframe).

Given the stored candles, compute the indicator set and produce an explainable
verdict: CALL / PUT / NEUTRAL, a strength score, the reasons behind it, and the
raw indicator values. This is what the dashboard's "Analyze" button calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import indicators as ta
from signal_bot import Strategy


@dataclass
class Analysis:
    asset: str
    period: int
    candles: int
    enough: bool
    action: str = "NEUTRAL"       # CALL | PUT | NEUTRAL
    score: int = 0
    bull: int = 0
    bear: int = 0
    reasons: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)
    note: str = ""
    engine: str = "rule"          # "rule" | "smart"
    regime: str = ""              # trend | range (smart only)
    confidence: float = 0.0       # 0..1 (smart only)


def analyze_smart(asset: str, period: int, candles: list[list],
                  strategy=None) -> Analysis:
    """Regime-aware analysis (ADX + volatility gate + breakout confirmation).

    Uses `smart_engine.SmartEngine`. Returns the same `Analysis` shape so the
    dashboard can render either engine. Confidence is the engine's 0..1 score;
    an action only appears when confidence clears the engine's own gate.
    """
    from smart_engine import SmartEngine, SmartStrategy

    eng = SmartEngine(strategy or SmartStrategy())
    res = Analysis(asset=asset, period=period, candles=len(candles),
                   enough=len(candles) >= eng.s.min_candles(), engine="smart")
    if not res.enough:
        need = eng.s.min_candles()
        res.note = (f"need {need} candles, have {len(candles)} — "
                    f"let it stream for {(need - len(candles)) * period}s more")
        return res

    sig = eng.evaluate(candles)
    if sig is None:
        res.note = "no setup: regime/volatility gate rejected this candle"
        return res
    res.action = sig.action
    res.regime = sig.regime
    res.confidence = sig.confidence
    res.reasons = sig.reasons
    res.score = round(sig.confidence * 10)
    res.values = {"price": sig.price, "last_candle_time": sig.time}
    return res


def analyze_ensemble(asset: str, period: int, candles: list[list],
                     strategy=None) -> Analysis:
    """Three-model confluence analysis (SMC + trend + momentum).

    Uses `ensemble.EnsembleEngine`. Same `Analysis` shape as the others so the
    dashboard can render it; `values` carries the per-model votes.
    """
    from ensemble import EnsembleEngine, EnsembleStrategy

    eng = EnsembleEngine(strategy or EnsembleStrategy())
    res = Analysis(asset=asset, period=period, candles=len(candles),
                   enough=len(candles) >= eng.s.min_candles(), engine="ensemble")
    if not res.enough:
        need = eng.s.min_candles()
        res.note = (f"need {need} candles, have {len(candles)} — "
                    f"let it stream for {(need - len(candles)) * period}s more")
        return res

    sig = eng.evaluate(candles)
    if sig is None:
        res.note = "no setup: fewer than 2 of 3 models agree with the trend"
        return res
    res.action = sig.action
    res.confidence = sig.confidence
    res.score = sig.score
    res.reasons = sig.reasons
    res.values = {"price": sig.price, "last_candle_time": sig.time,
                  "votes": sig.votes}
    return res


def analyze_pa(asset: str, period: int, candles: list[list],
               strategy=None) -> Analysis:
    """Indicator + price action + SMC confluence (no training; `pabot.PAEngine`).

    Same `Analysis` shape as the other adapters so the dashboard, scanner and
    alerts can all render it. `values` carries the per-family block votes.
    """
    from pabot import PAEngine, PAStrategy

    eng = PAEngine(strategy or PAStrategy())
    res = Analysis(asset=asset, period=period, candles=len(candles),
                   enough=len(candles) >= eng.s.min_candles(), engine="pa")
    if not res.enough:
        need = eng.s.min_candles()
        res.note = (f"need {need} candles, have {len(candles)} — "
                    f"let it stream for {(need - len(candles)) * period}s more")
        return res

    sig = eng.evaluate(asset, candles)
    if sig is None:
        res.note = ("no setup: fewer than "
                    f"{eng.s.min_blocks} families (indicator/price-action/SMC) agree")
        return res
    res.action = sig.action
    res.regime = sig.regime
    res.confidence = sig.confidence
    res.score = sig.score
    res.reasons = sig.reasons
    res.bull = sum(1 for v in sig.blocks.values() if v == "CALL")
    res.bear = sum(1 for v in sig.blocks.values() if v == "PUT")
    res.values = {"price": sig.price, "last_candle_time": sig.time,
                  "blocks": sig.blocks}
    return res


def analyze(asset: str, period: int, candles: list[list],
            strategy: Optional[Strategy] = None, threshold: int = 4) -> Analysis:
    s = strategy or Strategy()
    need = s.min_candles()
    res = Analysis(asset=asset, period=period, candles=len(candles), enough=len(candles) >= need)
    if not res.enough:
        res.note = (f"need {need} candles, have {len(candles)} — "
                    f"let it stream for {(need - len(candles)) * period}s more")
        return res

    closes = [c[2] for c in candles]
    highs = [c[3] for c in candles]
    lows = [c[4] for c in candles]
    i = len(candles) - 1

    ema_f = ta.ema(closes, s.ema_fast)
    ema_s = ta.ema(closes, s.ema_slow)
    ema_t = ta.ema(closes, s.ema_trend)
    rsi = ta.rsi(closes, s.rsi_period)
    macd_line, sig_line, hist = ta.macd(closes, s.macd_fast, s.macd_slow, s.macd_signal)
    bb_u, bb_m, bb_l = ta.bollinger(closes, s.bb_period, s.bb_mult)
    st_k, st_d = ta.stochastic(highs, lows, closes, s.stoch_period, s.stoch_k, s.stoch_d)
    atr = ta.atr(highs, lows, closes, 14)

    bull = bear = 0
    reasons: list[str] = []

    if ema_f[i] > ema_s[i]:
        bull += 2; reasons.append(f"EMA{s.ema_fast} > EMA{s.ema_slow} (uptrend)")
    else:
        bear += 2; reasons.append(f"EMA{s.ema_fast} < EMA{s.ema_slow} (downtrend)")

    if closes[i] > ema_t[i]:
        bull += 1; reasons.append(f"price above EMA{s.ema_trend}")
    else:
        bear += 1; reasons.append(f"price below EMA{s.ema_trend}")

    if ta.crossed_above(ema_f, ema_s, i):
        bull += 2; reasons.append("fresh EMA bullish cross")
    elif ta.crossed_below(ema_f, ema_s, i):
        bear += 2; reasons.append("fresh EMA bearish cross")

    if macd_line[i] is not None and sig_line[i] is not None:
        if ta.crossed_above(macd_line, sig_line, i):
            bull += 2; reasons.append("MACD bullish cross")
        elif ta.crossed_below(macd_line, sig_line, i):
            bear += 2; reasons.append("MACD bearish cross")
        elif macd_line[i] > sig_line[i]:
            bull += 1; reasons.append("MACD above signal")
        else:
            bear += 1; reasons.append("MACD below signal")

    if rsi[i] is not None:
        if rsi[i] <= s.rsi_oversold:
            bull += 1; reasons.append(f"RSI oversold ({rsi[i]:.0f})")
        elif rsi[i] >= s.rsi_overbought:
            bear += 1; reasons.append(f"RSI overbought ({rsi[i]:.0f})")

    if st_k[i] is not None and st_d[i] is not None:
        if ta.crossed_above(st_k, st_d, i):
            bull += 1; reasons.append("Stochastic bullish cross")
        elif ta.crossed_below(st_k, st_d, i):
            bear += 1; reasons.append("Stochastic bearish cross")

    if bb_l[i] is not None and closes[i] < bb_l[i]:
        bull += 1; reasons.append("close below lower Bollinger band")
    elif bb_u[i] is not None and closes[i] > bb_u[i]:
        bear += 1; reasons.append("close above upper Bollinger band")

    net = bull - bear
    res.bull, res.bear, res.score = bull, bear, abs(net)
    if abs(net) >= threshold:
        res.action = "CALL" if net > 0 else "PUT"
    else:
        res.action = "NEUTRAL"
        res.note = f"score {abs(net)} below threshold {threshold} — no clear edge"

    res.reasons = reasons
    res.values = {
        "price": round(closes[i], 6),
        "ema_fast": _r(ema_f[i]), "ema_slow": _r(ema_s[i]), "ema_trend": _r(ema_t[i]),
        "rsi": _r(rsi[i], 1),
        "macd": _r(macd_line[i]), "macd_signal": _r(sig_line[i]), "macd_hist": _r(hist[i]),
        "bb_upper": _r(bb_u[i]), "bb_mid": _r(bb_m[i]), "bb_lower": _r(bb_l[i]),
        "stoch_k": _r(st_k[i], 1), "stoch_d": _r(st_d[i], 1),
        "atr": _r(atr[i]),
        "last_candle_time": candles[i][0],
    }
    return res


def _r(v, nd: int = 5):
    return round(v, nd) if isinstance(v, (int, float)) else None


def analyze_discipline(asset: str, period: int, candles: list[list],
                       strategy=None) -> Analysis:
    """Real-trade discipline gate (`discipline.assess`).

    This is the **abstain-by-default** engine: it runs a 7-gate checklist
    (data quality, regime, structure, confluence, agreement, strength) and
    returns NEUTRAL unless every gate passes. When they do, it still labels the
    read a *candidate* until the live journal has *measured* a payout-adjusted
    edge for this timeframe — so it never presents an unproven setup as a
    tradeable signal. Same `Analysis` shape as the other adapters.
    """
    from discipline import assess, edge_proven, DisciplineConfig, NO_TRADE
    from pabot import PAEngine, PAStrategy

    cfg = strategy if isinstance(strategy, DisciplineConfig) else DisciplineConfig()
    need = PAEngine(PAStrategy()).s.min_candles()
    res = Analysis(asset=asset, period=period, candles=len(candles),
                   enough=len(candles) >= need, engine="discipline")
    if not res.enough:
        res.note = (f"need {need} candles, have {len(candles)} — "
                    f"let it stream for {(need - len(candles)) * period}s more")
        return res

    try:
        proven, edge = edge_proven(period, cfg)
    except Exception:
        proven, edge = False, {"decisive": 0, "hit": 0.0, "ci": [0.0, 0.0],
                               "breakeven": cfg.breakeven()}
    st = assess(None, asset, period, cfg, candles=candles, proven=proven)

    res.action = "NEUTRAL" if st.action == NO_TRADE else st.action
    res.confidence = st.confidence
    res.score = round(st.confidence * 10)
    res.regime = st.regime
    res.bull = sum(1 for v in st.blocks.values() if v == "CALL")
    res.bear = sum(1 for v in st.blocks.values() if v == "PUT")
    res.reasons = list(st.reasons) + [
        f"{'✓' if ok else '✗'} {name}: {detail}" for name, ok, detail in st.checks]
    res.values = {"price": st.price, "last_candle_time": st.time,
                  "structure": st.structure, "quality": round(st.quality, 3),
                  "blocks": st.blocks}
    if st.status == "no_trade":
        res.note = "NO TRADE — blocked by: " + ", ".join(st.blocked_by)
    elif st.status == "candidate":
        res.note = (f"CANDIDATE (edge not yet proven): {edge['decisive']} settled, "
                    f"hit {edge['hit']:.1%}, CI {edge['ci'][0]:.1%}–{edge['ci'][1]:.1%} "
                    f"vs breakeven {edge['breakeven']:.1%}")
    else:
        res.note = f"SIGNAL — edge proven ({edge['decisive']} settled, hit {edge['hit']:.1%})"
    return res
