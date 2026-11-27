#!/usr/bin/env python3
"""Quotex / market-qx OTC M1 Live Scanner Dashboard."""

from __future__ import annotations

import os
import json
import time
import threading
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from candle_store import CandleStore, MultiFeed, asset_catalog
from scan import scan_all


PERIOD = 60
SCAN_INTERVAL = 15


PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quotex OTC M1 Scanner</title>
<style>
*{box-sizing:border-box}
body{
 margin:0;background:#080d17;color:#eaf1ff;
 font-family:Arial,sans-serif;padding:15px
}
.wrap{max-width:1000px;margin:auto}
header{
 background:#111c30;padding:18px;border-radius:14px;
 border:1px solid #263653;margin-bottom:15px
}
h2{margin:0 0 8px;color:#65a5ff}
.status{font-size:13px;color:#9eacc5}
.card{
 background:#111c30;border:1px solid #263653;
 border-radius:14px;padding:16px;margin-bottom:15px
}
button,select{
 width:100%;padding:14px;border-radius:9px;
 border:1px solid #344967;background:#182741;
 color:white;font-size:15px;margin-top:8px
}
button{font-weight:bold;cursor:pointer}
.start{background:#087c58;border:0}
.stop{background:#b52e45;border:0}
.signal{
 text-align:center;padding:25px 10px;
 background:#0b1424;border-radius:12px;margin-top:15px
}
.signal h1{font-size:42px;margin:10px 0}
.call{color:#16e0a0}
.put{color:#ff6478}
.neutral{color:#f5c451}
.grid{
 display:grid;grid-template-columns:1fr 1fr;gap:10px
}
.stat{
 background:#0b1424;border-radius:10px;padding:14px;
 overflow-wrap:anywhere
}
.stat small{color:#91a2bd}
.stat b{display:block;font-size:18px;margin-top:7px}
#chart{
 width:100%;height:230px;background:#0b1424;
 border-radius:10px;margin-top:12px
}
.item{
 padding:12px 4px;border-bottom:1px solid #263653;
 display:flex;justify-content:space-between;gap:10px
}
.muted{color:#91a2bd;font-size:12px}
.green{color:#16e0a0}
.red{color:#ff6478}
@media(max-width:550px){
 .grid{grid-template-columns:1fr 1fr}
 .signal h1{font-size:34px}
}
</style>
</head>
<body>
<div class="wrap">

<header>
 <h2>◆ QUOTEX OTC SCANNER</h2>
 <div class="status" id="status">Connecting to live market...</div>
</header>

<div class="card">
 <h3>Live Scanner Control</h3>
 <label>OTC Market Pair</label>
 <select id="pair"></select>

 <button id="toggle" class="start" onclick="toggleScan()">
 ▶ START M1 LIVE SCAN
 </button>

 <p class="muted">
 Timeframe: 1 Minute | Automatic scan: Every 15 seconds
 </p>
</div>

<div class="card">
 <h3>Live Signal</h3>
 <div class="signal">
  <div class="muted" id="signalPair">Waiting for scan</div>
  <h1 id="signal">WAIT</h1>
  <div id="reason" class="muted">Start scanner to analyze market</div>
 </div>
</div>

<div class="card">
 <h3>Market Information</h3>
 <div class="grid">
  <div class="stat"><small>Live Price</small><b id="price">—</b></div>
  <div class="stat"><small>Timeframe</small><b>1 MIN</b></div>
  <div class="stat"><small>Stored Candles</small><b id="count">0</b></div>
  <div class="stat"><small>Last Update</small><b id="updated">—</b></div>
 </div>
</div>

<div class="card">
 <h3>Live Candle Chart</h3>
 <canvas id="chart"></canvas>
 <div class="muted">Market-qx candle data</div>
</div>

<div class="card">
 <h3>Scanner Results</h3>
 <div id="results" class="muted">No scan results yet</div>
</div>

<div class="card">
 <h3>Scanner Information</h3>
 <div class="muted" id="info">
 Scanner stopped
 </div>
</div>

</div>

<script>
let running=false;
let scanTimer=null;
let stateTimer=null;
let candles=[];
let pairs=[];

const $=id=>document.getElementById(id);

async function request(url){
 const r=await fetch(url,{cache:"no-store"});
 if(!r.ok)throw new Error("HTTP "+r.status);
 return await r.json();
}

async function loadPairs(){
 try{
  const j=await request("/api/catalog");
  pairs=j.pairs||[];
  $("pair").innerHTML="";

  pairs.forEach(p=>{
   const o=document.createElement("option");
   o.value=p;
   o.textContent=p;
   $("pair").appendChild(o);
  });

  if(j.loading){
   $("status").textContent="Loading market instruments...";
   setTimeout(loadPairs,2000);
   return;
  }

  $("status").textContent="Live feed connected · "+pairs.length+" pairs";
  if(!pairs.length){
   $("status").textContent="No OTC pairs discovered";
  }
 }catch(e){
  $("status").textContent="Feed connection pending: "+e.message;
  setTimeout(loadPairs,3000);
 }
}

async function toggleScan(){
 if(running){
  stopScan();
 }else{
  startScan();
 }
}

function startScan(){
 if(!pairs.length){
  $("info").textContent="Waiting for OTC market instruments...";
  loadPairs();
  return;
 }

 running=true;
 $("toggle").textContent="■ STOP LIVE SCAN";
 $("toggle").className="stop";
 $("info").textContent="M1 scanner is running";
 scanNow();
 scanTimer=setInterval(scanNow,15000);
 stateTimer=setInterval(loadState,1800);
}

function stopScan(){
 running=false;
 clearInterval(scanTimer);
 clearInterval(stateTimer);
 $("toggle").textContent="▶ START M1 LIVE SCAN";
 $("toggle").className="start";
 $("info").textContent="Scanner stopped";
}

async function scanNow(){
 if(!running)return;

 $("info").textContent="Scanning all OTC pairs...";
 try{
  const j=await request("/api/scan");
  const list=j.results||j.signals||[];

  $("results").innerHTML="";

  if(!list.length){
   $("results").textContent=
    "No signals yet. Waiting for enough closed M1 candles.";
   return;
  }

  list.forEach(x=>{
   const action=String(x.action||x.signal||"NEUTRAL").toUpperCase();
   const cls=action==="CALL"||action==="UP"?"green":
             action==="PUT"||action==="DOWN"?"red":"muted";

   const div=document.createElement("div");
   div.className="item";

   const left=document.createElement("div");
   const name=document.createElement("b");
   name.textContent=x.asset||x.pair||"Unknown";
   const detail=document.createElement("div");
   detail.className="muted";
   detail.textContent=x.note||x.reason||"M1 analysis";
   left.append(name,detail);

   const right=document.createElement("b");
   right.className=cls;
   right.textContent=action;

   div.append(left,right);
   $("results").appendChild(div);
  });

  $("info").textContent="Last scan: "+new Date().toLocaleTimeString();
 }catch(e){
  $("info").textContent="Scan error: "+e.message;
 }
}

async function loadState(){
 const asset=$("pair").value;
 if(!asset)return;

 try{
  const j=await request(
   "/api/state?asset="+encodeURIComponent(asset)+"&period=60"
  );

  const c=j.candles||[];
  candles=c;
  $("count").textContent=c.length;

  if(c.length){
   const last=c[c.length-1];
   $("price").textContent=Number(last[4]).toFixed(6);
   $("updated").textContent=new Date().toLocaleTimeString();
   drawChart();
  }
 }catch(e){
  $("status").textContent="State error: "+e.message;
 }
}

function drawChart(){
 const canvas=$("chart");
 const ctx=canvas.getContext("2d");
 const w=canvas.clientWidth;
 const h=230;
 const dpr=window.devicePixelRatio||1;

 canvas.width=w*dpr;
 canvas.height=h*dpr;
 ctx.scale(dpr,dpr);
 ctx.clearRect(0,0,w,h);

 if(!candles.length)return;

 const data=candles.slice(-45);
 const highs=data.map(x=>Number(x[3]));
 const lows=data.map(x=>Number(x[2]));
 const max=Math.max(...highs);
 const min=Math.min(...lows);
 const range=max-min||1;
 const top=15,bottom=15;
 const usable=h-top-bottom;
 const step=w/data.length;
 const body=Math.max(3,step*.48);

 data.forEach((c,i)=>{
  const o=Number(c[1]),cl=Number(c[4]);
  const hi=Number(c[3]),lo=Number(c[2]);
  const x=i*step+step/2;
  const y=v=>top+(max-v)/range*usable;
  const up=cl>=o;

  ctx.strokeStyle=up?"#16e0a0":"#ff6478";
  ctx.fillStyle=ctx.strokeStyle;
  ctx.beginPath();
  ctx.moveTo(x,y(hi));
  ctx.lineTo(x,y(lo));
  ctx.stroke();

  const by=Math.min(y(o),y(cl));
  const bh=Math.max(2,Math.abs(y(o)-y(cl)));
  ctx.fillRect(x-body/2,by,body,bh);
 });
}

$("pair").addEventListener("change",()=>{
 if(running)loadState();
});

loadPairs();
setInterval(()=>{
 if(!running)loadPairs();
},10000);
</script>
</body>
</html>
"""


class Hub:
    def __init__(self, assets, store):
        self.store = store
        self.assets = assets
        self.feed = None
        self.feed_thread = None
        self.lock = threading.RLock()
        self.error = ""

        self.start_feed()

    def start_feed(self):
        try:
            self.feed = MultiFeed(
                self.assets,
                [PERIOD],
                self.store
            )

            start = getattr(self.feed, "start", None)

            if callable(start):
                start()
            else:
                run = getattr(self.feed, "run", None)
                if callable(run):
                    self.feed_thread = threading.Thread(
                        target=run,
                        daemon=True
                    )
                    self.feed_thread.start()
                else:
                    self.error = "MultiFeed has no start/run method"

        except Exception as exc:
            self.error = str(exc)
            print("FEED ERROR:", exc)

    def get_candles(self, asset):
        try:
            if self.feed:
                data = self.feed.candles(asset, PERIOD)
                if data:
                    return data
        except Exception:
            pass

        try:
            return self.store.load(asset, PERIOD, limit=300)
        except Exception:
            return []

    def state(self, asset):
        data = self.get_candles(asset)
        return {
            "asset": asset,
            "period": PERIOD,
            "candles": data[-300:],
            "count": len(data),
            "error": self.error
        }

    def close(self):
        try:
            if self.feed and hasattr(self.feed, "stop"):
                self.feed.stop()
        except Exception:
            pass


def discover_otc_assets():
    catalog = asset_catalog()
    found = []

    def walk(value):
        if isinstance(value, dict):
            symbols = value.get("symbols")
            if isinstance(symbols, (list, tuple)):
                for symbol in symbols:
                    if isinstance(symbol, str):
                        found.append(symbol)

            for key, child in value.items():
                if key != "symbols":
                    walk(child)

        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(catalog)

    # Keep only symbols explicitly marked as OTC.
    result = sorted(set(
        x for x in found
        if x.lower().endswith("_otc")
    ))

    return result


class Handler(BaseHTTPRequestHandler):
    hub = None

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/api/catalog":
            self.send_json({
                "pairs": self.hub.assets,
                "loading": not bool(self.hub.assets),
                "error": self.hub.error
            })
            return

        if u.path == "/api/state":
            asset = (q.get("asset") or [
                self.hub.assets[0] if self.hub.assets else ""
            ])[0]

            if asset not in self.hub.assets:
                self.send_json({"error": "Unknown asset"}, 404)
                return

            self.send_json(self.hub.state(asset))
            return

        if u.path == "/api/scan":
            try:
                report = scan_all(
                    self.hub.store,
                    [PERIOD],
                    "rule",
                    top=200
                )
                self.send_json(report)
            except Exception as exc:
                self.send_json({
                    "error": str(exc),
                    "results": []
                }, 500)
            return

        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--assets", default="")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "10000"))
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--db",
        default=os.environ.get("CANDLE_DB", "data/candles.db")
    )

    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)

    store = CandleStore(args.db)

    assets = [
        x.strip()
        for x in args.assets.split(",")
        if x.strip()
    ]

    if not assets:
        assets = discover_otc_assets()

    if not assets:
        print("WARNING: No OTC instruments discovered.")

    hub = Hub(assets, store)
    Handler.hub = hub

    server = ThreadingHTTPServer(
        (args.host, args.port),
        Handler
    )

    print("QUOTEX OTC M1 SCANNER STARTED")
    print("Port:", args.port)
    print("OTC pairs:", len(assets))
    print("Timeframe: 60 seconds")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
        server.server_close()


if __name__ == "__main__":
    main()