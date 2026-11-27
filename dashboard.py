"""Quotex OTC M1 Live Scanner Dashboard."""

import os
import json
import time
import threading
import argparse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from candle_store import CandleStore, MultiFeed
from scan import scan_all


PORT = int(os.environ.get("PORT", "10000"))
HOST = "0.0.0.0"
DB_PATH = os.environ.get("CANDLE_DB", "data/candles.db")

TIMEFRAME = 60
SCAN_INTERVAL = 15


PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quotex OTC M1 Scanner</title>
<style>
body {
    background:#101522;
    color:white;
    font-family:Arial;
    margin:0;