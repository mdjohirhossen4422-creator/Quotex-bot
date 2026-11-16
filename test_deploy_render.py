#!/usr/bin/env python3
"""Tests for the Render collector deploy files (deploy/render/).

No network and no Docker build: these check the deploy *configuration* is
consistent, so a broken blueprint is caught before it reaches Render.

  python test_deploy_render.py
"""
from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.join(ROOT, "deploy", "render")


def test_blueprint_is_a_worker_with_disk_and_us_region():
    spec = yaml.safe_load(open(os.path.join(DEPLOY, "render.yaml")))
    svc = spec["services"][0]
    assert svc["type"] == "worker", "collector must be a background worker"
    assert svc["runtime"] == "docker"
    assert svc["region"] != "frankfurt", "EU region is geo-blocked for this feed"
    assert svc["disk"]["mountPath"] == "/data", "durable data must live on /data"
    # CANDLE_DB must point at the disk, else history is lost on redeploy.
    env = {e["key"]: e.get("value") for e in svc["envVars"]}
    assert env["CANDLE_DB"].startswith("/data/")
    assert env["COLLECTOR_STORE_TICKS"] == "0", "default must be candle-only"
    print("PASS test_blueprint_is_a_worker_with_disk_and_us_region")


def test_dockerfile_copies_only_collector_modules():
    text = open(os.path.join(DEPLOY, "Dockerfile.collector")).read()
    # Every module the collector imports must be COPYed in.
    for mod in ("live_collector.py", "market_qx_candles.py", "candle_store.py",
                "provenance.py", "tick_summary.py", "storage_monitor.py"):
        assert mod in text, f"{mod} not copied into the collector image"
    # And the heavy ML stack must NOT be installed (it is not needed). Only
    # inspect real instructions, not comments.
    code_lines = [ln for ln in text.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    for heavy in ("numpy", "scikit-learn", "pyarrow"):
        assert heavy not in code, f"{heavy} must not be in the collector image"
    # The copied modules must actually exist at the repo root.
    for mod in ("live_collector.py", "market_qx_candles.py", "candle_store.py",
                "provenance.py", "tick_summary.py", "storage_monitor.py"):
        assert os.path.exists(os.path.join(ROOT, mod)), f"{mod} missing at root"
    print("PASS test_dockerfile_copies_only_collector_modules")


def test_blueprint_context_matches_dockerfile_copy_paths():
    """dockerContext=. means COPY paths are repo-root relative; the blueprint's
    dockerfilePath must resolve inside the repo too."""
    spec = yaml.safe_load(open(os.path.join(DEPLOY, "render.yaml")))
    svc = spec["services"][0]
    assert svc["dockerContext"] == "."
    df = os.path.join(ROOT, svc["dockerfilePath"])
    assert os.path.exists(df), f"dockerfilePath does not resolve: {df}"
    print("PASS test_blueprint_context_matches_dockerfile_copy_paths")


if __name__ == "__main__":
    test_blueprint_is_a_worker_with_disk_and_us_region()
    test_dockerfile_copies_only_collector_modules()
    test_blueprint_context_matches_dockerfile_copy_paths()
    print("\nall render-deploy tests passed")
