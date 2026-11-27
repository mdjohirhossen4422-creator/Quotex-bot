#!/usr/bin/env python3
"""QX Quant - one-button 1-minute live OTC scanner."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from candle_store import CandleStore, MultiFeed, TIMEFRAMES, asset_catalog


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QX Quant · M1 Live Scanner</title>
<style>
:root{color-scheme:dark;--bg:#080d16;--panel:#111b2b;--line:#26364d;--txt:#edf4ff;--mut:#91a3bb;--green:#19d69a;--red:#ff6077;--blue:#6d9cff}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(140deg,#080d16,#101a2b);color:var(--txt);font:14px Arial,sans-serif}
header{padding:17px 20px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:19px;margin:0}.sub{color:var(--mut);font-size:12px}.pill{border:1px solid var(--line);padding:7px 11px;border-radius:30px;color:var(--mut)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#65748a;margin-right:6px}.dot.on{background:var(--green)}
main{max-width:1100px;margin:20px auto;padding:0 14px;display:grid;gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:17px}
.topline{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:12px;padding:15px 22px;font-size:16px;font-weight:bold;background:linear-gradient(120deg,#80b2ff,#6b80ff);color:#06101f;cursor:pointer}
button.stop{background:linear-gradient(120deg,#ff8797,#ff6077)}button:disabled{opacity:.5}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:15px}
.stat{background:#0b1422;border:1px solid var(--line);border-radius:10px;padding:12px}.stat small{display:block;color:var(--mut);margin-bottom:5px}.stat b{font-size:18px}
#chart{width:100%;height:260px;display:block;margin-top:10px}
#hint{color:var(--mut);margin-top:12px;line-height:1.5}
.sig{display:grid;grid-template-columns:75px 1fr auto;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding:12px 3px}
.sig:last-child{border:0}.tag{font-weight:bold;border-radius:7px;padding:5px 8px;text-align:center;font-size:11px}
.CALL{background:#123e37;color:var(--green)}.PUT{background:#481f2a;color:var(--red)}.NEUTRAL{background:#263346;color:#b6c3d5}
.pair{font-weight:bold}.why{font-size:12px;color:var(--mut);margin-top:4px}.score{font-size:12px;color:var(--mut);text-align:right}
.empty{text-align:center;color:var(--mut);padding:25px}
footer{text-align:center;color:var(--mut);font-size:12px;padding:15px}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.sig{grid-template-columns:65px 1fr}.score{grid-column:2;text-align:left}}
</style>
</head>
<body>
<header>
  <div><h1>QX QUANT · M1 LIVE SCANNER</h1><div class="sub">Quotex market-qx live feed · 1-minute candles</div></div>
  <span class="pill"><i class="dot" id="dot"></i><span id="feed">Connecting…</span></span>
  <span class="pill">OTC / pairs: <b id="pairs">—</b></span>
</header>
<main>
  <section class="card">
    <div class="topline">
      <div><b>Live market scanner</b><div class="sub">Press once to start scanning all discovered pairs every 15 seconds</div></div>
      <button id="toggle">▶ START M1 LIVE SCAN</button>
    </div>
    <div class="stats">
      <div class="stat"><small>Selected pair</small><b id="asset">—</b></div>
      <div class="stat"><small>Live price</small><b id="price">—</b></div>
      <div class="stat"><small>Ticks / second</small><b id="tps">—</b></div>
    </div>
    <div id="hint">Waiting for live feed…</div>
  </section>
  <section class="card">
    <div class="topline"><b>1-minute live candle chart</b><span class="sub" id="chartName">—</span></div>
    <canvas id="chart"></canvas>
  </section>
  <section class="card">
    <div class="topline"><b>Live scan signals</b><span class="sub">CALL / PUT / NEUTRAL · M1</span></div>
    <div id="results"><div class="empty">Press START M1 LIVE SCAN to begin.</div></div>
  </section>
</main>
<footer>Signals are statistical estimates, not guaranteed outcomes or financial advice. Use a demo account.</footer>
<script>
let active=false, timer=null, busy=false, asset='', pairs=[], lastState=null;
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function draw(cs){
  const c=$('chart'), dpr=devicePixelRatio||1, w=c.clientWidth, h=c.clientHeight;
  c.width=w*dpr;c.height=h*dpr;
  const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);
  if(!cs||cs.length<2){g.fillStyle='#91a3bb';g.font='13px Arial';g.fillText('Collecting 1-minute candles…',15,h/2);return}
  cs=cs.slice(-70);let hi=Math.max(...cs.map(x=>x[3])),lo=Math.min(...cs.map(x=>x[4]));
  const span=(hi-lo)||1;hi+=span*.06;lo-=span*.06;
  const y=v=>10+(h-20)*(1-(v-lo)/(hi-lo)), cw=(w-20)/cs.length;
  g.strokeStyle='#26364d';for(let i=0;i<5;i++){let yy=10+(h-20)*i/4;g.beginPath();g.moveTo(10,yy);g.lineTo(w-10,yy);g.stroke()}
  cs.forEach((x,i)=>{const up=x[2]>=x[1],col=up?'#19d69a':'#ff6077',xx=10+cw*(i+.5);
    g.strokeStyle=col;g.fillStyle=col;g.beginPath();g.moveTo(xx,y(x[3]));g.lineTo(xx,y(x[4]));g.stroke();
    const a=y(x[1]),b=y(x[2]);g.fillRect(xx-Math.max(1,cw*.55)/2,Math.min(a,b),Math.max(1,cw*.55),Math.max(1,Math.abs(b-a)));
  });
}
async function refresh(){
  try{
    const r=await fetch('/api/catalog');const j=await r.json();
    if(!j.loading&&j.groups){
      pairs=j.groups.flatMap(g=>g.symbols||[]);
      $('pairs').textContent=pairs.length;
      if(!asset)asset=j.default_asset||pairs[0]||'';
      $('asset').textContent=asset||'—';
    }
  }catch(e){}
  try{
    const r=await fetch('/api/state?asset='+encodeURIComponent(asset)+'&period=60');const s=await r.json();
    lastState=s;$('dot').className='dot'+(s.connected?' on':'');
    $('feed').textContent=s.connected?'LIVE FEED ONLINE':(s.error||'Connecting…');
    $('tps').textContent=s.tps??'—';
    $('price').textContent=(s.prices&&s.prices[asset]!=null)?s.prices[asset]:'—';
    const cs=(s.series||{})[asset+'|60']||[];draw(cs);
    $('chartName').textContent=asset+' · 1m · '+cs.length+' closed candles';
  }catch(e){$('feed').textContent='Feed request failed'}
}
async function scan(){
  if(!active||busy)return;busy=true;$('toggle').disabled=true;
  $('hint').textContent='Scanning every available pair using 1-minute candles…';
  try{
    const r=await fetch('/api/scan?periods=60&engine=rule&top=200');const j=await r.json();
    if(j.error)throw Error(j.error);
    const signals=j.signals||[];
    $('hint').textContent=(j.scanned??0)+' pair/timeframe series · '+(j.ready??0)+' ready · '+(j.signal_count??signals.length)+' signals · M1';
    if(!signals.length){$('results').innerHTML='<div class="empty">No qualifying signal now, or not enough closed candles yet. Scanner will check again.</div>';return}
    $('results').innerHTML=signals.map(s=>{
      const action=['CALL','PUT','NEUTRAL'].includes(s.action)?s.action:'NEUTRAL';
      const reason=(s.reasons||[]).slice(0,3).join(' · ');
      return '<div class="sig"><span class="tag '+action+'">'+action+'</span><div><div class="pair">'+safe(s.asset)+' <span class="sub">M1</span></div><div class="why">'+safe(reason)+'</div></div><div class="score">Score '+safe(s.score)+'<br>Bull '+safe(s.bull)+' / Bear '+safe(s.bear)+'</div></div>';
    }).join('');
  }catch(e){$('hint').textContent='Scan error: '+e.message}
  finally{busy=false;$('toggle').disabled=false}
}
function safe(v){return String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
$('toggle').onclick=()=>{
  active=!active;
  if(active){$('toggle').textContent='■ STOP LIVE SCAN';$('toggle').classList.add('stop');scan();timer=setInterval(scan,15000)}
  else{clearInterval(timer);timer=null;$('toggle').textContent='▶ START M1 LIVE SCAN';$('toggle').classList.remove('stop');$('hint').textContent='Live scan stopped.'}
};
async function loop(){await refresh();setTimeout(loop,1800)}
loop();
window.addEventListener('resize',()=>{if(lastState){const cs=(lastState.series||{})[asset+'|60']||[];draw(cs)}});
</script>
</body></html>"""


