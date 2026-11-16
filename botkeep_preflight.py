#!/usr/bin/env python3
"""Botkeep readiness preflight for the live collector (Phase 2.6).

Read-only. Run it *on the Botkeep instance* (or locally) to confirm the box can
host `live_collector.py` before deploying. It checks the things a 512 MB / 0.5
vCore / 1 GiB free plan can actually fail on and prints PASS / FAIL / WARN:

  1. Python version (needs >= 3.9 for the type hints used),
  2. the collector's runtime imports resolve with a minimal dependency set,
  3. outbound WebSocket reachability to the feed host,
  4. database size vs a storage budget (default 1 GiB) and growth headroom,
  5. current RSS vs a memory budget (default 512 MiB),
  6. that the DB/log/state paths are writable.

Exit code 0 if no check FAILs, 1 otherwise. It never writes to the DB.

Usage
-----
    python botkeep_preflight.py
    python botkeep_preflight.py --db data/candles.db --storage-mb 1024 --ram-mb 512
"""
from __future__ import annotations

import argparse
import os
import ssl
import sqlite3
import sys
from pathlib import Path

MIN_PY = (3, 9)
FEED_HOST = "ws2.market-qx.info"
FEED_PORT = 443


def _ok(name, detail, status="PASS"):
    """Print one check. Returns True unless the status is FAIL (WARN is fine)."""
    print(f"[{status}] {name}: {detail}")
    return status != "FAIL"


def check_python():
    v = sys.version_info
    return _ok("python", f"{v.major}.{v.minor}.{v.micro}",
               "PASS" if v[:2] >= MIN_PY else "FAIL")


def check_imports():
    missing = []
    for mod in ("websocket",):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return _ok("deps", "websocket-client present" if not missing
               else f"missing {missing}", "PASS" if not missing else "FAIL")


def check_feed(proxy: str = ""):
    """Do a real WebSocket handshake, not just a TCP connect.

    Cloudflare answers the TCP connection but returns `403` on the *handshake*
    when the source IP is geo/Cloudflare-blocked. A bare TCP probe would PASS and
    hide that, so this performs the actual upgrade and reports the status.
    """
    try:
        import websocket
        from market_qx_candles import MarketQX
    except ImportError as e:
        return _ok("feed", f"cannot import websocket client: {e}", "FAIL")

    qx = MarketQX(proxy=proxy)
    url = "wss://%s/socket.io/?EIO=3&transport=websocket" % FEED_HOST
    try:
        ws = websocket.create_connection(
            url, header=qx._headers(), timeout=15,
            sslopt={"cert_reqs": ssl.CERT_NONE}, **qx._proxy_opts())
        ws.close()
        via = f" via {proxy}" if proxy else ""
        return _ok("feed", f"handshake OK to {FEED_HOST}{via}")
    except websocket.WebSocketBadStatusException as e:
        status = getattr(e, "status_code", None)
        hint = ""
        if status == 403:
            hint = (" — Cloudflare is refusing this IP (geo/bot block). "
                    "Set QX_PROXY to an allowed-region proxy and retry.")
        return _ok("feed", f"handshake HTTP {status} from {FEED_HOST}{hint}", "FAIL")
    except Exception as e:
        return _ok("feed", f"cannot reach {FEED_HOST}: {type(e).__name__}: {e}", "FAIL")


def _db_bytes(db: str) -> int:
    total = 0
    for suf in ("", "-wal", "-shm"):
        p = db + suf
        if os.path.exists(p):
            total += os.path.getsize(p)
    return total


def check_storage(db: str, storage_mb: int):
    if not os.path.exists(db):
        return _ok("storage", f"db {db} not present yet (fresh start)", "WARN")
    size = _db_bytes(db)
    limit = storage_mb * 1024 * 1024
    pct = size / limit * 100
    status = "PASS" if pct < 80 else ("WARN" if pct < 95 else "FAIL")
    detail = f"{size/1e6:.0f} MB of {storage_mb} MB budget ({pct:.0f}%)"
    # growth headroom: bytes/tick measured on this feed (~86 B)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        con.close()
        if n:
            bpt = size / n
            detail += f"; ~{bpt:.0f} B/tick, {bpt*15e6/1e9:.2f} GB per 15M-tick day"
    except sqlite3.Error:
        pass
    return _ok("storage", detail, status)


def check_ram(ram_mb: int):
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
            else:
                return _ok("ram", "VmRSS unavailable", "WARN")
    except OSError:
        return _ok("ram", "no /proc (non-Linux?)", "WARN")
    status = "PASS" if rss < ram_mb * 1024 * 1024 * 0.5 else "WARN"
    return _ok("ram", f"preflight RSS {rss/1e6:.0f} MB (collector ~50 MB) "
                      f"vs {ram_mb} MB budget", status)


def check_paths(db: str, state: str):
    ok = True
    for p in (os.path.dirname(os.path.abspath(db)) or ".",
              os.path.dirname(os.path.abspath(state)) or "."):
        if not os.path.isdir(p):
            try:
                os.makedirs(p, exist_ok=True)
            except OSError as e:
                ok = _ok("paths", f"cannot create {p}: {e}", "FAIL") and ok
                continue
        if not os.access(p, os.W_OK):
            ok = _ok("paths", f"{p} not writable", "FAIL") and ok
    if ok:
        _ok("paths", f"{os.path.abspath(db)} and state dir writable")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Botkeep preflight for live_collector")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--state", default=os.environ.get("COLLECTOR_STATE",
                                                      "data/collector_state.json"))
    ap.add_argument("--storage-mb", type=int, default=1024)
    ap.add_argument("--ram-mb", type=int, default=512)
    ap.add_argument("--proxy", default=os.environ.get("QX_PROXY", ""),
                    help="test the feed handshake through this proxy")
    args = ap.parse_args()

    print(f"Botkeep preflight — db={args.db} storage={args.storage_mb}MB "
          f"ram={args.ram_mb}MB\n")
    results = [
        check_python(),
        check_imports(),
        check_feed(args.proxy),
        check_storage(args.db, args.storage_mb),
        check_ram(args.ram_mb),
        check_paths(args.db, args.state),
    ]
    failed = results.count(False)
    print(f"\n{'ALL CHECKS PASS' if not failed else f'{failed} CHECK(S) FAILED'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
