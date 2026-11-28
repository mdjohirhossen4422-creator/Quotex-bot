#!/usr/bin/env python3
"""Quotex OTC M1 Hybrid Scanner Dashboard."""

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
        self.connection = "DISCONNECTED"

        self.lock = threading.RLock()
        self.worker = None

        self.pairs = []
        self.results = []
        self.last_scan = None
        self.last_error = ""

        self.message = "Scanner starting..."
        self.started_at = time.time()

    def _get_series(self):
        try:
            return self.store.series(
                min_candles=MIN_CANDLES,
                periods=[PERIOD],
            )
        except Exception as exc:
            self.last_error = str(exc)
            print(
                "DATABASE SERIES ERROR:",
                repr(exc),
                flush=True,
            )
            return []

    def _get_series_count(self):
        series = self._get_series()

        try:
            print(
                "DATABASE CHECK:",
                "path =", self.store.path,
                "candles =", self.store.total,
                "ticks =", self.store.tick_total,
                "series =", len(series),
                flush=True,
            )
        except Exception as exc:
            print(
                "DATABASE CHECK ERROR:",
                repr(exc),
                flush=True,
            )

        return len(series)

    def _extract_rows(self, data):
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

    def _normalise_rows(self, rows):
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

            if action in ("BUY", "CALL", "UP"):
                action = "CALL"

            elif action in ("SELL", "PUT", "DOWN"):
                action = "PUT"

            else:
                action = "NEUTRAL"

            item["action"] = action

            output.append(item)

        return output

    def _sensor_stats(self):
        total = 0
        ready = 0
        up = 0
        down = 0

        for row in self.results:
            engines = row.get("engines", {})

            if isinstance(engines, dict):
                sensor = engines.get(
                    "ai_sensor",
                    engines.get("AI_SENSOR", {}),
                )

                if isinstance(sensor, dict):
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
            "ai_sensor_total": total,
            "ai_sensor_ready": ready,
            "ai_sensor_up": up,
            "ai_sensor_down": down,
            "ai_sensor_status": (
                "READY" if ready else "WAITING"
            ),
        }

    def _scan_once(self):
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

            rows = self._normalise_rows(
                self._extract_rows(data)
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
                    f"Database series: "
                    f"{self._get_series_count()}"
                )

            print(
                "SCAN COMPLETE:",
                len(rows),
                "results",
                flush=True,
            )

        except Exception as exc:
            self.last_error = str(exc)

            print(
                "SCAN ERROR:",
                repr(exc),
                flush=True,
            )

            traceback.print_exc()

            with self.lock:
                self.message = "Scanner error: " + str(exc)

    def _worker_loop(self):
        while self.running:
            try:
                self._scan_once()
            except Exception:
                traceback.print_exc()

            for _ in range(SCAN_INTERVAL):
                if not self.running:
                    break
                time.sleep(1)

    def start(self):
        with self.lock:
            if self.running:
                return

            self.running = True
            self.connection = "CONNECTING"
            self.message = "Connecting MarketQX feed..."

        try:
            self.feed = MultiFeed(
                assets=None,
                periods=[PERIOD],
                store=self.store,
                wait_instruments=75,
            )

            self.pairs = list(self.feed.assets)

            self.connection = "CONNECTED"

            self.worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
            )

            self.worker.start()

            print(
                "SCANNER STARTED:",
                len(self.pairs),
                "pairs",
                flush=True,
            )

        except Exception as exc:
            self.running = False
            self.connection = "ERROR"
            self.last_error = str(exc)
            self.message = str(exc)

            print(
                "START ERROR:",
                repr(exc),
                flush=True,
            )

            traceback.print_exc()

    def stop(self):
        with self.lock:
            self.running = False
            self.connection = "STOPPED"
            self.message = "Scanner stopped"

        if self.feed:
            try:
                self.feed.close()
            except Exception as exc:
                print(
                    "FEED STOP ERROR:",
                    repr(exc),
                    flush=True,
                )

            self.feed = None

    def state(self):
        with self.lock:
            series_count = self._get_series_count()

            sensor = self._sensor_stats()

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

                "database_path": self.store.path,
                "database_candles": self.store.total,
                "database_ticks": self.store.tick_total,
                "database_series": series_count,

                "last_error": self.last_error,

                "ai_sensor_ready": sensor["ai_sensor_ready"],
                "ai_sensor_total": sensor["ai_sensor_total"],
                "ai_sensor_up": sensor["ai_sensor_up"],
                "ai_sensor_down": sensor["ai_sensor_down"],
                "ai_sensor_status": sensor["ai_sensor_status"],

                "feed_status": feed_status,
            }

    def analyze_pair(self, pair):
        pair = str(pair).strip()

        if not pair:
            return {
                "error": "Pair is required"
            }

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

            rows = self._normalise_rows(
                self._extract_rows(data)
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
                "message": "No signal returned by scanner",
                "candles": len(candles),
            }

        except Exception as exc:
            return {
                "pair": pair,
                "action": "ERROR",
                "error": str(exc),
            }


