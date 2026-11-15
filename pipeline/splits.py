"""Chronological train/val/test splits by TIMESTAMP, with an embargo (Task 9).

The pooled rows of a (market, timeframe) dataset carry many instruments that
share the same candle timestamps. An index-based split would put the same
timestamp in two splits (train/test contamination), so the split is cut on the
timestamp itself: every row at a given time belongs to exactly one split, and an
embargo gap of `embargo` candles is dropped on each side of a boundary so a label
that reaches one candle forward cannot touch the next split.
"""
from __future__ import annotations

from .config import TRAIN_FRAC, VAL_FRAC, EMBARGO


def time_split(times: list[int], period: int, train_frac: float = TRAIN_FRAC,
               val_frac: float = VAL_FRAC, embargo: int = EMBARGO
               ) -> dict[str, list[int]]:
    """Return {train,val,test} -> row indices, split on the timestamp.

    `times` must be non-decreasing. Returns index lists (not ranges) because the
    boundary is a time value, and ties at that value all land on one side.
    """
    n = len(times)
    if n == 0:
        return {"train": [], "val": [], "test": []}
    cut1 = times[min(n - 1, int(n * train_frac))]
    cut2 = times[min(n - 1, int(n * (train_frac + val_frac)))]
    emb = embargo * period
    out: dict[str, list[int]] = {"train": [], "val": [], "test": []}
    for i, t in enumerate(times):
        if t < cut1:
            out["train"].append(i)
        elif t < cut1 + emb:
            continue                      # embargo gap 1 (dropped)
        elif t < cut2:
            out["val"].append(i)
        elif t < cut2 + emb:
            continue                      # embargo gap 2 (dropped)
        else:
            out["test"].append(i)
    return out


def counts(split: dict[str, list[int]]) -> dict[str, int]:
    return {k: len(v) for k, v in split.items()}
