#!/usr/bin/env python3
"""Quotex OTC M1 Live Hybrid Dashboard."""

import os
import json
import time
import threading
import traceback

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from candle_store import CandleStore, MultiFeed
from scan import scan_all


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))

PERIOD = 60
SCAN_INTERVAL = 15
MIN_CANDLES = 0


class ScannerApp:

    def __init__(self):
        self.store = CandleStore()

        self.feed = None
        self.running = False
        self.starting = False
        self.connection = "DISCONNECTED"

        self.lock = threading.RLock()

        self.worker = None
        self.scan_worker = None

        self.pairs = []
        self.results = []

        self.last_scan = None
        self.last_error = ""
        self.message = "Dashboard starting..."

        self.start_time = time.time()

    def log(self, *args):
        print(
            time.strftime("%H:%M:%S"),
            *args,
            flush=True,
        )

    def database_info(self):
        try:
            series = self.store.series(
                min_candles=0,
                periods=[PERIOD],
            )

            return {
                "path": self.store.path,
                "candles": self.store.total,
                "ticks": self.store.tick_total,
                "series": len(series),
                "assets": len(self.store.assets),
            }

        except Exception as exc:
            self.last_error = str(exc)

            self.log(
                "DATABASE ERROR:",
                repr(exc),
            )

            return {
                "path": self.store.path,
                "candles": 0,
                "ticks": 0,
                "series": 0,
                "assets": 0,
            }

    def extract_rows(self, data):
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in (
                "signals",
                "results",
                "rows",
                "data",
            ):
                value = data.get(key)

                if isinstance(value, list):
                    return value

        return []

    def normalize_rows(self, rows):
        output = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            item = dict(row)

            action = str(
                item.get(
                    "action",
                    item.get(
                        "signal",
                        item.get("direction", "NEUTRAL"),
                    ),
                )
            ).upper()

            if action in ("BUY", "UP"):
                action = "CALL"

            elif action in ("SELL", "DOWN"):
                action = "PUT"

            elif action not in ("CALL", "PUT"):
                action = "NEUTRAL"

            item["action"] = action

            output.append(item)

        return output

    def scan_once(self):
        try:
            db = self.database_info()

            self.log(
                "SCAN START:",
                "candles =", db["candles"],
                "series =", db["series"],
            )

            data = scan_all(
                self.store,
                [PERIOD],
                engine="hybrid",
                top=200,
                include_neutral=True,
                min_candles=MIN_CANDLES,
                skip_dead=False,
            )

            rows = self.normalize_rows(
                self.extract_rows(data)
            )

            with self.lock:
                self.results = rows

                self.last_scan = time.strftime(
                    "%H:%M:%S",
                    time.localtime(),
                )

                self.message = (
                    f"5-engine hybrid scanning: "
                    f"{len(rows)} results | "
                    f"Database series: {db['series']}"
                )

            self.log(
                "SCAN COMPLETE:",
                len(rows),
                "results",
            )

        except Exception as exc:
            self.last_error = str(exc)

            self.log(
                "SCAN ERROR:",
                repr(exc),
            )

            traceback.print_exc()

    def scan_loop(self):
        while self.running:
            self.scan_once()

            for _ in range(SCAN_INTERVAL):
                if not self.running:
                    break

                time.sleep(1)

    def start(self):
        with self.lock:
            if self.running or self.starting:
                return

            self.starting = True
            self.connection = "CONNECTING"
            self.message = "Connecting MarketQX..."

        try:
            self.log("CONNECTING MARKETQX...")

            feed = MultiFeed(
                assets=None,
                periods=[PERIOD],
                store=self.store,
                wait_instruments=75,
            )

            self.feed = feed
            self.pairs = list(feed.assets)

            self.running = True
            self.starting = False
            self.connection = "CONNECTED"

            self.log(
                "MARKETQX CONNECTED:",
                len(self.pairs),
                "pairs",
            )

            self.scan_worker = threading.Thread(
                target=self.scan_loop,
                daemon=True,
                name="scanner-loop",
            )

            self.scan_worker.start()

            self.message = (
                "Live feed connected. Scanner running."
            )

        except Exception as exc:
            self.running = False
            self.starting = False
            self.connection = "ERROR"
            self.last_error = str(exc)
            self.message = str(exc)

            self.log(
                "START ERROR:",
                repr(exc),
            )

            traceback.print_exc()

    def stop(self):
        with self.lock:
            self.running = False
            self.starting = False
            self.connection = "STOPPED"
            self.message = "Scanner stopped"

        if self.feed:
            try:
                self.feed.close()
            except Exception as exc:
                self.log(
                    "FEED CLOSE ERROR:",
                    repr(exc),
                )

            self.feed = None

        self.log("SCANNER STOPPED")

    def sensor_stats(self):
        total = 0
        ready = 0
        up = 0
        down = 0

        for row in self.results:
            engines = row.get("engines", {})

            if not isinstance(engines, dict):
                continue

            sensor = engines.get(
                "ai_sensor",
                engines.get("AI_SENSOR", {}),
            )

            if not isinstance(sensor, dict):
                continue

            action = str(
                sensor.get(
                    "action",
                    sensor.get("signal", "WAIT"),
                )
            ).upper()

            total += 1

            if action in (
                "CALL", "PUT", "UP", "DOWN", "BUY", "SELL"
            ):
                ready += 1

                if action in ("CALL", "UP", "BUY"):
                    up += 1
                else:
                    down += 1

        return {
            "total": total,
            "ready": ready,
            "up": up,
            "down": down,
            "status": "READY" if ready else "WAITING",
        }

    def state(self):
        db = self.database_info()

        with self.lock:
            sensor = self.sensor_stats()

            feed_status = {}

            if self.feed:
                try:
                    feed_status = self.feed.status()
                except Exception:
                    pass

            return {
                "running": self.running,
                "connection": self.connection,
                "message": self.message,

                "pairs": self.pairs,
                "pair_count": len(self.pairs),

                "results": self.results,
                "result_count": len(self.results),

                "last_scan": self.last_scan,
                "server_time": time.time(),

                "database_path": db["path"],
                "database_candles": db["candles"],
                "database_ticks": db["ticks"],
                "database_series": db["series"],
                "database_assets": db["assets"],

                "last_error": self.last_error,

                "ai_sensor_ready": sensor["ready"],
                "ai_sensor_total": sensor["total"],
                "ai_sensor_up": sensor["up"],
                "ai_sensor_down": sensor["down"],
                "ai_sensor_status": sensor["status"],

                "feed_status": feed_status,
            }

    def analyze_pair(self, pair):
        pair = str(pair).strip()

        if not pair:
            return {"error": "Pair required"}

        candles = self.store.get_candles(
            pair,
            PERIOD,
            200,
        )

        if not candles:
            return {
                "pair": pair,
                "action": "NEUTRAL",
                "message": "No closed candles available",
                "candles": 0,
            }

        try:
            data = scan_all(
                self.store,
                [PERIOD],
                engine="hybrid",
                top=200,
                include_neutral=True,
                min_candles=MIN_CANDLES,
                skip_dead=False,
            )

            rows = self.normalize_rows(
                self.extract_rows(data)
            )

            for row in rows:
                symbol = str(
                    row.get(
                        "asset",
                        row.get(
                            "pair",
                            row.get("symbol", ""),
                        ),
                    )
                )

                if symbol == pair:
                    return row

            return {
                "pair": pair,
                "action": "NEUTRAL",
                "candles": len(candles),
                "message": "No signal returned",
            }

        except Exception as exc:
            return {
                "pair": pair,
                "action": "ERROR",
                "error": str(exc),
            }