app = ScannerApp()


class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(
            "[HTTP]",
            fmt % args,
            flush=True,
        )

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
            "Access-Control-Allow-Origin",
            "*",
        )
        self.send_header(
            "Cache-Control",
            "no-store",
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


PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quotex OTC AI Scanner</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #0b1020;
    color: #edf2ff;
    font-family: Arial, sans-serif;
}

header {
    padding: 18px;
    background: #121a30;
    border-bottom: 1px solid #283553;
}

h1 {
    font-size: 21px;
    margin: 0;
}

main {
    max-width: 1200px;
    margin: auto;
    padding: 14px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
}

.card {
    background: #151f36;
    border: 1px solid #293958;
    border-radius: 12px;
    padding: 14px;
    margin-bottom: 12px;
}

.label {
    font-size: 12px;
    color: #9cabc9;
}

.value {
    font-size: 23px;
    font-weight: bold;
    margin-top: 7px;
}

.green {
    color: #38df9b;
}

.red {
    color: #ff657d;
}

.yellow {
    color: #ffd166;
}

button, select {
    background: #263957;
    color: white;
    border: 1px solid #40577e;
    padding: 11px 15px;
    border-radius: 8px;
    margin: 4px;
}

button:active {
    opacity: .7;
}

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}

th, td {
    padding: 10px 6px;
    border-bottom: 1px solid #293958;
    text-align: left;
}

th {
    color: #9cabc9;
}

.signal {
    font-weight: bold;
}

#status {
    color: #38df9b;
}

#pairs {
    max-height: 180px;
    overflow-y: auto;
    font-size: 12px;
    line-height: 1.8;
    color: #b9c9e8;
    overflow-wrap: anywhere;
}

input {
    width: 100%;
    background: #0b1020;
    color: white;
    border: 1px solid #40577e;
    padding: 12px;
    border-radius: 8px;
}

pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: #b9c9e8;
    font-size: 12px;
}

@media (max-width: 500px) {
    .grid {
        grid-template-columns: 1fr 1fr;
    }

    .value {
        font-size: 19px;
    }
}
</style>
</head>

<body>

<header>
    <h1>⚡ QUOTEX OTC AI SCANNER</h1>
    <div class="label" style="margin-top:8px">
        M1 • 5 Engine Hybrid • Live Feed
    </div>
</header>

<main>

<div class="card">
    <div class="label">CONNECTION</div>
    <div class="value" id="status">CONNECTING</div>
    <div id="message" class="label">Starting scanner...</div>

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
    <div class="label" id="aiDetails">
        UP: 0 | DOWN: 0
    </div>
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
                <th>ANALYZE</th>
            </tr>
        </thead>
        <tbody id="results">
            <tr>
                <td colspan="4">Waiting for signals...</td>
            </tr>
        </tbody>
    </table>
</div>

<div class="card">
    <h3>PAIR ANALYSIS</h3>

    <input
        id="pairInput"
        placeholder="Example: EURUSD_otc"
    >

    <button onclick="analyzePair()">ANALYZE PAIR</button>

    <pre id="analysis">Select a pair to analyze.</pre>
</div>

<div class="card">
    <h3>DATABASE DIAGNOSTICS</h3>
    <pre id="diagnostics">Loading...</pre>
</div>

</main>

<script>
async function api(url) {
    const response = await fetch(url, {
        cache: "no-store"
    });

    return await response.json();
}

function escapeHtml(value) {
    return String(value ?? "").replace(
        /[&<>"']/g,
        char => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#039;"
        })[char]
    );
}

