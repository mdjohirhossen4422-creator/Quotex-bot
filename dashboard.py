#!/usr/bin/env python3
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
SCAN_INTERVAL = 15
TIMEFRAME = 60


PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quotex OTC Live Scanner</title>
<style>
body {
    background:#101522;color:#fff;
    font-family:Arial;margin:0;padding:15px;
}
.card {
    background:#1c2638;padding:15px;
    border-radius:12px;margin-bottom:12px;
}
h2 {color:#36d6a0}
button,select {
    padding:13px;border:0;border-radius:8px;
    font-size:16px;margin:5px;
}
button {background:#16a879;color:white}
.stop {background:#d84949}
.signal {
    font-size:30px;font-weight:bold;
    text-align:center;padding:20px;
}
.call {color:#24df9b}
.put {color:#ff6565}
.neutral {color:#ffc857}
pre {white-space:pre-wrap;overflow-wrap:anywhere}
</style>
</head>
<body>
<h2>QUOTEX OTC M1 SCANNER</h2>

<div class="card">
    <div>Connection: <b id="connection">Checking...</b></div>
    <div>OTC Pairs: <b id="pairs">0</b></div>
    <div>Timeframe: <b>1 Minute</b></div>
    <div>Scan Interval: <b>15 Seconds</b></div>
</div>

<div class="card">
    <button onclick="start()">START SCANNER</button>
    <button class="stop" onclick="stop()">STOP SCANNER</button>
</div>

<div class="card">
    <select id="pair"></select>
    <button onclick="analyze()">ANALYZE</button>
</div>

<div class="card">
    <h3>Latest Signal</h3>
    <div id="signal" class="signal neutral">WAITING</div>
    <div id="details">Waiting for market data...</div>
</div>

<div class="card">
    <h3>Live Scan Results</h3>
    <pre id="results">Waiting...</pre>
</div>

<script>
let timer=null;

async function api(path) {
    const r=await fetch(path);
    return await r.json();
}

async function refresh() {
    try {
        const s=await api('/api/state');
        document.getElementById('connection').textContent=
            s.running ? 'RUNNING' : 'STOPPED';
        document.getElementById('pairs').textContent=s.pairs.length;

        const select=document.getElementById('pair');
        const old=select.value;
        if(select.options.length!==s.pairs.length) {
            select.innerHTML='';
            s.pairs.forEach(p=>{
                const o=document.createElement('option');
                o.value=p;o.textContent=p;select.appendChild(o);
            });
            if(s.pairs.includes(old)) select.value=old;
        }

        if(s.last_scan) {
            document.getElementById('results').textContent=
                JSON.stringify(s.last_scan,null,2);
        }
    } catch(e) {
        document.getElementById('connection').textContent='ERROR';
    }
}

async function start() {
    await api('/api/start');
    refresh();
}

async function stop() {
    await api('/api/stop');
    refresh();
}

async function analyze() {
    const p=document.getElementById('pair').value;
    if(!p) return;
    const r=await api('/api/analyze?pair='+encodeURIComponent(p));
    showSignal(r);
}

function showSignal(r) {
    const el=document.getElementById('signal');
    const v=(r.signal||'NEUTRAL').toUpperCase();
    el.textContent=v;
    el.className='signal '+(
        v==='CALL'?'call':v==='PUT'?'put':'neutral'
    );
    document.getElementById('details').textContent=
        r.reason||r.message||'No analysis details';
}

refresh();
setInterval(refresh,3000);
</script>
</body>
</html>
"""


class ScannerHub:

    def __init__(self, store, assets):
        self.store = store
        self.assets = assets
        self.feed = None
        self.running = False
        self.last_scan = None
        self.lock = threading.Lock()
        self.thread = None

    def start(self):
        if self.running:
            return

        if not self.assets:
            raise RuntimeError("No OTC instruments configured")

        self.running = True

        def feed_worker():
            try:
                self.feed = MultiFeed(
                    self.assets,
                    [TIMEFRAME],
                    self.store
                )
                self.feed.run()
            except Exception as e:
                print("FEED ERROR:", repr(e), flush=True)
                self.running = False

        self.thread = threading.Thread(
            target=feed_worker,
            daemon=True
        )
        self.thread.start()

    def stop(self):
        self.running = False
        if self.feed and hasattr(self.feed, "stop"):
            try:
                self.feed.stop()
            except Exception:
                pass

    def scan(self):
        try:
            result = scan_all(
                self.store,
                [TIMEFRAME],
                "rule",
                top=200
            )
            with self.lock:
                self.last_scan = result
            return result
        except Exception as e:
            return {"error": str(e)}

    def analyze(self, pair):
        try:
            result = self.scan()
            if isinstance(result, dict):
                rows = result.get(
                    "results",
                    result.get("signals", [])
                )
            else:
                rows = result

            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        symbol = row.get(
                            "asset",
                            row.get("symbol", row.get("pair"))
                        )
                        if symbol == pair:
                            return row

            return {
                "signal": "NEUTRAL",
                "reason": "No matching analysis result yet"
            }
        except Exception as e:
            return {"signal": "NEUTRAL", "reason": str(e)}


def discover_assets():
    """Read OTC instruments from an optional JSON environment variable."""
    raw = os.environ.get("OTC_ASSETS", "")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            return [
                x.strip() for x in raw.split(",") if x.strip()
            ]

    # Do not guess the asset_catalog() signature.
    # Configure OTC_ASSETS until the actual instruments source is wired.
    return []


def make_handler(hub):

    class Handler(BaseHTTPRequestHandler):

        def send_json(self, data):
            body = json.dumps(
                data, default=str
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path)
            route = path.path

            if route == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/state":
                return self.send_json({
                    "running": hub.running,
                    "pairs": hub.assets,
                    "last_scan": hub.last_scan
                })

            if route == "/api/start":
                try:
                    hub.start()
                    return self.send_json({"ok": True})
                except Exception as e:
                    return self.send_json({"ok": False, "error": str(e)})

            if route == "/api/stop":
                hub.stop()
                return self.send_json({"ok": True})

            if route == "/api/scan":
                return self.send_json(hub.scan())

            if route == "/api/analyze":
                pair = parse_qs(path.query).get("pair", [""])[0]
                return self.send_json(hub.analyze(pair))

            return self.send_json({"error": "Not found"})

        def log_message(self, fmt, *args):
            print("HTTP:", fmt % args, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default="")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)

    store = CandleStore(args.db)

    assets = (
        [x.strip() for x in args.assets.split(",") if x.strip()]
        if args.assets else discover_assets()
    )

    hub = ScannerHub(store, assets)

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(hub)
    )

    print("QUOTEX OTC M1 SCANNER STARTED", flush=True)
    print("Port:", args.port, flush=True)
    print("OTC pairs configured:", len(assets), flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        server.server_close()


if __name__ == "__main__":
    main()