app = ScannerApp()


PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quotex OTC AI Scanner</title>

<style>
* { box-sizing:border-box; }

body {
    margin:0;
    background:#0b1020;
    color:#edf2ff;
    font-family:Arial,sans-serif;
}

header {
    padding:18px;
    background:#121a30;
    border-bottom:1px solid #293958;
}

h1 { font-size:21px; margin:0; }

main {
    max-width:1200px;
    margin:auto;
    padding:14px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(2,1fr);
    gap:10px;
}

.card {
    background:#151f36;
    border:1px solid #293958;
    border-radius:12px;
    padding:14px;
    margin-bottom:12px;
}

.label {
    font-size:12px;
    color:#9cabc9;
}

.value {
    font-size:22px;
    font-weight:bold;
    margin-top:7px;
}

.green { color:#38df9b; }
.red { color:#ff657d; }
.yellow { color:#ffd166; }

button {
    background:#263957;
    color:white;
    border:1px solid #40577e;
    padding:11px 15px;
    border-radius:8px;
    margin:4px;
}

input {
    width:100%;
    background:#0b1020;
    color:white;
    border:1px solid #40577e;
    padding:12px;
    border-radius:8px;
}

table {
    width:100%;
    border-collapse:collapse;
    font-size:12px;
}

th,td {
    padding:10px 6px;
    border-bottom:1px solid #293958;
    text-align:left;
}

th { color:#9cabc9; }

#pairs {
    max-height:180px;
    overflow-y:auto;
    overflow-wrap:anywhere;
    line-height:1.8;
    font-size:12px;
}

pre {
    white-space:pre-wrap;
    overflow-wrap:anywhere;
    font-size:12px;
    color:#b9c9e8;
}

@media(max-width:500px) {
    .grid { grid-template-columns:1fr 1fr; }
    .value { font-size:19px; }
}
</style>
</head>

<body>

<header>
    <h1>⚡ QUOTEX OTC AI SCANNER</h1>
    <div class="label">LIVE M1 • 5 ENGINE HYBRID</div>
</header>

<main>

<div class="card">
    <div class="label">CONNECTION</div>
    <div class="value" id="status">CONNECTING</div>
    <div class="label" id="message">Starting...</div>

    <button onclick="startScanner()">START</button>
    <button onclick="stopScanner()">STOP</button>
    <button onclick="refresh()">REFRESH</button>
</div>

<div class="grid">
    <div class="card">
        <div class="label">OTC PAIRS</div>
        <div class="value" id="pairCount">0</div>
    </div>

    <div class="card">
        <div class="label">DATABASE SERIES</div>
        <div class="value" id="seriesCount">0</div>
    </div>

    <div class="card">
        <div class="label">SAVED CANDLES</div>
        <div class="value" id="candleCount">0</div>
    </div>

    <div class="card">
        <div class="label">SAVED TICKS</div>
        <div class="value" id="tickCount">0</div>
    </div>

    <div class="card">
        <div class="label">SIGNAL RESULTS</div>
        <div class="value" id="resultCount">0</div>
    </div>

    <div class="card">
        <div class="label">LAST SCAN</div>
        <div class="value" id="lastScan">--</div>
    </div>
</div>

<div class="card">
    <div class="label">AI SENSOR STATUS</div>
    <div class="value yellow" id="aiStatus">WAITING</div>
    <div class="label" id="aiDetails">UP: 0 | DOWN: 0</div>
</div>

<div class="card">
    <h3>OTC PAIRS</h3>
    <div id="pairs">Loading...</div>
</div>

<div class="card">
    <h3>HYBRID SIGNALS</h3>

    <table>
        <thead>
            <tr>
                <th>PAIR</th>
                <th>SIGNAL</th>
                <th>CONFIDENCE</th>
                <th>VIEW</th>
            </tr>
        </thead>
        <tbody id="results">
            <tr><td colspan="4">Waiting...</td></tr>
        </tbody>
    </table>
</div>

<div class="card">
    <h3>PAIR ANALYSIS</h3>
    <input id="pairInput" placeholder="Example: EURUSD_otc">
    <button onclick="analyzePair()">ANALYZE</button>
    <pre id="analysis">Enter a pair.</pre>
</div>

<div class="card">
    <h3>DATABASE DIAGNOSTICS</h3>
    <pre id="diagnostics">Loading...</pre>
</div>

</main>

<script>
async function api(url) {
    const response = await fetch(url, {cache:"no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    return await response.json();
}

function safe(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&":"&amp;",
        "<":"&lt;",
        ">":"&gt;",
        '"':"&quot;",
        "'":"&#039;"
    })[c]);
}

function signalClass(s) {
    if (s === "CALL" || s === "UP") return "green";
    if (s === "PUT" || s === "DOWN") return "red";
    return "yellow";
}

function pairOf(row) {
    return row.asset || row.pair || row.symbol || "";
}

function renderResults(rows) {
    const body = document.getElementById("results");

    if (!rows || rows.length === 0) {
        body.innerHTML =
            '<tr><td colspan="4">No signals yet. Waiting for candle data...</td></tr>';
        return;
    }

    body.innerHTML = rows.map(row => {
        const pair = pairOf(row);
        const signal = String(row.action || "NEUTRAL").toUpperCase();
        const confidence = row.confidence ?? row.score ?? "--";

        return `
        <tr>
            <td>${safe(pair)}</td>
            <td class="${signalClass(signal)}">${safe(signal)}</td>
            <td>${safe(confidence)}</td>
            <td>
                <button onclick="quickAnalyze('${safe(pair)}')">VIEW</button>
            </td>
        </tr>`;
    }).join("");
}

async function refresh() {
    try {
        const d = await api("/api/state");

        document.getElementById("status").textContent =
            d.connection || "UNKNOWN";

        document.getElementById("message").textContent =
            d.message || "";

        document.getElementById("pairCount").textContent =
            d.pair_count ?? 0;

        document.getElementById("seriesCount").textContent =
            d.database_series ?? 0;

        document.getElementById("candleCount").textContent =
            d.database_candles ?? 0;

        document.getElementById("tickCount").textContent =
            d.database_ticks ?? 0;

        document.getElementById("resultCount").textContent =
            d.result_count ?? 0;

        document.getElementById("lastScan").textContent =
            d.last_scan || "--";

        document.getElementById("aiStatus").textContent =
            d.ai_sensor_status || "WAITING";

        document.getElementById("aiDetails").textContent =
            "UP: " + (d.ai_sensor_up || 0) +
            " | DOWN: " + (d.ai_sensor_down || 0) +
            " | READY: " + (d.ai_sensor_ready || 0);

        document.getElementById("pairs").textContent =
            (d.pairs || []).join(" • ");

        renderResults(d.results || []);

        document.getElementById("diagnostics").textContent =
            "Database: " + (d.database_path || "--") +
            "\nCandles: " + (d.database_candles ?? 0) +
            "\nTicks: " + (d.database_ticks ?? 0) +
            "\nSeries: " + (d.database_series ?? 0) +
            "\nAssets: " + (d.database_assets ?? 0) +
            "\nFeed: " + JSON.stringify(d.feed_status || {}, null, 2) +
            "\nLast error: " + (d.last_error || "None");

    } catch(e) {
        document.getElementById("status").textContent = "API ERROR";
        document.getElementById("message").textContent = e.message;
    }
}

async function startScanner() {
    try {
        await api("/api/start");
        setTimeout(refresh, 1000);
    } catch(e) {
        alert(e.message);
    }
}

async function stopScanner() {
    try {
        await api("/api/stop");
        await refresh();
    } catch(e) {
        alert(e.message);
    }
}

async function quickAnalyze(pair) {
    document.getElementById("pairInput").value = pair;
    await analyzePair();
}

async function analyzePair() {
    const pair = document.getElementById("pairInput").value.trim();

    if (!pair) {
        alert("Enter pair name");
        return;
    }

    document.getElementById("analysis").textContent = "Analyzing...";

    try {
        const d = await api(
            "/api/analyze?pair=" + encodeURIComponent(pair)
        );

        document.getElementById("analysis").textContent =
            JSON.stringify(d, null, 2);

    } catch(e) {
        document.getElementById("analysis").textContent = e.message;
    }
}

refresh();
setInterval(refresh, 3000);
</script>

</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args, flush=True)

    def send_json(self, data, status=200):
        body = json.dumps(
            data,
            default=str,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")

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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.send_html(PAGE)
            return

        if path in ("/api/state", "/apl/state"):
            self.send_json(app.state())
            return

        if path == "/api/start":
            threading.Thread(
                target=app.start,
                daemon=True,
            ).start()

            self.send_json({
                "ok": True,
                "message": "Start requested",
            })
            return

        if path == "/api/stop":
            app.stop()

            self.send_json({
                "ok": True,
                "message": "Scanner stopped",
            })
            return

        if path == "/api/analyze":
            params = parse_qs(parsed.query)
            pair = params.get("pair", [""])[0]

            self.send_json(
                app.analyze_pair(pair)
            )
            return

        if path == "/health":
            self.send_json({
                "ok": True,
                "time": time.time(),
            })
            return

        self.send_json(
            {"error": "Not found"},
            404,
        )


def main():
    print(
        "QUOTEX OTC DASHBOARD STARTING",
        flush=True,
    )

    # Automatically start MarketQX feed and scanner.
    threading.Thread(
        target=app.start,
        daemon=True,
        name="marketqx-startup",
    ).start()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        DashboardHandler,
    )

    server.daemon_threads = True

    print(
        "DASHBOARD LISTENING:",
        PORT,
        flush=True,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        app.stop()
        server.server_close()
        app.store.close()


if __name__ == "__main__":
    main()