function signalClass(signal) {
    if (signal === "CALL" || signal === "UP") {
        return "green";
    }

    if (signal === "PUT" || signal === "DOWN") {
        return "red";
    }

    return "yellow";
}

function getPair(row) {
    return row.asset || row.pair || row.symbol || "";
}

function getSignal(row) {
    return String(
        row.action || row.signal || row.direction || "NEUTRAL"
    ).toUpperCase();
}

function getConfidence(row) {
    return row.confidence ??
           row.score ??
           row.strength ??
           "--";
}

function renderResults(rows) {
    const body = document.getElementById("results");

    if (!Array.isArray(rows) || rows.length === 0) {
        body.innerHTML =
            '<tr><td colspan="4">No signals yet. Waiting for candle data...</td></tr>';
        return;
    }

    body.innerHTML = rows.map(row => {
        const pair = getPair(row);
        const signal = getSignal(row);
        const confidence = getConfidence(row);

        return `
        <tr>
            <td>${escapeHtml(pair)}</td>
            <td class="signal ${signalClass(signal)}">
                ${escapeHtml(signal)}
            </td>
            <td>${escapeHtml(confidence)}</td>
            <td>
                <button onclick="quickAnalyze('${escapeHtml(pair)}')">
                    VIEW
                </button>
            </td>
        </tr>`;
    }).join("");
}

async function refresh() {
    try {
        const data = await api("/api/state");

        document.getElementById("status").textContent =
            data.connection || "UNKNOWN";

        document.getElementById("message").textContent =
            data.message || "";

        document.getElementById("pairCount").textContent =
            data.pair_count ?? (data.pairs || []).length;

        document.getElementById("seriesCount").textContent =
            data.database_series ?? 0;

        document.getElementById("candleCount").textContent =
            data.database_candles ?? 0;

        document.getElementById("tickCount").textContent =
            data.database_ticks ?? 0;

        document.getElementById("resultCount").textContent =
            data.result_count ?? (data.results || []).length;

        document.getElementById("lastScan").textContent =
            data.last_scan || "--";

        document.getElementById("aiStatus").textContent =
            data.ai_sensor_status || "WAITING";

        document.getElementById("aiDetails").textContent =
            "UP: " + (data.ai_sensor_up || 0) +
            " | DOWN: " + (data.ai_sensor_down || 0) +
            " | READY: " + (data.ai_sensor_ready || 0);

        document.getElementById("pairs").textContent =
            (data.pairs || []).join(" • ");

        renderResults(data.results || []);

        document.getElementById("diagnostics").textContent =
            "Database: " + (data.database_path || "--") +
            "\nCandles: " + (data.database_candles ?? 0) +
            "\nTicks: " + (data.database_ticks ?? 0) +
            "\nSeries: " + (data.database_series ?? 0) +
            "\nFeed: " + JSON.stringify(data.feed_status || {}, null, 2) +
            "\nLast error: " + (data.last_error || "None");

    } catch (error) {
        document.getElementById("status").textContent =
            "API ERROR";

        document.getElementById("message").textContent =
            error.message;
    }
}

async function startScanner() {
    try {
        await api("/api/start");
        setTimeout(refresh, 1000);
    } catch (error) {
        alert(error.message);
    }
}

async function stopScanner() {
    try {
        await api("/api/stop");
        await refresh();
    } catch (error) {
        alert(error.message);
    }
}

async function quickAnalyze(pair) {
    document.getElementById("pairInput").value = pair;
    await analyzePair();
}

async function analyzePair() {
    const pair = document.getElementById("pairInput").value.trim();

    if (!pair) {
        alert("Enter an OTC pair");
        return;
    }

    document.getElementById("analysis").textContent =
        "Analyzing " + pair + "...";

    try {
        const data = await api(
            "/api/analyze?pair=" + encodeURIComponent(pair)
        );

        document.getElementById("analysis").textContent =
            JSON.stringify(data, null, 2);

    } catch (error) {
        document.getElementById("analysis").textContent =
            error.message;
    }
}

refresh();
setInterval(refresh, 3000);
</script>

</body>
</html>
"""


def main():
    print(
        "QUOTEX OTC HYBRID DASHBOARD STARTING",
        flush=True,
    )

    server = ThreadingHTTPServer(
        (HOST, PORT),
        DashboardHandler,
    )

    server.daemon_threads = True

    print(
        "Dashboard running on port:",
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