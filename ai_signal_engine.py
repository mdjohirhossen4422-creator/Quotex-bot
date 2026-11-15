#!/usr/bin/env python3
"""AI Signal Engine — turns a stored series into a CALL / PUT / NO TRADE verdict.

Two decision paths, in priority order:

  1. **Model path** — if a model trained for this (source, timeframe) exists
     (`train_signal_model.py`), its `predict_proba` for the last *closed* candle
     gives the direction and confidence. A probability near 0.5 is a NO TRADE.
  2. **Heuristic path** — with no model yet (day 1), a transparent indicator +
     order-flow vote stands in. It is deliberately conservative: it only fires
     when several independent signals agree, and it abstains otherwise.

Honesty rules (the brief is explicit):

  * the verdict is read from candles that have **already closed**, so the
    earliest honest action is the open of the NEXT candle (`entry_plan`),
  * confidence is never rounded up to look like an edge; a weak reading is
    reported as NO TRADE,
  * the source (real vs OTC) is part of the model key, so a real-market model is
    never applied to OTC data or vice-versa,
  * no profitability claim is made anywhere — this emits signals, it does not
    trade.

The result is an `analysis.Analysis`, so the dashboard, scanner and Telegram
paths can consume it unchanged.

Usage
-----
    python ai_signal_engine.py EURUSD_otc 60
    python ai_signal_engine.py EURUSD 300 --db data/candles.db
"""
from __future__ import annotations

import argparse
import os
import pickle
from dataclasses import dataclass, field
from typing import Optional

from analysis import Analysis
from provenance import classify_source
from signal_data import SignalData
from signal_features import FEATURES, compute_row, MTFContext

MODEL_DIR = "models"
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_MTF_PERIOD = 300


class SignalModel:
    """A loaded, feature-checked model artifact."""

    def __init__(self, blob: dict, path: str = ""):
        self.model = blob["model"]
        self.features = blob["features"]
        self.source = blob.get("source", "")
        self.period = blob.get("period", 0)
        self.mtf_period = blob.get("mtf_period", 0)
        self.path = path
        if self.features != FEATURES:
            raise ValueError(
                f"model {path} was trained on a different feature set; "
                "retrain it")

    def prob_up(self, vector: list[float]) -> float:
        return float(self.model.predict_proba([vector])[0][1])


