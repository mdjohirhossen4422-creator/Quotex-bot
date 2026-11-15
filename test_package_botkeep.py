#!/usr/bin/env python3
"""Tests for the Botkeep deployment package (package_botkeep.py) and preflight.

The point is safety: the archive must be self-contained (importable), must NOT
contain any secret or database file, and the preflight must treat a WARN as
non-fatal (a fresh deploy has no DB yet — that is not a failure).

  python test_package_botkeep.py
"""
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import package_botkeep as pk


def test_secret_scanner_flags_assignments_not_mentions():
    # a bare env-var mention is fine; an assigned value is not
    assert not pk.SECRET_RE.search("set TELEGRAM_BOT_TOKEN in the panel")
    assert pk.SECRET_RE.search("TELEGRAM_BOT_TOKEN=123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert pk.SECRET_RE.search('{"bot_token": "abcdefghijklmnop"}')
    assert pk.SECRET_RE.search("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    print("PASS test_secret_scanner_flags_assignments_not_mentions")


def test_forbidden_files_are_rejected():
    for bad in (".env", "telegram.json", "candles.db", "x.sqlite", "k.pem",
                "id.key", "bundle.zip"):
        base = os.path.basename(bad)
        assert (base in pk.FORBIDDEN_NAMES
                or os.path.splitext(base)[1] in pk.FORBIDDEN_EXT), bad
    print("PASS test_forbidden_files_are_rejected")


def test_build_is_self_contained_and_clean():
    out = os.path.join(tempfile.mkdtemp(), "bundle.zip")
    pk.build(out)
    names = set(zipfile.ZipFile(out).namelist())
    # required entry points present at the archive root
    for must in ("requirements.txt", "live_collector.py", "README.md",
                 "botkeep_preflight.py", "storage_monitor.py", "start.sh"):
        assert must in names, f"missing {must}: {sorted(names)}"
    # no nested folder, no secrets, no db
    assert not any("/" in n for n in names), names
    for n in names:
        assert not n.endswith((".db", ".sqlite", ".zip", ".env")), n
    assert "telegram.json" not in names and "payout.json" not in names
    print("PASS test_build_is_self_contained_and_clean")


def test_bundled_modules_import_from_archive():
    import subprocess
    out = os.path.join(tempfile.mkdtemp(), "bundle.zip")
    pk.build(out)
    d = tempfile.mkdtemp()
    zipfile.ZipFile(out).extractall(d)
    code = ("import sys; sys.path.insert(0,'.'); "
            "import live_collector, market_qx_candles, candle_store, "
            "provenance, tick_summary, botkeep_preflight, storage_monitor; "
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=d,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout, r.stdout
    print("PASS test_bundled_modules_import_from_archive")


def test_preflight_treats_warn_as_non_fatal():
    import botkeep_preflight as bp
    assert bp._ok("x", "y", "PASS") is True
    assert bp._ok("x", "y", "WARN") is True     # fresh-DB warning is not a failure
    assert bp._ok("x", "y", "FAIL") is False
    print("PASS test_preflight_treats_warn_as_non_fatal")


def test_trader_shim_runs_collector_status():
    """The Botkeep egg runs `python trader.py`; the shim must forward to the
    collector. `--status` is a no-feed path, so it is safe to run here."""
    import subprocess
    out = os.path.join(tempfile.mkdtemp(), "bundle.zip")
    pk.build(out)
    d = tempfile.mkdtemp()
    zipfile.ZipFile(out).extractall(d)
    assert os.path.exists(os.path.join(d, "trader.py")), "trader.py missing from ZIP"
    r = subprocess.run([sys.executable, "trader.py", "--status"],
                       cwd=d, capture_output=True, text=True, timeout=60,
                       env={**os.environ,
                            "CANDLE_DB": os.path.join(d, "data", "candles.db"),
                            "COLLECTOR_STATE": os.path.join(d, "data", "state.json")})
    assert r.returncode == 0, r.stderr
    assert "no heartbeat yet" in r.stdout or "status" in r.stdout, r.stdout
    print("PASS test_trader_shim_runs_collector_status")


if __name__ == "__main__":
    test_secret_scanner_flags_assignments_not_mentions()
    test_forbidden_files_are_rejected()
    test_build_is_self_contained_and_clean()
    test_bundled_modules_import_from_archive()
    test_preflight_treats_warn_as_non_fatal()
    test_trader_shim_runs_collector_status()
    print("\nall package-botkeep tests passed")
