"""Phase 2 configuration — timeframes, paths, and the exact feature contract.

Everything that defines a dataset lives here so the pipeline is reproducible:
change a constant, re-run, get the same columns in the same order.
"""
from __future__ import annotations

from pathlib import Path

# --- paths ----------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
SRC_DB = ROOT / "data" / "candles.db"          # read-only source of truth
OUT_DIR = ROOT / "processed_data"
VERSION = "phase2-v1"

# --- timeframes ------------------------------------------------------------ #
# Prediction timeframe -> the native stored candle period it is built from.
TIMEFRAMES: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}

# --- markets --------------------------------------------------------------- #
MARKETS = ("real", "otc")

# --- minimums -------------------------------------------------------------- #
MIN_CANDLES_PER_ASSET = 150        # "sufficient" marker for the report
HARD_MIN_ROWS = 10                 # below this, do not write a dataset at all
MIN_TICKS_PER_CANDLE = 1           # a candle with 0 ticks is not stored
EMA_WARMUP = 200                   # EMA200 needs this many bars before it is valid

# --- label definition ------------------------------------------------------ #
# SIDEWAYS band as a multiple of the candle's own ATR(14) at time T. A fixed
# threshold would label quiet pairs as all-SIDEWAYS and wild pairs as never; a
# volatility-relative band keeps the classes comparable across instruments.
SIDEWAYS_ATR_MULT = 0.25

# --- split ---------------------------------------------------------------- #
TRAIN_FRAC, VAL_FRAC, TEST_FRAC = 0.70, 0.15, 0.15
EMBARGO = 1                        # extra candles dropped at each split boundary

# --- feature groups (order is the column order) --------------------------- #
# Every feature is scale-free on purpose (a ratio, a % of close, or a bounded
# oscillator) so rows from different instruments can be pooled. Price-level
# indicators (EMA/SMA/Bollinger/MACD/ATR/std/range) are stored as a percentage
# of the candle close rather than an absolute price.
TREND_FEATURES = ["ema_9_pct", "ema_21_pct", "ema_50_pct", "ema_200_pct",
                  "sma_20_pct", "sma_50_pct", "ema9_over_ema21",
                  "ema21_over_ema50", "ema9_slope", "ema21_slope"]
MOMENTUM_FEATURES = ["rsi_7", "rsi_14", "macd_pct", "macd_signal_pct",
                     "macd_hist_pct", "stochrsi_k", "stochrsi_d"]
VOLATILITY_FEATURES = ["atr_pct", "bb_width", "bb_pctb", "roll_std_pct"]
GEOMETRY_FEATURES = ["candle_range_pct", "body_pct", "upper_wick_pct",
                     "lower_wick_pct", "body_to_range", "close_pos",
                     "ret_1", "ret_2", "ret_3", "bull_streak", "bear_streak"]
TICK_FEATURES = ["tick_count", "tick_velocity", "avg_tick_interval",
                 "tick_up_count", "tick_down_count", "tick_flat_count",
                 "tick_dir_ratio", "tick_momentum", "tick_short_vol",
                 "tick_accel", "tick_range", "broker_dir_ratio",
                 "tick_activity"]
STRUCTURE_FEATURES = ["dist_support_atr", "dist_resistance_atr",
                      "dist_swing_high_atr", "dist_swing_low_atr",
                      "bos_dir", "choch_dir", "sweep", "fvg",
                      "ob_dir", "range_pos", "equal_highs", "equal_lows"]
MTF_FEATURES = ["trend_1m", "trend_5m", "trend_15m", "trend_30m"]

FEATURE_ORDER: list[str] = (TREND_FEATURES + MOMENTUM_FEATURES +
                            VOLATILITY_FEATURES + GEOMETRY_FEATURES +
                            TICK_FEATURES + STRUCTURE_FEATURES + MTF_FEATURES)

# Labels are NEVER in FEATURE_ORDER; kept separate and asserted against in tests.
LABEL_COLUMNS = ["label", "next_ret", "next_range", "next_dir", "label_ts"]
META_COLUMNS = ["asset", "market_type", "timeframe", "time", "open", "high",
                "low", "close", "tick_count", "price_change", "source",
                "completeness_status"]
