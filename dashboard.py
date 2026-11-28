#!/usr/bin/env python3
"""Quotex OTC M1 Hybrid Dashboard with AI Sensor and UP/DOWN signals."""

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


PAGE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quotex OTC AI Sensor Scanner</title>

<style>
*{box-sizing:border-box}

body{
 background:#07111f;
 color:#e8f0ff;
 font-family:Arial,sans-serif;
 padding:14px;
 margin:0
}

main{max-width:1100px;margin:auto}
h2{color:#55d6ff}
h3{margin-top:0}

.card{
 background:#101e31;
 border:1px solid #263b56;
 border-radius:12px;
 padding:15px;
 margin:12px 0
}

.grid{
 display:grid;
 grid-template-columns:repeat(3,1fr);
 gap:10px
}

.stat{
 background:#091729;
 padding:12px;
 border-radius:8px;
 min-width:0
}

small{
 color:#91a5c4;
 display:block;
 margin-bottom:8px
}

button,select{
 padding:12px;
 border:0;
 border-radius:7px;
 font-weight:bold;
 margin:5px 3px
}

button{cursor:pointer}
.start{background:#19c37d}
.stop{background:#ff5268;color:white}
.analyze{background:#299bff;color:white}

select{
 background:#07111f;
 color:white;
 width:100%;
 border:1px solid #344966
}

.green,.UP,.CALL{color:#35df9a}
.red,.DOWN,.PUT{color:#ff687b}
.yellow,.NEUTRAL{color:#ffc857}

.signal{
 font-size:30px;
 font-weight:bold;
 margin:12px 0
}

.timer{
 font-size:35px;
 font-weight:bold;
 color:#55d6ff;
 font-family:monospace
}

.progress{
 height:8px;
 background:#263b56;
 border-radius:10px;
 overflow:hidden;
 margin-top:10px
}

#progressBar{
 height:100%;
 width:0%;
 background:#19c37d;
 transition:width .3s linear
}

.engine-grid{
 display:grid;
 grid-template-columns:repeat(5,1fr);
 gap:8px;
 margin-top:12px
}

.engine{
 background:#091729;
 border:1px solid #263b56;
 border-radius:8px;
 padding:10px;
 min-width:0;
 text-align:center
}

.engine b{
 display:block;
 margin-top:7px;
 font-size:14px;
 overflow-wrap:anywhere
}

.engine small{
 font-size:10px;
 margin-bottom:4px
}

.sensor{
 border:1px solid #287b68;
 background:#0c2928
}

table{
 width:100%;
 border-collapse:collapse;
 min-width:650px;
 font-size:13px
}

td,th{
 padding:10px 6px;
 text-align:left;
 border-bottom:1px solid #263b56;
 vertical-align:top
}

th{color:#91a5c4}
.scroll{overflow-x:auto}

#message{
 color:#ffc857;
 font-size:13px
}

.reason-list{
 color:#a9bdd8;
 font-size:13px;
 line-height:1.7;
 overflow-wrap:anywhere
}

.badge{
 display:inline-block;
 padding:5px 9px;
 border-radius:6px;
 background:#263b56;
 font-size:12px;
 margin:3px
}

@media(max-width:650px){
 .grid{grid-template-columns:repeat(2,1fr)}
 .engine-grid{grid-template-columns:repeat(2,1fr)}
 .timer{font-size:30px}
}
</style>
</head>

<body>
<main>

<h2>QUOTEX OTC M1 AI SENSOR SCANNER</h2>
<p><small>Live Feed | 5 Engines | AI Sensor | UP/DOWN</small></p>

<div class="card">
<h3>M1 CANDLE COUNTDOWN</h3>
<div class="timer" id="countdown">01:00</div>
<div class="progress">
 <div id="progressBar"></div>
</div>
<p><small id="candleStatus">Waiting for server time...</small></p>
</div>

<div class="card">
<div class="grid">

<div class="stat">
 <small>CONNECTION</small>
 <b id="connection">OFFLINE</b>
</div>

<div class="stat">
 <small>OTC PAIRS</small>
 <b id="pairs">0</b>
</div>

<div class="stat">
 <small>TIMEFRAME</small>
 <b>1 MIN</b>
</div>

<div class="stat">
 <small>SCANNER</small>
 <b id="scanner">STOPPED</b>
</div>

<div class="stat">
 <small>SCAN INTERVAL</small>
 <b>15 SEC</b>
</div>

<div class="stat">
 <small>LAST SCAN</small>
 <b id="last">WAITING</b>
</div>

</div>

<button class="start" onclick="startScan()">START SCANNER</button>
<button class="stop" onclick="stopScan()">STOP SCANNER</button>
<p id="message">Waiting...</p>
</div>


<div class="card">
<h3>AI SENSOR STATUS</h3>

<div class="grid">

<div class="stat">
 <small>AI SENSOR</small>
 <b id="aiStatus" class="yellow">WAITING</b>
</div>

<div class="stat">
 <small>AI READY PAIRS</small>
 <b id="aiReady">0</b>
</div>

<div class="stat">
 <small>HYBRID ENGINES</small>
 <b>5 ENGINES</b>
</div>

</div>

<div class="engine-grid">

<div class="engine">
 <small>RULE</small>
 <b id="engineRule">WAIT</b>
</div>

<div class="engine">
 <small>SMART</small>
 <b id="engineSmart">WAIT</b>
</div>

<div class="engine">
 <small>ENSEMBLE</small>
 <b id="engineEnsemble">WAIT</b>
</div>

<div class="engine">
 <small>PRICE ACTION</small>
 <b id="enginePa">WAIT</b>
</div>

<div class="engine sensor">
 <small>AI SENSOR</small>
 <b id="engineAi">WAIT</b>
</div>

</div>
</div>


<div class="card">
<h3>PAIR ANALYSIS</h3>

<select id="pair">
 <option value="">Waiting for pairs...</option>
</select>

<button class="analyze" onclick="analyzePair()">ANALYZE PAIR</button>

<div id="signal" class="signal">WAITING</div>
<p id="reason">Select a pair.</p>
<p id="confidence">Confidence: --</p>

<h3>5 ENGINE VOTES</h3>
<div id="votes" class="reason-list">Waiting for analysis...</div>

<h3>AI SENSOR DETAILS</h3>
<div id="aiDetails" class="reason-list">Waiting for analysis...</div>
</div>


<div class="card">
<h3>LIVE HYBRID SCAN RESULTS</h3>

<div class="scroll">
<table>
<thead>
<tr>
 <th>PAIR</th>
 <th>TIME</th>
 <th>UP/DOWN</th>
 <th>SCORE</th>
 <th>CONFIDENCE</th>
 <th>AI SENSOR</th>
 <th>REASON</th>
</tr>
</thead>
<tbody id="results">
<tr><td colspan="7">Waiting for data...</td></tr>
</tbody>
</table>
</div>
</div>

<p><small>
AI Sensor is a local indicator-based analysis, not a guaranteed prediction.
Confidence scores are not verified win probabilities. Test on demo first.
</small></p>

</main>

<script>
let oldPairs = [];
let serverOffset = 0;
let clockSynced = false;

async function api(url, method="GET"){
 const r = await fetch(url,{
  method,
  cache:"no-store"
 });

 if(!r.ok) throw new Error("HTTP "+r.status);
 return await r.json();
}

function syncClock(serverTime){
 if(!serverTime) return;
 serverOffset = serverTime * 1000 - Date.now();
 clockSynced = true;
}

function updateCountdown(){
 const timer = document.getElementById("countdown");
 const bar = document.getElementById("progressBar");
 const status = document.getElementById("candleStatus");

 if(!clockSynced){
  timer.textContent = "--:--";
  return;
 }

 const now = Date.now() + serverOffset;
 const milliseconds = now % 60000;

 let remaining = Math.ceil((60000-milliseconds)/1000);
 if(remaining < 1) remaining = 60;

 timer.textContent =
  String(Math.floor(remaining/60)).padStart(2,"0")
  + ":" +
  String(remaining%60).padStart(2,"0");

 bar.style.width = (milliseconds/60000*100)+"%";
 status.textContent = "Next M1 candle in "+remaining+" seconds";
}

setInterval(updateCountdown,200);

function updatePairs(pairs){
 if(JSON.stringify(pairs) === JSON.stringify(oldPairs)) return;

 const el = document.getElementById("pair");
 const old = el.value;

 oldPairs = pairs.slice();
 el.innerHTML = "";

 pairs.forEach(p=>{
  const o = document.createElement("option");
  o.value = p;
  o.textContent = p;
  el.appendChild(o);
 });

 if(pairs.includes(old)) el.value = old;
}

function directionOf(r){
 const d = String(r.direction || r.display_signal || "").toUpperCase();

 if(d === "UP" || d === "DOWN") return d;

 const a = String(r.action || r.signal || "NEUTRAL").toUpperCase();

 if(a === "CALL" || a === "UP") return "UP";
 if(a === "PUT" || a === "DOWN") return "DOWN";

 return "NEUTRAL";
}

function displayConfidence(value){
 if(typeof value !== "number") return "--";
 return (value <= 1 ? value*100 : value).toFixed(1)+"%";
}

function setDirection(el, direction){
 el.textContent = direction === "UP" ? "UP ↑" :
  direction === "DOWN" ? "DOWN ↓" : "NEUTRAL";

 el.className = "signal " + direction;
}

function render(rows){
 const body = document.getElementById("results");
 body.innerHTML = "";

 if(!rows.length){
  body.innerHTML =
   '<tr><td colspan="7">Waiting for closed candles...</td></tr>';
  return;
 }

 rows.forEach(r=>{
  const direction = directionOf(r);
  const votes = (r.values || {}).engine_votes || {};
  const ai = votes.ai_sensor || {};
  const tr = document.createElement("tr");

  const values = [
   r.asset || r.symbol || "-",
   "1m",
   direction === "UP" ? "UP ↑" :
    direction === "DOWN" ? "DOWN ↓" : "NEUTRAL",
   r.score ?? "-",
   displayConfidence(r.confidence),
   ai.action || "WAIT",
   r.note || r.reason || "-"
  ];

  values.forEach((v,i)=>{
   const td = document.createElement("td");
   td.textContent = String(v);

   if(i===2 || i===5){
    td.className = String(v).startsWith("UP") ||
     v === "CALL" ? "UP" :
     String(v).startsWith("DOWN") ||
     v === "PUT" ? "DOWN" : "NEUTRAL";
   }

   tr.appendChild(td);
  });

  body.appendChild(tr);
 });
}

function updateEngineCards(row){
 const votes = (row && row.values && row.values.engine_votes) || {};

 const mapping = {
  rule:"engineRule",
  smart:"engineSmart",
  ensemble:"engineEnsemble",
  pa:"enginePa",
  ai_sensor:"engineAi"
 };

 Object.keys(mapping).forEach(name=>{
  const item = votes[name] || {};
  const action = String(item.direction || item.action || "WAIT").toUpperCase();
  const el = document.getElementById(mapping[name]);

  el.textContent = action;
  el.className = action === "UP" || action === "CALL" ? "UP" :
   action === "DOWN" || action === "PUT" ? "DOWN" : "NEUTRAL";
 });
}

function renderPairAnalysis(row){
 if(!row) return;

 const direction = directionOf(row);
 setDirection(document.getElementById("signal"),direction);

 document.getElementById("reason").textContent =
  row.note || row.reason || "Analysis complete";

 document.getElementById("confidence").textContent =
  "Confidence score: " + displayConfidence(row.confidence);

 const votes = (row.values || {}).engine_votes || {};
 const voteEl = document.getElementById("votes");
 voteEl.innerHTML = "";

 ["rule","smart","ensemble","pa","ai_sensor"].forEach(name=>{
  const v = votes[name] || {};
  const action = v.direction || v.action || "WAIT";
  const line = document.createElement("div");
  line.textContent = name.toUpperCase() + ": " +
   action + " | " + displayConfidence(
    typeof v.confidence === "number" ? v.confidence/100 : null
   );
  voteEl.appendChild(line);
 });

 const ai = votes.ai_sensor || {};
 const details = (row.values || {}).ai_sensor || {};
 const aiEl = document.getElementById("aiDetails");

 aiEl.textContent =
  "AI direction: " + (ai.direction || ai.action || "WAIT") +
  " | Sensor confidence: " + displayConfidence(
   typeof ai.confidence === "number" ? ai.confidence/100 : null
  );

 updateEngineCards(row);
}

async function refresh(){
 try{
  const s = await api("/api/state");
  syncClock(s.server_time);

  const c = document.getElementById("connection");
  c.textContent = s.connection;
  c.className = s.connection === "CONNECTED" ? "green" : "yellow";

  document.getElementById("pairs").textContent = s.pairs.length;

  const sc = document.getElementById("scanner");
  sc.textContent = s.running ? "RUNNING" : "STOPPED";
  sc.className = s.running ? "green" : "yellow";

  document.getElementById("last").textContent = s.last_scan || "WAITING";
  document.getElementById("message").textContent = s.message || "";

  const ai = document.getElementById("aiStatus");
  ai.textContent = s.ai_sensor_status || "WAITING";
  ai.className = s.ai_sensor_status === "ACTIVE" ? "green" : "yellow";

  document.getElementById("aiReady").textContent =
   (s.ai_sensor_ready || 0) + " / " + (s.ai_sensor_total || 0);

  updatePairs(s.pairs || []);
  render(s.results || []);

  if(s.results && s.results.length){
   const selected = document.getElementById("pair").value;
   const row = s.results.find(r =>
    (r.asset || r.symbol) === selected
   ) || s.results[0];

   updateEngineCards(row);
  }

 }catch(e){
  document.getElementById("message").textContent = e.message;
 }
}

async function startScan(){
 try{
  const r = await api("/api/start","POST");
  document.getElementById("message").textContent = r.message;
  refresh();
 }catch(e){
  document.getElementById("message").textContent = e.message;
 }
}

async function stopScan(){
 try{
  const r = await api("/api/stop","POST");
  document.getElementById("message").textContent = r.message;
  refresh();
 }catch(e){
  document.getElementById("message").textContent = e.message;
 }
}

async function analyzePair(){
 const p = document.getElementById("pair").value;

 if(!p){
  alert("No pair available");
  return;
 }

 try{
  const r = await api("/api/analyze?pair="+encodeURIComponent(p));

  if(r.row){
   renderPairAnalysis(r.row);
  }else{
   document.getElementById("signal").textContent = "WAITING";
   document.getElementById("reason").textContent = r.message;
  }
 }catch(e){
  document.getElementById("reason").textContent = e.message;
 }
}

refresh();
setInterval(refresh,3000);
</script>

</body>
</html>
"""


class Scanner:

    def __init__(self):
        self.store = CandleStore()
        self.lock = threading.RLock()

        self.feed = None
        self.thread = None
        self.event = None

        self.running = False
        self.connection = "DISCONNECTED"
        self.message = "Scanner stopped"

        self.pairs = []
        self.results = []
        self.last_scan = ""

    def _sensor_stats(self, rows):
        ready = 0
        total = len(rows)

        for row in rows:
            votes = (row.get("values") or {}).get(
                "engine_votes", {}
            )
            ai = votes.get("ai_sensor", {})
            if ai.get("action") not in (None, "WAIT", "ERROR"):
                ready += 1

        return {
            "ai_sensor_ready": ready,
            "ai_sensor_total": total,
            "ai_sensor_status": (
                "ACTIVE" if ready > 0 else "WAITING"
            ),
        }

    def state(self):
        with self.lock:
            rows = list(self.results)

            data = {
                "running": self.running,
                "connection": self.connection,
                "message": self.message,
                "pairs": list(self.pairs),
                "results": rows,
                "last_scan": self.last_scan,
                "server_time": time.time(),
            }

            data.update(self._sensor_stats(rows))
            return data

    def start(self):
        with self.lock:
            if self.running:
                return {"message": "Scanner already running"}

            self.event = threading.Event()
            self.running = True
            self.connection = "CONNECTING"
            self.message = "Connecting to MarketQX..."
            self.results = []

            self.thread = threading.Thread(
                target=self.worker,
                args=(self.event,),
                daemon=True
            )
            self.thread.start()

        return {"message": "Scanner starting..."}

    def stop(self):
        with self.lock:
            event = self.event
            feed = self.feed
            self.running = False

            if event:
                event.set()

        if feed:
            try:
                feed.close()
            except Exception:
                pass

        return {"message": "Scanner stopped"}

    def worker(self, event):
        feed = None

        try:
            feed = MultiFeed(
                assets=None,
                periods=[PERIOD],
                store=self.store,
                wait_instruments=75
            )

            with self.lock:
                self.feed = feed
                self.pairs = sorted(list(feed.assets or []))
                self.connection = "CONNECTED"
                self.message = "Live feed connected"

            while not event.is_set():
                try:
                    data = scan_all(
                        self.store,
                        [PERIOD],
                        engine="hybrid",
                        top=200,
                        include_neutral=True,
                        min_candles=0,
                        skip_dead=False
                    )

                    if isinstance(data, dict):
                        rows = data.get(
                            "signals",
                            data.get("results", [])
                        )
                    elif isinstance(data, list):
                        rows = data
                    else:
                        rows = []

                    with self.lock:
                        self.results = [
                            dict(r)
                            for r in rows
                            if isinstance(r, dict)
                        ]

                        self.last_scan = time.strftime("%H:%M:%S")
                        self.message = (
                            "5-engine hybrid scanning: "
                            + str(len(self.results))
                            + " results"
                        )

                except Exception as exc:
                    with self.lock:
                        self.message = "Scan error: " + str(exc)
                    traceback.print_exc()

                event.wait(SCAN_INTERVAL)

        except Exception as exc:
            with self.lock:
                self.connection = "ERROR"
                self.message = "Feed error: " + str(exc)
            traceback.print_exc()

        finally:
            if feed:
                try:
                    feed.close()
                except Exception:
                    pass

            with self.lock:
                if self.feed is feed:
                    self.feed = None

                self.running = False

                if event.is_set():
                    self.connection = "DISCONNECTED"
                    self.message = "Scanner stopped"

    def analyze_pair(self, pair):
        with self.lock:
            rows = list(self.results)

        for row in rows:
            asset = (
                row.get("asset")
                or row.get("symbol")
                or row.get("pair")
            )

            if asset == pair:
                return {
                    "row": row,
                    "message": "Hybrid analysis found"
                }

        try:
            data = scan_all(
                self.store,
                [PERIOD],
                engine="hybrid",
                top=200,
                include_neutral=True,
                min_candles=0,
                skip_dead=False
            )

            rows = (
                data.get("signals", data.get("results", []))
                if isinstance(data, dict)
                else data if isinstance(data, list)
                else []
            )

            for row in rows or []:
                asset = (
                    row.get("asset")
                    or row.get("symbol")
                    or row.get("pair")
                )

                if asset == pair:
                    return {
                        "row": row,
                        "message": "Hybrid analysis complete"
                    }

        except Exception as exc:
            return {"row": None, "message": str(exc)}

        return {
            "row": None,
            "message": "Waiting for closed M1 candles or sufficient history."
        }


bot = Scanner()


class Handler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path)

        if path.path == "/":
            body = PAGE.encode()

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.path == "/api/state":
            self.send_json(bot.state())
            return

        if path.path == "/api/analyze":
            query = parse_qs(path.query)
            pair = query.get("pair", [""])[0]

            if not pair:
                self.send_json(
                    {"row": None, "message": "Select a pair"},
                    400
                )
                return

            self.send_json(bot.analyze_pair(pair))
            return

        if path.path == "/health":
            self.send_json({"ok": True})
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/start":
            self.send_json(bot.start())
            return

        if path == "/api/stop":
            self.send_json(bot.stop())
            return

        self.send_json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args)


def main():
    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    print(
        "Quotex OTC M1 AI Sensor Dashboard started on port",
        PORT,
        flush=True
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()
        bot.store.close()
        server.server_close()


if __name__ == "__main__":
    main()