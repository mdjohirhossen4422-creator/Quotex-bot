"""Automated data-leakage checks (Task 11).

Each check returns (ok: bool, detail: dict). They are deliberately able to FAIL:
`test_pipeline.py` runs every one against a deliberately leaky input and asserts
it is rejected, so a PASS here means something.

The core rule enforced: at prediction time T, a feature may only depend on data
available at or before T. Labels live in separate columns and are never inputs.
"""
from __future__ import annotations

from typing import Optional

from .config import FEATURE_ORDER, LABEL_COLUMNS


def check_target_not_in_features(feature_names: list[str]) -> tuple[bool, dict]:
    leak = sorted(set(feature_names) & set(LABEL_COLUMNS))
    # also catch any column literally named like a target
    suspicious = [f for f in feature_names if f in ("label", "next_ret",
                                                    "next_range", "next_dir")]
    return (not leak and not suspicious), {"overlap": leak, "suspicious": suspicious}


def check_chronological(times: list[int]) -> tuple[bool, dict]:
    """Times must be non-decreasing (ties are legitimate: assets share a boundary)."""
    bad = [i for i in range(1, len(times)) if times[i] < times[i - 1]]
    return (not bad), {"violations": len(bad), "first": bad[0] if bad else None}


def check_no_duplicate_keys(assets: list[str], times: list[int]) -> tuple[bool, dict]:
    """No (asset, time) may appear twice — that would double-count a sample."""
    seen: set = set()
    dups = 0
    for a, t in zip(assets, times):
        if (a, t) in seen:
            dups += 1
        else:
            seen.add((a, t))
    return (dups == 0), {"duplicate_keys": dups}


def check_future_feature_causality(candles: list[list], probe: int,
                                   builder=None) -> tuple[bool, dict]:
    """Future-permutation: mutate candles AFTER `probe` and require the feature
    value AT `probe` to be unchanged. If it changes, the feature peeks ahead.

    `builder` defaults to the real feature builder; tests pass a deliberately
    leaky one to prove this check can fail.
    """
    import copy

    if builder is None:
        from . import features as feat
        builder = feat.build_features
    mutated = copy.deepcopy(candles)
    for i in range(probe + 1, len(mutated)):
        mutated[i][2] = mutated[i][2] * 1.5 + 1.0   # wreck the future close
        mutated[i][3] = mutated[i][3] * 1.5 + 1.0
        mutated[i][4] = mutated[i][4] * 0.5
    a = builder(candles)
    b = builder(mutated)
    changed = []
    for k, va in a.items():
        vb = b.get(k)
        if vb is None:
            continue
        if _val(va, probe) != _val(vb, probe):
            changed.append(k)
    return (not changed), {"changed_features": changed}


def _val(arr, i):
    v = arr[i] if arr is not None and i < len(arr) else None
    if isinstance(v, float):
        return None if v != v else round(v, 12)
    return v


def check_htf_no_lookahead(higher_times: list[int], period: int, t: int,
                           used_index: Optional[int]) -> tuple[bool, dict]:
    """The higher-TF candle used at time t must have closed by t."""
    if used_index is None:
        return True, {"used": None}
    closed_at = higher_times[used_index] + period
    return (closed_at <= t), {"closed_at": closed_at, "t": t}


def check_split_disjoint(train_times: list[int], val_times: list[int],
                         test_times: list[int]) -> tuple[bool, dict]:
    """No timestamp may appear in more than one split, and order must hold."""
    s_tr, s_va, s_te = set(train_times), set(val_times), set(test_times)
    overlap = (s_tr & s_va) | (s_tr & s_te) | (s_va & s_te)
    ordered = (not train_times or not val_times or max(train_times) < min(val_times)) \
        and (not val_times or not test_times or max(val_times) < min(test_times))
    return (not overlap and ordered), {"overlap": len(overlap), "ordered": ordered}


def check_feature_label_correlation(features: dict, labels: list,
                                    thresh: float = 0.9) -> tuple[bool, dict]:
    """A feature that correlates ~perfectly with the label is a leak, not a signal.

    Pearson correlation of each numeric feature against the label. If any
    |r| >= thresh, the feature is almost certainly the target in disguise.
    """
    import math
    ys = [float(v) for v in labels]
    n = len(ys)
    if n < 3:
        return True, {"max_abs_corr": 0.0, "offenders": []}
    my = sum(ys) / n
    vy = sum((y - my) ** 2 for y in ys)
    offenders = []
    mx_corr = 0.0
    for k, col in features.items():
        xs = [float(v) for v in col if isinstance(v, (int, float))
              and math.isfinite(v)]
        if len(xs) != n or vy == 0:
            continue
        mxx = sum(xs) / n
        vx = sum((x - mxx) ** 2 for x in xs)
        if vx == 0:
            continue
        cov = sum((xs[i] - mxx) * (ys[i] - my) for i in range(n))
        r = cov / math.sqrt(vx * vy)
        mx_corr = max(mx_corr, abs(r))
        if abs(r) >= thresh:
            offenders.append((k, round(r, 4)))
    return (not offenders), {"max_abs_corr": round(mx_corr, 4),
                             "offenders": offenders}


def run_all(feature_names: list[str], times: list[int],
            assets: list[str] | None = None) -> dict:
    """The structural checks that do not need raw candles (always runnable)."""
    out = {}
    ok, d = check_target_not_in_features(feature_names)
    out["target_not_in_features"] = {"ok": ok, **d}
    ok, d = check_chronological(times)
    out["chronological"] = {"ok": ok, **d}
    if assets is not None:
        ok, d = check_no_duplicate_keys(assets, times)
        out["no_duplicate_keys"] = {"ok": ok, **d}
    return out
