#!/usr/bin/env python3
"""Build the Botkeep collector deployment ZIP.

Produces a self-contained archive with the collector at the ROOT (so Botkeep's
runtime detection finds `requirements.txt` immediately), plus a manual-deploy
README. It refuses to include anything secret or any production database.

  python package_botkeep.py                 # -> dist/botkeep-collector-<date>.zip
  python package_botkeep.py --out dist/x.zip
  python package_botkeep.py --check         # build, then print + verify SHA256

The archive is NOT uploaded anywhere. Deployment is manual (Botkeep panel) and
requires the operator's explicit action — see deploy/botkeep/DEPLOY_ZIP_README.md.
"""
import argparse
import hashlib
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")

# Files that MUST be in the archive, with the path they take inside the ZIP.
# Flat at the root so Botkeep sees requirements.txt without an extra folder.
BUNDLE = [
    "live_collector.py",
    "market_qx_candles.py",
    "candle_store.py",
    "provenance.py",
    "tick_summary.py",
    "botkeep_preflight.py",
    "storage_monitor.py",
    "requirements-collector.txt",
    "deploy/botkeep/start.sh",
]

# Extra files copied in with a fixed destination name.
EXTRA = {
    "deploy/botkeep/README.md": "BOTKEEP_README.md",
    "deploy/botkeep/DEPLOY_ZIP_README.md": "README.md",
    "deploy/botkeep/requirements.txt": "requirements.txt",
}

# Anything matching these must never appear in the archive.
FORBIDDEN_NAMES = {".env", "telegram.json", "payout.json", ".gitignore"}
FORBIDDEN_EXT = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".zip"}
# Flag a secret *assignment*, not a bare env-var name. A Telegram bot token is
# also caught by its own shape (123456789:AA...), independent of the key name.
SECRET_RE = re.compile(
    r"(?:bot[_-]?token|telegram[_-]?(?:bot_)?token|api[_-]?key|secret[_-]?key|"
    r"password)"
    r"['\"]?\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    r"|BEGIN [A-Z ]*PRIVATE KEY"
    r"|\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
    re.IGNORECASE)


def _fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


def _collect():
    """Return [(src_abs, dest_in_zip)] or exit if anything is missing/forbidden."""
    items = []
    for rel in BUNDLE:
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            _fail(f"required file missing: {rel}")
        items.append((src, os.path.basename(rel)))
    for rel, dest in EXTRA.items():
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            _fail(f"required file missing: {rel}")
        items.append((src, dest))

    # Safety: never ship a secret-bearing or database file, however it got listed.
    for src, dest in items:
        base = os.path.basename(dest)
        if base in FORBIDDEN_NAMES or os.path.splitext(base)[1] in FORBIDDEN_EXT:
            _fail(f"refusing to include forbidden file: {dest}")
    return items


def _scan_text(path: str) -> list[str]:
    """Best-effort secret scan of a text file. Returns hit descriptions."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    hits = []
    for m in SECRET_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        hits.append(f"{os.path.basename(path)}:{line}: {m.group(0)[:40]!r}")
    return hits


def build(out_path: str) -> str:
    items = _collect()

    # Secret scan the whole bundle before writing a single byte.
    all_hits = []
    for src, _ in items:
        all_hits += _scan_text(src)
    if all_hits:
        _fail("possible secrets found — aborting:\n  " + "\n  ".join(all_hits))

    os.makedirs(os.path.dirname(out_path) or DIST, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, dest in sorted(items, key=lambda x: x[1]):
            z.write(src, dest)
    return out_path


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def list_zip(path: str):
    with zipfile.ZipFile(path) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            print(f"  {info.file_size:>8}  {info.filename}")


def main():
    ap = argparse.ArgumentParser(description="Build the Botkeep collector ZIP")
    ap.add_argument("--out", default="",
                    help="output path (default dist/botkeep-collector-<date>.zip)")
    ap.add_argument("--check", action="store_true",
                    help="build, list contents, print and verify the SHA256")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = args.out or os.path.join(DIST, f"botkeep-collector-{stamp}.zip")

    path = build(out)
    digest = sha256(path)
    size = os.path.getsize(path)

    print(f"built: {path}")
    print(f"size:  {size:,} bytes")
    print(f"sha256: {digest}")
    if args.check:
        print("\ncontents:")
        list_zip(path)
        # re-hash to confirm the written file is stable
        assert sha256(path) == digest, "hash changed between reads"
        print("\nsha256 verified (stable on re-read).")
        print("NOT deployed — upload manually per deploy/botkeep/DEPLOY_ZIP_README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
