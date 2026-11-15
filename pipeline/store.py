"""Storage for processed datasets (Task 10).

Writes ONLY to processed_data/ — never to the source database. Uses Parquet when
pyarrow is available (it is), falling back to gzip CSV otherwise. Each dataset
gets a JSON metadata sidecar with the fields the brief requires.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import OUT_DIR, VERSION, FEATURE_ORDER, LABEL_COLUMNS, META_COLUMNS


def _have_parquet() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


def dataset_dir(market: str, tf: str) -> Path:
    return Path(OUT_DIR) / market / tf


def write_dataset(market: str, tf: str, columns: dict[str, list],
                  metadata: dict) -> dict:
    """Persist one dataset + metadata. Returns a small summary dict."""
    d = dataset_dir(market, tf)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"dataset_version": VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "market_type": market, "timeframe": tf,
            "feature_list": FEATURE_ORDER, "label_columns": LABEL_COLUMNS,
            **metadata}
    files = {}
    if _have_parquet():
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.table({k: v for k, v in columns.items()})
        path = d / "data.parquet"
        pq.write_table(table, path, compression="zstd")
        files["data"] = str(path)
    else:
        path = d / "data.csv.gz"
        with gzip.open(path, "wt", newline="") as fh:
            keys = list(columns)
            fh.write(",".join(keys) + "\n")
            for i in range(meta.get("sample_count", len(next(iter(columns.values()))))):
                fh.write(",".join(_fmt(columns[k][i]) for k in keys) + "\n")
        files["data"] = str(path)
    mpath = d / "metadata.json"
    mpath.write_text(json.dumps(meta, indent=2, default=str))
    files["metadata"] = str(mpath)
    return {"files": files, "dir": str(d)}


def _fmt(v):
    if isinstance(v, float):
        return repr(v)
    return "" if v is None else str(v)


def fingerprint_dir(market: str, tf: str) -> Optional[dict]:
    p = dataset_dir(market, tf) / "metadata.json"
    if p.exists():
        return json.loads(p.read_text())
    return None
