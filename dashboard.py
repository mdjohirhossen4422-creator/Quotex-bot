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
    padding:15px;
}
.card {
    background:#1c2638;
    padding:15px;
    border-radius:12px;
    margin-bottom:12px;
}
h2 {color:#36d6a0}
button,select {
    padding:13px;
    border:0;
    border-radius:8px;
    font-size:15px;
    margin:5px;
}
button {
    background:#16a879;
    color:white;
}
.stop {background:#d84949}
.signal {
    font-size:30px;
    font-weight:bold;
    text-align:center;
    padding:20px;
}
.call {color:#24df9b}
.put {color:#ff6565}
.neutral {color:#ffc857}
pre {
    white-space:pre-wrap;
    overflow-wrap:anywhere;
}
</style>
</head>
<body>

<h2>QUOTEX OTC M1 SCANNER</h2>

<div class="card">
    <div>Connection: <b id="connection">STOPPED</b></div>
    <div>OTC Pairs: <b id="pairs">0</b></div>
    <div>Timeframe: <b>1 Minute</b></div>
    <div>Scan Interval: <b>15 Seconds</b></div>
    <div>Status: <b id="status">Waiting</b></div>
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
async function api(path) {
    const r = await fetch(path);
    return await r.json();
}

async function refresh() {
    try {
        const s = await api('/api/state');

        document.getElementById('connection').textContent =
            s.status === 'RUNNING' ? 'CONNECTED' :
            s.status === 'CONNECTING' ? 'CONNECTING' :
            s.status === 'ERROR' ? 'ERROR' : 'STOPPED';

        document.getElementById('pairs').textContent =
            s.pairs.length;

        document.getElementById('status').textContent =
            s.message || s.status;

        const select = document.getElementById('pair');
        const old = select.value;

        if (select.options.length !== s.pairs.length) {
            select.innerHTML = '';

            s.pairs.forEach(p => {
                const o = document.createElement('option');
                o.value = p;
                o.textContent = p;
                select.appendChild(o);
            });

            if (s.pairs.includes(old)) {
                select.value = old;
            }
        }

        if (s.last_scan) {
            document.getElementById('results').textContent =
                JSON.stringify(s.last_scan, null, 2);
        }

    } catch(e) {
        document.getElementById('connection').textContent = 'ERROR';
    }
}

async function start() {
    const r = await api('/api/start');

    if (!r.ok) {
        document.getElementById('status').textContent =
            r.error || 'Start failed';
    }

    refresh();
}

async function stop() {
    await api('/api/stop');
    refresh();
}

async function analyze() {
    const p = document.getElementById('pair').value;

    if (!p) {
        document.getElementById('details').textContent =
            'No pair available';
        return;
    }

    const r = await api(
        '/api/analyze?pair=' + encodeURIComponent(p)
    );

    showSignal(r);
}

function showSignal(r) {
    const el = document.getElementById('signal');
    const v = String(r.signal || 'NEUTRAL').toUpperCase();

    el.textContent = v;
    el.className = 'signal ' +
        (v === 'CALL' ? 'call' :
         v === 'PUT' ? 'put' : 'neutral');

    document.getElementById('details').textContent =
        r.reason || r.message || 'Analysis received';
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class ScannerHub:

    def __init__(self, store):
        self.store = store
        self.assets = []
        self.feed = None

        self.running = False
        self.status = "STOPPED"
        self.message = "Waiting"

        self.last_scan = None
        self.last_error = None

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.scan_worker = None

    def start(self):
        with self.lock:
            if self.status in ("CONNECTING", "RUNNING"):
                return {"ok": True, "message": self.message}

            self.stop_event = threading.Event()
            self.running = True
            self.status = "CONNECTING"
            self.message = "Connecting to MarketQX..."
            self.last_error = None

            self.worker = threading.Thread(
                target=self._feed_worker,
                daemon=True,
            )
            self.worker.start()

        return {"ok": True, "message": "Connecting"}

    def _feed_worker(self):
        try:
            print("Connecting to MarketQX...", flush=True)

            # MultiFeed discovers OTC instruments automatically.
            # Its constructor connects and subscribes to the feed.
            feed = MultiFeed(
                assets=None,
                periods=[TIMEFRAME],
                store=self.store,
                wait_instruments=75,
            )

            with self.lock:
                if self.stop_event.is_set():
                    feed.close()
                    return

                self.feed = feed
                self.assets = list(feed.assets)
                self.status = "RUNNING"
                self.message = "Live feed connected"

            print(
                "LIVE FEED CONNECTED:",
                len(self.assets),
                "OTC pairs",
                flush=True,
            )

            self.scan_worker = threading.Thread(
                target=self._scan_loop,
                daemon=True,
            )
            self.scan_worker.start()

            # Keep the worker alive while the feed is active.
            while not self.stop_event.wait(1):
                if not feed.qx.connected:
                    raise RuntimeError("MarketQX WebSocket disconnected")

        except Exception as exc:
            print("FEED ERROR:", repr(exc), flush=True)

            with self.lock:
                self.last_error = str(exc)
                self.status = "ERROR"
                self.message = str(exc)
                self.running = False

        finally:
            with self.lock:
                feed = self.feed
                self.feed = None

                if self.status != "ERROR":
                    self.status = "STOPPED"
                    self.message = "Scanner stopped"

                self.running = False

            if feed:
                feed.close()

    def _scan_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.status != "RUNNING":
                    break

                result = scan_all(
                    self.store,
                    [TIMEFRAME],
                    "rule",
                    top=200,
                )

                with self.lock:
                    self.last_scan = result

                print("AUTO SCAN COMPLETED", flush=True)

            except Exception as exc:
                print("SCAN ERROR:", repr(exc), flush=True)

                with self.lock:
                    self.last_error = str(exc)
                    self.message = "Scan error: " + str(exc)

            self.stop_event.wait(SCAN_INTERVAL)

    def stop(self):
        self.stop_event.set()

        with self.lock:
            feed = self.feed
            self.running = False
            self.status = "STOPPED"
            self.message = "Scanner stopped"

        if feed:
            feed.close()

        print("SCANNER STOP REQUESTED", flush=True)

        return {"ok": True}

    def state(self):
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "message": self.message,
                "pairs": list(self.assets),
                "last_scan": self.last_scan,
                "error": self.last_error,
            }

    def scan(self):
        try:
            result = scan_all(
                self.store,
                [TIMEFRAME],
                "rule",
                top=200,
            )

            with self.lock:
                self.last_scan = result

            return result

        except Exception as exc:
            return {"error": str(exc)}

    def analyze(self, pair):
        try:
            result = self.scan()

            if isinstance(result, dict):
                rows = result.get(
                    "results",
                    result.get("signals", []),
                )
            else:
                rows = result

            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue

                    symbol = row.get(
                        "asset",
                        row.get("symbol", row.get("pair")),
                    )

                    if symbol == pair:
                        return row

            return {
                "signal": "NEUTRAL",
                "reason": "No matching analysis result yet",
            }

        except Exception as exc:
            return {
                "signal": "NEUTRAL",
                "reason": str(exc),
            }


def make_handler(hub):

    class Handler(BaseHTTPRequestHandler):

        def send_json(self, data):
            body = json.dumps(
                data,
                default=str,
            ).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json",
            )
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/":
                body = PAGE.encode("utf-8")

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )
                self.send_header(
                    "Content-Length",
                    str(len(body)),
                )
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/state":
                return self.send_json(hub.state())

            if route == "/api/start":
                return self.send_json(hub.start())

            if route == "/api/stop":
                return self.send_json(hub.stop())

            if route == "/api/scan":
                return self.send_json(hub.scan())

            if route == "/api/analyze":
                pair = parse_qs(
                    parsed.query
                ).get("pair", [""])[0]

                return self.send_json(
                    hub.analyze(pair)
                )

            return self.send_json({
                "error": "Not found"
            })

        def log_message(self, fmt, *args):
            print("HTTP:", fmt % args, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--db", default=DB_PATH)

    args = parser.parse_args()

    os.makedirs(
        os.path.dirname(args.db) or ".",
        exist_ok=True,
    )

    store = CandleStore(args.db)
    hub = ScannerHub(store)

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(hub),
    )

    print("QUOTEX OTC M1 SCANNER STARTED", flush=True)
    print("Port:", args.port, flush=True)
    print("Waiting for START button...", flush=True)

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        hub.stop()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main(