class Hub:
    def __init__(self, assets, store, wait_instruments=75):
        self.store = store
        self.assets = list(assets)
        self.feed = None
        self.instruments = {}
        self.ready = threading.Event()
        self.error = ""
        self.lock = threading.Lock()
        self.tick_count = 0
        self.last_count = 0
        self.tps = 0.0
        threading.Thread(target=self._boot, args=(wait_instruments,), daemon=True).start()

    def _boot(self, wait):
        try:
            self.feed = MultiFeed(self.assets, [60], self.store,
                                  on_tick=self._on_tick,
                                  wait_instruments=wait)
            self.assets = self.feed.assets
            self.instruments = self.feed.instruments
            self.ready.set()
            threading.Thread(target=self._tps_loop, daemon=True).start()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print("feed boot failed:", self.error, flush=True)

    def _on_tick(self, asset, ts, price):
        with self.lock:
            self.tick_count += 1

    def _tps_loop(self):
        while True:
            time.sleep(5)
            with self.lock:
                n = self.tick_count
                self.tps = round((n - self.last_count) / 5.0, 1)
                self.last_count = n

    def catalog(self):
        return {
            "groups": asset_catalog(self.instruments, self.assets) if self.ready.is_set() else [],
            "timeframes": [{"seconds": s, "label": l} for s, l in TIMEFRAMES],
            "default_asset": self.assets[0] if self.assets else "",
            "loading": not self.ready.is_set(),
            "error": self.error,
        }

    def state(self, asset, period):
        if not self.ready.is_set() or self.feed is None:
            return {"connected": False, "loading": True, "error": self.error,
                    "tps": 0, "prices": {}, "series": {}, "stored": self.store.total}
        with self.lock:
            tps = self.tps
        prices = dict(self.feed.last_price)
        series = {}
        if asset and period == 60:
            series[f"{asset}|60"] = self.feed.candles(asset, 60)[-70:]
        return {"connected": self.feed.connected, "loading": False, "tps": tps,
                "prices": prices, "series": series, "stored": self.store.total}


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None

    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
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
            return self.send_json(self.hub.catalog())
        if u.path == "/api/state":
            asset = (q.get("asset") or [""])[0]
            try:
                period = int((q.get("period") or ["60"])[0])
            except ValueError:
                period = 60
            return self.send_json(self.hub.state(asset, period))
        if u.path == "/api/scan":
            if not self.hub.ready.is_set():
                return self.send_json({"error": "Live feed is still connecting."}, 503)
            try:
                from scan import scan_all
                rep = scan_all(self.hub.store, [60], "rule", top=200)
                return self.send_json(rep)
            except Exception as exc:
                return self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="QX Quant M1 live scanner")
    ap.add_argument("--assets", default="", help="optional comma-separated asset list")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 12000)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--wait-instruments", type=float, default=75.0)
    args = ap.parse_args()
    store = CandleStore(args.db)
    assets = [x.strip() for x in args.assets.split(",") if x.strip()]
    hub = Hub(assets, store, args.wait_instruments)
    Handler.hub = hub
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"QX Quant M1 dashboard listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        if hub.feed:
            hub.feed.close()


if __name__ == "__main__":
    main()