def load_model(source: str, period: int, model_dir: str = MODEL_DIR
               ) -> Optional[SignalModel]:
    path = os.path.join(model_dir, f"{source}_{period}s.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return SignalModel(pickle.load(f), path)


@dataclass
class Heuristic:
    """Transparent indicator + order-flow vote. Never claims an edge."""
    bull: int = 0
    bear: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return self.bull - self.bear

    def vote(self, cond: bool, side: str, text: str):
        if cond:
            if side == "CALL":
                self.bull += 1
            else:
                self.bear += 1
            self.reasons.append(text)


def _heuristic(row: dict) -> Heuristic:
    h = Heuristic()
    # trend: EMA fast above slow and price above the trend EMA
    h.vote(row["ema_fast_slow"] > 0, "CALL", "EMA(9) > EMA(21)")
    h.vote(row["ema_fast_slow"] < 0, "PUT", "EMA(9) < EMA(21)")
    h.vote(row["price_ema_trend"] > 0, "CALL", "price > EMA(50)")
    h.vote(row["price_ema_trend"] < 0, "PUT", "price < EMA(50)")
    # momentum: MACD histogram and its direction
    h.vote(row["macd_hist"] > 0 and row["macd_hist"] >= row["macd_hist_prev"],
           "CALL", "MACD hist rising > 0")
    h.vote(row["macd_hist"] < 0 and row["macd_hist"] <= row["macd_hist_prev"],
           "PUT", "MACD hist falling < 0")
    # oscillator extremes
    h.vote(row["rsi"] < 0.30, "CALL", "RSI oversold")
    h.vote(row["rsi"] > 0.70, "PUT", "RSI overbought")
    # order flow (from tick summaries): net tick direction and momentum
    h.vote(row["tk_dir_ratio"] > 0.15 and row["tk_momentum"] > 0,
           "CALL", "net ticks up")
    h.vote(row["tk_dir_ratio"] < -0.15 and row["tk_momentum"] < 0,
           "PUT", "net ticks down")
    # higher-timeframe agreement
    h.vote(row["mtf_trend"] > 0, "CALL", "higher-TF trend up")
    h.vote(row["mtf_trend"] < 0, "PUT", "higher-TF trend down")
    return h


class AISignalEngine:
    def __init__(self, db: str = "data/candles.db", model_dir: str = MODEL_DIR,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 mtf_period: int = DEFAULT_MTF_PERIOD, min_score: int = 3):
        self.db = db
        self.model_dir = model_dir
        self.min_confidence = min_confidence
        self.mtf_period = mtf_period
        self.min_score = min_score

    def evaluate(self, asset: str, period: int,
                 candles: Optional[list[list]] = None,
                 summaries: Optional[list] = None,
                 mtf: Optional[MTFContext] = None) -> Analysis:
        source = classify_source(asset)
        sd = None
        if candles is None:
            sd = SignalData(self.db)
            candles, summaries, mtf = sd.load_with_mtf(asset, period,
                                                       self.mtf_period)
            sd.close()
        res = Analysis(asset=asset, period=period, candles=len(candles),
                       enough=False, engine="ai")
        if len(candles) < 55:
            res.note = f"need >=55 candles, have {len(candles)}"
            return res

        i = len(candles) - 1
        summ = summaries[i] if summaries else None
        row = compute_row(candles, i, summ, mtf)
        if row is None:
            res.note = "not enough indicator history"
            return res
        res.enough = True

        model = load_model(source, period, self.model_dir)
        if model is not None:
            prob = model.prob_up([row[k] for k in FEATURES])
            res.values = {"model_prob_up": round(prob, 4),
                          "model": os.path.basename(model.path)}
            conf = max(prob, 1 - prob)
            res.confidence = round(conf, 4)
            if conf < self.min_confidence:
                res.action = "NEUTRAL"
                res.note = (f"model confidence {conf:.2f} < "
                            f"{self.min_confidence:.2f} -> NO TRADE")
            else:
                res.action = "CALL" if prob >= 0.5 else "PUT"
                res.reasons = [f"model p(up)={prob:.3f}",
                               f"source={source}"]
        else:
            h = _heuristic(row)
            res.values = {"heuristic_score": h.score,
                          "tick_dir_ratio": row["tk_dir_ratio"],
                          "rsi": round(row["rsi"], 3)}
            res.reasons = h.reasons + [f"source={source} (no trained model yet)"]
            res.score = h.score
            # confidence: how lopsided the vote is, capped below a certainty the
            # heuristic cannot honestly claim
            res.confidence = round(min(0.5 + abs(h.score) * 0.05, 0.75), 3)
            if abs(h.score) < self.min_score:
                res.action = "NEUTRAL"
                res.note = (f"heuristic score {h.score} (need |score|>="
                            f"{self.min_score}) -> NO TRADE")
            else:
                res.action = "CALL" if h.score > 0 else "PUT"
        res.note = res.note or f"source={source}"
        return res


def entry_plan(period: int, now: float, expiry_candles: int = 1) -> dict:
    """Earliest honest entry (next candle boundary) and its expiry."""
    import math
    entry = (int(now // period) + 1) * period
    expiry = entry + expiry_candles * period
    return {"entry_at": entry, "expiry_at": expiry,
            "entry_in": max(0, int(entry - now)),
            "expires_in": max(0, int(expiry - now)),
            "expiry_candles": expiry_candles}


def _format(res: Analysis) -> str:
    if not res.enough:
        return f"{res.asset} {res.period}s: NOT ENOUGH DATA ({res.note})"
    head = {"CALL": "CALL (up)", "PUT": "PUT (down)",
            "NEUTRAL": "NO TRADE"}.get(res.action, res.action)
    lines = [f"{res.asset} {res.period}s -> {head}  "
             f"confidence={res.confidence:.2f}"]
    if res.values:
        lines.append("  values: " + ", ".join(f"{k}={v}" for k, v in res.values.items()))
    for r in res.reasons[:8]:
        lines.append(f"  - {r}")
    if res.note:
        lines.append(f"  note: {res.note}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="AI Signal Engine (single pair)")
    ap.add_argument("asset")
    ap.add_argument("period", type=int)
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    ap.add_argument("--mtf-period", type=int, default=DEFAULT_MTF_PERIOD)
    args = ap.parse_args()
    eng = AISignalEngine(args.db, args.model_dir, args.min_confidence,
                         args.mtf_period)
    res = eng.evaluate(args.asset, args.period)
    print(_format(res))
    import time
    if res.action in ("CALL", "PUT"):
        print("  entry plan:", entry_plan(args.period, time.time()))


if __name__ == "__main__":
    main()
