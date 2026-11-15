#!/usr/bin/env python3
"""Live web dashboard for the market-qx signal bot (multi-pair, multi-timeframe).

The user picks a pair and a timeframe, then presses Analyze. The bot computes
the indicator set on the stored candles for that exact (pair, timeframe) and
returns an explainable CALL / PUT / NEUTRAL verdict.

A single websocket connection streams every pair on every timeframe; closed
candles are persisted to SQLite (candle_store.py) so history accumulates across
restarts. Stdlib only — no Flask/etc.

Usage
-----
    .venv/bin/python dashboard.py --port 12000
    .venv/bin/python dashboard.py --assets EURUSD_otc,BTCUSD_otc --port 12000
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from analysis import analyze, analyze_smart, analyze_ensemble
from candle_store import CandleStore, MultiFeed, PERIODS, TIMEFRAMES, asset_catalog
from journal import Journal, settle_open, summary as journal_summary
from payout import DEFAULT_PAYOUT

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QX Quant · AI Signal Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#05070c; --bg2:#080b12; --panel:rgba(20,26,38,.72); --panel2:#0d1220;
    --line:rgba(120,140,180,.14); --line2:rgba(120,140,180,.24);
    --txt:#eaf0f8; --mut:#8794ab; --mut2:#5d6a80;
    --up:#16e0a0; --up-dim:rgba(22,224,160,.14);
    --down:#ff5c72; --down-dim:rgba(255,92,114,.14);
    --acc:#5b8cff; --acc2:#8b6cff; --gold:#f5c451;
    --glow:0 0 0 1px rgba(120,140,180,.12), 0 18px 60px -20px rgba(0,0,0,.9);
    --radius:18px;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;color:var(--txt);font:14.5px/1.5 Inter,-apple-system,Segoe UI,Roboto,sans-serif;
    background:
      radial-gradient(1100px 620px at 12% -8%, rgba(91,140,255,.16), transparent 60%),
      radial-gradient(1000px 560px at 100% 0%, rgba(139,108,255,.14), transparent 55%),
      radial-gradient(900px 700px at 60% 120%, rgba(22,224,160,.07), transparent 60%),
      linear-gradient(180deg,var(--bg),var(--bg2));
    background-attachment:fixed;min-height:100vh;
  }
  a{color:var(--acc)}
  .mono{font-family:'JetBrains Mono',ui-monospace,Menlo,monospace}

  /* ---------- top bar ---------- */
  header.top{
    position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
    padding:14px 26px;background:rgba(6,9,15,.72);backdrop-filter:blur(18px) saturate(160%);
    border-bottom:1px solid var(--line);
  }
  .brand{display:flex;align-items:center;gap:12px;font-weight:800;letter-spacing:.2px}
  .logo{
    width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-size:19px;
    background:linear-gradient(135deg,var(--acc),var(--acc2));
    box-shadow:0 10px 30px -8px rgba(91,140,255,.7), inset 0 1px 0 rgba(255,255,255,.35);
  }
  .brand small{display:block;font-weight:500;font-size:11px;color:var(--mut);letter-spacing:.14em;text-transform:uppercase}
  .pill{
    display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;
    background:rgba(255,255,255,.03);border:1px solid var(--line);font-size:12px;color:var(--mut);
  }
  .dot{width:8px;height:8px;border-radius:50%;background:var(--mut2)}
  .dot.on{background:var(--up);box-shadow:0 0 10px var(--up)}
  .spacer{margin-left:auto}
  .hstat{text-align:right}
  .hstat b{display:block;font-size:15px;font-variant-numeric:tabular-nums}
  .hstat span{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut2)}

  /* ---------- layout ---------- */
  .shell{max-width:1560px;margin:0 auto;padding:22px 26px 60px;
         display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:20px}
  @media(max-width:1120px){.shell{grid-template-columns:1fr}}
  .col{display:flex;flex-direction:column;gap:20px;min-width:0}
  .card{
    background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    box-shadow:var(--glow);backdrop-filter:blur(14px);overflow:hidden;
  }
  .card .hd{display:flex;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--line)}
  .card .hd h3{margin:0;font-size:12px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--mut)}
  .card .hd .tag{margin-left:auto;font-size:11px;color:var(--mut2)}
  .card .bd{padding:18px}

  /* ---------- controls ---------- */
  .controls{display:grid;grid-template-columns:1.3fr 1.1fr .9fr;gap:14px}
  @media(max-width:760px){.controls{grid-template-columns:1fr}}
  label.fld{display:block}
  label.fld>span{display:block;font-size:10.5px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--mut2);margin:0 0 7px 3px}
  select,button{width:100%;font-family:inherit}
  select{
    appearance:none;padding:12px 40px 12px 14px;border-radius:12px;border:1px solid var(--line2);
    background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.01));
    color:var(--txt);font-size:14px;cursor:pointer;transition:.15s;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M2 4l4 4 4-4' fill='none' stroke='%238794ab' stroke-width='1.6' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 14px center;
  }
  select:hover{border-color:var(--acc)}
  select:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 4px rgba(91,140,255,.16)}
  option{background:#0b1018;color:var(--txt)}
  .cta{
    margin-top:16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  }
  button.primary{
    flex:1;min-width:220px;padding:15px 22px;border:none;border-radius:13px;cursor:pointer;
    font-weight:800;font-size:15px;letter-spacing:.02em;color:#04070d;
    background:linear-gradient(135deg,#7fb0ff,#5b8cff 45%,#8b6cff);
    box-shadow:0 16px 40px -14px rgba(91,140,255,.85), inset 0 1px 0 rgba(255,255,255,.5);
    transition:.15s;position:relative;overflow:hidden;
  }
  button.primary:hover{transform:translateY(-1px);box-shadow:0 22px 50px -14px rgba(91,140,255,.95)}
  button.primary:active{transform:translateY(0)}
  button.primary:disabled{opacity:.55;cursor:default;transform:none}
  button.ghost{
    padding:14px 18px;border-radius:13px;cursor:pointer;font-weight:700;font-size:13.5px;
    background:rgba(255,255,255,.03);border:1px solid var(--line2);color:var(--txt);transition:.15s;
  }
  button.ghost:hover{border-color:var(--acc);color:#fff}
  .hint{margin-top:12px;font-size:12.5px;color:var(--mut)}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
  .chip{padding:6px 11px;border-radius:999px;font-size:11.5px;color:var(--mut);
        background:rgba(255,255,255,.03);border:1px solid var(--line)}
  .chip b{color:var(--txt);font-weight:600}

  /* ---------- signal hero ---------- */
  .hero{padding:22px;border-radius:var(--radius);position:relative;overflow:hidden;
        border:1px solid var(--line);background:linear-gradient(180deg,rgba(20,26,38,.85),rgba(12,16,26,.85))}
  .hero.up{box-shadow:inset 0 0 0 1px rgba(22,224,160,.35), 0 30px 80px -40px rgba(22,224,160,.6)}
  .hero.down{box-shadow:inset 0 0 0 1px rgba(255,92,114,.35), 0 30px 80px -40px rgba(255,92,114,.6)}
  .hero .row1{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
  .verdictBig{font-size:52px;font-weight:800;line-height:1;letter-spacing:-.02em}
  .hero.up .verdictBig{color:var(--up);text-shadow:0 0 34px rgba(22,224,160,.5)}
  .hero.down .verdictBig{color:var(--down);text-shadow:0 0 34px rgba(255,92,114,.5)}
  .hero.idle .verdictBig{color:var(--mut)}
  .arrow{font-size:34px;filter:drop-shadow(0 0 16px currentColor)}
  .hero.up .arrow{color:var(--up)} .hero.down .arrow{color:var(--down)} .hero.idle .arrow{color:var(--mut2)}
  .heroMeta{margin-left:auto;text-align:right}
  .heroMeta .pair{font-weight:700;font-size:16px}
  .heroMeta .tf{font-size:12px;color:var(--mut)}
  .conf{margin-top:18px}
  .conf .lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-bottom:6px;letter-spacing:.08em;text-transform:uppercase}
  .track{height:12px;border-radius:999px;background:rgba(255,255,255,.05);overflow:hidden;position:relative}
  .track i{display:block;height:100%;border-radius:999px;transition:width .5s cubic-bezier(.2,.8,.2,1)}
  .hero.up .track i{background:linear-gradient(90deg,#0e8f6a,var(--up))}
  .hero.down .track i{background:linear-gradient(90deg,#a2303f,var(--down))}
  .hero.idle .track i{background:var(--mut2)}
  .hero .verdict-line{margin-top:14px;font-size:13px;color:var(--mut)}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;letter-spacing:.04em}
  .badge.edge{background:var(--up-dim);color:var(--up);border:1px solid rgba(22,224,160,.3)}
  .badge.noedge{background:rgba(245,196,81,.12);color:var(--gold);border:1px solid rgba(245,196,81,.3)}
  .badge.bad{background:var(--down-dim);color:var(--down);border:1px solid rgba(255,92,114,.3)}

  /* ---------- metrics ---------- */
  .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:6px}
  @media(max-width:760px){.metrics{grid-template-columns:1fr 1fr}}
  .metric{background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:14px;padding:13px 14px}
  .metric span{display:block;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut2);margin-bottom:5px}
  .metric b{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}
  .metric small{display:block;color:var(--mut);font-size:11.5px;margin-top:3px}

  /* ---------- progress ---------- */
  .prog{margin-top:16px}
  .prog .bar{height:8px;border-radius:999px;background:rgba(255,255,255,.06);overflow:hidden}
  .prog .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));width:0;transition:width .4s;border-radius:999px}
  .prog .txt{margin-top:9px;font-size:12.5px;color:var(--mut);display:flex;align-items:center;gap:9px}
  .spin{width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.18);border-top-color:var(--acc);animation:sp .7s linear infinite;display:none}
  .prog.busy .spin{display:inline-block}
  @keyframes sp{to{transform:rotate(360deg)}}

  /* ---------- chart ---------- */
  canvas{width:100%;display:block}
  #chart{height:340px}
  .chartWrap{position:relative}
  .chartWrap .ov{position:absolute;top:12px;left:14px;font-size:11px;color:var(--mut);background:rgba(5,7,12,.6);padding:5px 10px;border-radius:8px;border:1px solid var(--line)}

  /* ---------- tables ---------- */
  table{width:100%;border-collapse:collapse;font-size:13px}
  td,th{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}
  th{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut2);font-weight:600}
  td:last-child,th:last-child{text-align:right}
  tr:last-child td{border-bottom:none}
  .k{color:var(--mut)}
  .imp{height:7px;border-radius:999px;background:linear-gradient(90deg,var(--acc),var(--acc2));display:inline-block;vertical-align:middle}
  .verdictCell{font-weight:700}
  .v-edge{color:var(--up)} .v-no{color:var(--gold)} .v-bad{color:var(--down)}

  /* ---------- tf rows (all mode) ---------- */
  .tfgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  @media(max-width:760px){.tfgrid{grid-template-columns:1fr}}
  .tfbox{border:1px solid var(--line);border-radius:14px;padding:13px;background:rgba(255,255,255,.02)}
  .tfbox .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
  .tfbox .name{font-weight:700}
  .tfbox .act{font-weight:800;font-size:15px}
  .tfbox.up .act{color:var(--up)} .tfbox.down .act{color:var(--down)}
  .tfbox .m{font-size:11.5px;color:var(--mut);font-variant-numeric:tabular-nums}

  /* ---------- scan ---------- */
  .sig{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;padding:11px 4px;border-bottom:1px solid var(--line)}
  .sig:last-child{border-bottom:none}
  .tag{font-weight:800;font-size:11px;padding:4px 9px;border-radius:8px;letter-spacing:.04em}
  .tag.CALL{background:var(--up-dim);color:var(--up)}
  .tag.PUT{background:var(--down-dim);color:var(--down)}
  .tag.NEUTRAL{background:rgba(255,255,255,.05);color:var(--mut)}
  .sig .m{font-variant-numeric:tabular-nums;color:var(--mut)}
  .sig .why{color:var(--mut2);font-size:11.5px;margin-top:3px}
  .scroll{max-height:440px;overflow:auto}
  .empty{padding:22px;text-align:center;color:var(--mut2);font-size:13px}
  .skeleton{height:12px;border-radius:6px;background:linear-gradient(90deg,rgba(255,255,255,.04),rgba(255,255,255,.09),rgba(255,255,255,.04));background-size:200% 100%;animation:sh 1.3s infinite}
  @keyframes sh{to{background-position:-200% 0}}
  footer{padding:26px;text-align:center;color:var(--mut2);font-size:12px}
  .fineprint{color:var(--mut2);font-size:11.5px;line-height:1.6}
</style></head><body>
<header class="top">
  <div class="brand">
    <div class="logo">◆</div>
    <div>QX Quant<small>AI Signal Terminal</small></div>
  </div>
  <span class="pill"><span class="dot" id="dot"></span><span id="feedState">connecting…</span></span>
  <span class="pill">pairs <b class="mono" id="nPairs" style="color:var(--txt)">–</b></span>
  <span class="spacer"></span>
  <div class="hstat"><b class="mono" id="hPrice">–</b><span>live price</span></div>
  <div class="hstat"><b class="mono" id="hTps">–</b><span>ticks / s</span></div>
  <div class="hstat"><b class="mono" id="hStored">–</b><span>candles</span></div>
</header>

<div class="shell">
  <!-- ============ LEFT ============ -->
  <div class="col">
    <div class="card">
      <div class="hd"><h3>Market selection</h3><span class="tag" id="srcTag">Yahoo Finance · historical</span></div>
      <div class="bd">
        <div class="controls">
          <label class="fld"><span>Market / pair</span><select id="pair"></select></label>
          <label class="fld"><span>Timeframe</span><select id="tf"></select></label>
          <label class="fld"><span>Live chart pair</span><select id="livePair"></select></label>
        </div>
        <div class="cta">
          <button class="primary" id="go">⚡ Get Analysis</button>
          <button class="ghost" id="retrain" title="Ignore the cache and retrain">↻ Retrain</button>
        </div>
        <div class="prog" id="prog">
          <div class="bar"><i id="progBar"></i></div>
          <div class="txt"><span class="spin"></span><span id="progTxt">pick a market and press Get Analysis</span></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="hd"><h3>ML signal</h3><span class="tag" id="mlAsOf"></span></div>
      <div class="bd">
        <div class="hero idle" id="hero">
          <div class="row1">
            <span class="arrow" id="arrow">◆</span>
            <div class="verdictBig" id="verdictBig">NO SIGNAL</div>
            <div class="heroMeta">
              <div class="pair" id="heroPair">—</div>
              <div class="tf" id="heroTf">choose a market</div>
            </div>
          </div>
          <div class="conf">
            <div class="lbl"><span>Model confidence</span><span id="confPct">—</span></div>
            <div class="track"><i id="confBar" style="width:0%"></i></div>
          </div>
          <div class="verdict-line" id="heroLine">The model trains on freshly downloaded history, then predicts the next candle's direction.</div>
        </div>

        <div class="metrics" id="metrics" style="margin-top:16px"></div>
        <div id="tfBreakdown"></div>
        <div id="signalHint" class="fineprint" style="margin-top:14px"></div>
      </div>
    </div>

    <div class="card">
      <div class="hd"><h3>Price action</h3><span class="tag" id="chartTag">live feed</span></div>
      <div class="bd chartWrap" style="padding:0">
        <div class="ov" id="chartOv">live market-qx candles</div>
        <canvas id="chart"></canvas>
      </div>
    </div>

    <div class="card">
      <div class="hd"><h3>Scan all pairs</h3>
        <span class="tag"><select id="scanTf" style="width:auto;padding:5px 26px 5px 8px;font-size:11.5px;border-radius:8px">
          <option value="5">5s</option><option value="15">15s</option><option value="60">1m</option>
          <option value="5,15,60">5s+15s+1m</option><option value="all">all</option>
        </select></span>
      </div>
      <div class="bd">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="ghost" id="scanGo" style="width:auto;flex:0 0 auto">Scan now</button>
          <span class="hint" id="scanHint" style="margin:0">runs the rule engine over every stored pair</span>
        </div>
        <div class="scroll" id="scanList" style="margin-top:12px"></div>
      </div>
    </div>
  </div>

  <!-- ============ RIGHT ============ -->
  <div class="col">
    <div class="card">
      <div class="hd"><h3>Model diagnostics</h3><span class="tag" id="diagTag">walk-forward</span></div>
      <div class="bd">
        <table id="diag"><tbody><tr><td class="k">status</td><td>waiting for a run</td></tr></tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="hd"><h3>Feature importance</h3><span class="tag">permutation</span></div>
      <div class="bd"><div id="impList" class="fineprint">run an analysis to see which features the model leans on</div></div>
    </div>

    <div class="card">
      <div class="hd"><h3>Data quality</h3><span class="tag">cleaning report</span></div>
      <div class="bd"><div id="quality" class="fineprint">no data downloaded yet</div></div>
    </div>

    <div class="card">
      <div class="hd"><h3>Demo → Real money gate</h3></div>
      <div class="bd">
        <div class="hero idle" id="glBanner" style="padding:16px">
          <div class="verdictBig" id="glStage" style="font-size:26px">—</div>
          <div class="verdict-line" id="glSub" style="margin-top:8px">checking…</div>
        </div>
        <div id="glChecks" class="fineprint" style="margin-top:12px"></div>
        <div id="glPaper" class="fineprint" style="margin-top:10px"></div>
      </div>
    </div>

    <div class="card">
      <div class="hd"><h3>Track record</h3><span class="tag">settled signals</span></div>
      <div class="bd">
        <div class="metrics" id="jStats" style="grid-template-columns:1fr 1fr"></div>
        <div id="jVerdict" class="fineprint" style="margin-top:12px"></div>
        <div id="jPairs" style="margin-top:8px"></div>
      </div>
    </div>
  </div>
</div>

<footer>
  QX Quant · market-qx live feed + Yahoo Finance history · signals are research alerts, not financial advice.
</footer>

<script>
let catalog=[], tfList=[], cur={pair:null,tf:null}, live={asset:null}, polling=false, autoScan=0, scanning=false, lastBundle=null;

function fmt(t){return new Date(t*1000).toLocaleTimeString();}
function clock(ts){return new Date(ts*1000).toLocaleTimeString();}
function cd(secs){if(secs==null)return '—';if(secs<=0)return 'now';if(secs<60)return 'in '+secs+'s';return 'in '+Math.floor(secs/60)+'m'+String(secs%60).padStart(2,'0')+'s';}
function pct(x){return (x*100).toFixed(1)+'%';}

async function boot(){
  const r=await fetch('/api/ml_catalog'); const j=await r.json();
  catalog=j.groups||[]; tfList=j.timeframes||[];
  const ps=document.getElementById('pair');
  catalog.forEach(g=>{
    const og=document.createElement('optgroup'); og.label=g.type.toUpperCase();
    g.symbols.forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s;og.appendChild(o);});
    ps.appendChild(og);
  });
  const tfs=document.getElementById('tf');
  tfList.forEach(t=>{const o=document.createElement('option');o.value=t.value;o.textContent=t.label;tfs.appendChild(o);});
  tfs.value='1h';
  ps.value=catalog.length&&catalog[0].symbols.includes('EURUSD')?'EURUSD':(catalog[0]?catalog[0].symbols[0]:'');
  document.getElementById('go').onclick=()=>analyze(false);
  document.getElementById('retrain').onclick=()=>analyze(true);
  document.getElementById('scanGo').onclick=scanAll;
  loadLivePairs();
  tick();
  scanAll();
}

async function loadLivePairs(){
  const sel=document.getElementById('livePair');
  for(let attempt=0;attempt<40;attempt++){
    try{
      const r=await fetch('/api/catalog'); const j=await r.json();
      if(j.loading||!(j.groups||[]).length){
        document.getElementById('nPairs').textContent='…';
        document.getElementById('feedState').textContent='loading instruments…';
        await sleep(2000); continue;
      }
      let n=0;
      (j.groups||[]).forEach(g=>g.symbols.forEach(s=>{n++;const o=document.createElement('option');o.value=s;o.textContent=s;sel.appendChild(o);}));
      document.getElementById('nPairs').textContent=n;
      sel.value=j.default_asset||sel.options[0]?.value;
      live.asset=sel.value;
      sel.onchange=()=>{live.asset=sel.value;drawLive();};
      document.getElementById('feedState').textContent='live feed online';
      document.getElementById('dot').className='dot on';
      return;
    }catch(e){await sleep(2000);}
  }
  document.getElementById('feedState').textContent='feed offline';
}

function setProgress(busy,text,p){
  const prog=document.getElementById('prog');
  prog.className='prog'+(busy?' busy':'');
  document.getElementById('progTxt').textContent=text;
  document.getElementById('progBar').style.width=(p*100)+'%';
}

async function analyze(force){
  const pair=document.getElementById('pair').value;
  const tf=document.getElementById('tf').value;
  if(!pair)return;
  cur={pair,tf};
  document.getElementById('go').disabled=true;
  document.getElementById('retrain').disabled=true;
  document.getElementById('heroPair').textContent=pair+' · '+tf;
  setProgress(true,'starting…',0.02);
  try{
    const r=await fetch('/api/ml_train?pair='+encodeURIComponent(pair)+'&tf='+encodeURIComponent(tf)+(force?'&force=1':''));
    const j=await r.json();
    if(j.error){setProgress(false,'error: '+j.error,0);document.getElementById('heroLine').textContent=j.error;return;}
    pollJob(j.job_id);
  }catch(e){
    setProgress(false,'request failed — retrying is safe',0);
  }finally{
    document.getElementById('retrain').disabled=false;
  }
}

async function pollJob(id){
  polling=true;
  while(polling){
    let s;
    try{const r=await fetch('/api/ml_job?id='+id);s=await r.json();}catch(e){await sleep(1200);continue;}
    setProgress(true,s.step||'training…',s.progress||0);
    if(s.state==='done'){polling=false;document.getElementById('go').disabled=false;setProgress(false,'done · '+s.step,1);render(s.result);break;}
    if(s.state==='error'){polling=false;document.getElementById('go').disabled=false;setProgress(false,'error',0);document.getElementById('heroLine').textContent=s.error||'training failed';break;}
    await sleep(900);
  }
}
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}

function render(B){
  if(!B)return;
  lastBundle=B;
  const s=B.signal||{};
  const up=s.action==='UP', down=s.action==='DOWN';
  const hero=document.getElementById('hero');
  hero.className='hero '+(up?'up':down?'down':'idle');
  document.getElementById('arrow').textContent=up?'▲':down?'▼':'◆';
  document.getElementById('verdictBig').textContent=s.action||'NO SIGNAL';
  document.getElementById('heroPair').textContent=B.pair+' · '+(B.tf==='all'?('all · '+B.tfs.join('/')):B.tf);
  document.getElementById('heroTf').textContent=B.symbol||'';
  const conf=s.confidence||0;
  document.getElementById('confPct').textContent=pct(conf);
  document.getElementById('confBar').style.width=Math.max(3,Math.min(100,conf*100))+'%';
  document.getElementById('mlAsOf').textContent='trained '+B.trained_on;
  const v=B.verdict||'';
  const vc=v.includes('EDGE DETECTED')?'edge':(v.includes('BELOW')?'bad':'noedge');
  document.getElementById('heroLine').innerHTML=
    'Next-candle call <b>'+(s.action||'—')+'</b> at '+pct(conf)+' confidence · '+
    '<span class="badge '+vc+'">'+v+'</span>'+
    (s.agree?(' · agreement '+s.agree):'');

  renderMetrics(B);
  renderBreakdown(B);
  renderDiagnostics(B);
  renderImportance(B);
  renderQuality(B);
  drawBundleChart(B);
  document.getElementById('signalHint').innerHTML=
    'Honest note: this feed behaves close to a random walk. A confidence near 50% and a '+
    'walk-forward edge near zero is the expected, truthful result — the badge above reports it. '+
    'Never treat a signal as a guaranteed trade.';
}

function renderMetrics(B){
  const v=B.validation||{};
  const q=B.quality||{};
  const items=[
    ['Accuracy', v.mean_acc!=null?pct(v.mean_acc):'—', 'walk-forward out-of-sample'],
    ['Baseline', v.mean_baseline!=null?pct(v.mean_baseline):'—', 'majority-class guess'],
    ['Edge', v.mean_edge!=null?((v.mean_edge>=0?'+':'')+pct(v.mean_edge)):'—', 'accuracy − baseline'],
    ['AUC', v.mean_auc!=null?v.mean_auc.toFixed(3):'—', 'ranking quality (0.5 = coin)'],
    ['Samples', (B.samples||0).toLocaleString(), (B.features||0)+' features'],
    ['Bars', (B.bars||0).toLocaleString(), 'history length'],
    ['Quality', q.score!=null?q.score.toFixed(2):'—', q.ok?'passed cleaning':'failed cleaning'],
    ['Up-rate', B.up_rate!=null?pct(B.up_rate):'—', 'label balance'],
  ];
  document.getElementById('metrics').innerHTML=items.map(([k,val,sub])=>
    '<div class="metric"><span>'+k+'</span><b>'+val+'</b><small>'+sub+'</small></div>').join('');
}

function renderBreakdown(B){
  const el=document.getElementById('tfBreakdown');
  if(B.tf!=='all'||!B.per_timeframe){el.innerHTML='';return;}
  el.innerHTML='<div class="tfgrid" style="margin-top:16px">'+B.per_timeframe.map(b=>{
    const s=b.signal, v=b.validation||{};
    const up=s.action==='UP';
    return '<div class="tfbox '+(up?'up':'down')+'">'
      +'<div class="top"><span class="name">'+b.tf+'</span><span class="act">'+s.action+'</span></div>'
      +'<div class="m">conf '+pct(s.confidence)+'</div>'
      +'<div class="m">edge '+(v.mean_edge!=null?((v.mean_edge>=0?'+':'')+pct(v.mean_edge)):'—')+'</div>'
      +'<div class="m">acc '+(v.mean_acc!=null?pct(v.mean_acc):'—')+'</div>'
      +'</div>';
  }).join('')+'</div>';
}

function renderDiagnostics(B){
  const v=B.validation||{};
  const rows=[
    ['verdict', '<span class="verdictCell '+(B.verdict==='EDGE DETECTED'?'v-edge':B.verdict.includes('BELOW')?'v-bad':'v-no')+'">'+B.verdict+'</span>'],
    ['folds', v.n_folds||0],
    ['mean accuracy', v.mean_acc!=null?pct(v.mean_acc):'—'],
    ['mean baseline', v.mean_baseline!=null?pct(v.mean_baseline):'—'],
    ['mean edge', v.mean_edge!=null?((v.mean_edge>=0?'+':'')+pct(v.mean_edge)):'—'],
    ['mean AUC', v.mean_auc!=null?v.mean_auc.toFixed(4):'—'],
    ['trained on', B.samples?B.samples.toLocaleString()+' rows':'—'],
  ];
  let html='<tbody>'+rows.map(([k,val])=>'<tr><td class="k">'+k+'</td><td>'+val+'</td></tr>').join('');
  if((v.folds||[]).length){
    html+='<tr><td class="k" colspan="2" style="padding-top:14px"><b style="color:var(--mut)">per fold</b></td></tr>';
    html+=v.folds.map(f=>'<tr><td class="k">fold '+f.fold+' <span style="color:var(--mut2)">(n='+f.n_test+')</span></td>'
      +'<td class="mono">'+pct(f.acc)+' <span class="k">· edge '+(f.edge>=0?'+':'')+pct(f.edge)+'</span></td></tr>').join('');
  }
  html+='</tbody>';
  document.getElementById('diag').innerHTML=html;
}

function renderImportance(B){
  const imp=B.importances||[];
  if(!imp.length){document.getElementById('impList').textContent='no importance data';return;}
  const max=Math.max(...imp.map(d=>Math.abs(d.drop)))||1;
  document.getElementById('impList').innerHTML=imp.map(d=>
    '<div style="display:flex;align-items:center;gap:10px;margin:7px 0">'
    +'<span style="flex:0 0 130px;color:var(--mut)">'+d.feature.replace(/_/g,' ')+'</span>'
    +'<span class="imp" style="width:'+Math.max(3,Math.abs(d.drop)/max*100)+'%"></span>'
    +'<span class="mono" style="margin-left:auto">'+d.drop.toFixed(3)+'</span></div>').join('');
}

function renderQuality(B){
  const q=B.quality||{};
  const items=[
    ['bars kept', q.bars],['dropped', q.dropped],
    ['flat bars', pct(q.flat_ratio||0)],['gaps', pct(q.gap_ratio||0)],
    ['spikes', pct(q.spike_ratio||0)],['open==close', pct(q.oc_equal_ratio||0)],
    ['score', (q.score||0).toFixed(3)],['passed', q.ok?'yes':'no'],
  ];
  let html='<div class="metrics" style="grid-template-columns:1fr 1fr;margin-top:0">'
    +items.map(([k,val])=>'<div class="metric"><span>'+k+'</span><b>'+val+'</b></div>').join('')+'</div>';
  if((q.notes||[]).length) html+='<div style="margin-top:10px;color:var(--gold)">⚠ '+q.notes.join(' · ')+'</div>';
  document.getElementById('quality').innerHTML=html;
}

function drawBundleChart(B){
  const cs=B.recent||[];
  document.getElementById('chartTag').textContent=(B.pair||'')+' '+(B.tf==='all'?'daily':B.tf);
  document.getElementById('chartOv').textContent=(B.pair||'')+' · '+(B.tf==='all'?'1d':B.tf)+' history';
  drawCandles(cs);
}

async function scanAll(){
  if(scanning)return; scanning=true;
  const tfs=document.getElementById('scanTf').value;
  const btn=document.getElementById('scanGo'); btn.disabled=true;
  document.getElementById('scanHint').textContent='scanning every pair…';
  const t0=performance.now();
  try{
    const r=await fetch('/api/scan?periods='+encodeURIComponent(tfs)+'&engine=rule');
    const j=await r.json();
    const ms=Math.round(performance.now()-t0);
    const scope=tfs==='all'?'all timeframes':tfs.replace(/,/g,'s, ')+'s';
    document.getElementById('scanHint').textContent=j.scanned+' series · '+j.ready+' ready · '+j.signal_count+' signals · '+ms+' ms';
    const el=document.getElementById('scanList');
    if(!j.signals.length){el.innerHTML='<div class="empty">no signal right now — most candles are noise</div>';return;}
    el.innerHTML=j.signals.map(s=>{
      const strength='score '+s.score+' · bull '+s.bull+' / bear '+s.bear;
      const why=(s.reasons||[]).slice(0,2).join(' · ');
      const when=(s.entry_at&&s.expiry_at)
        ?'entry <b style="color:var(--acc)">'+clock(s.entry_at)+'</b> <span class="k" data-in="'+s.entry_at+'">('+cd(s.entry_in)+')</span>'
          +' → expiry <b style="color:var(--acc)">'+clock(s.expiry_at)+'</b> <span class="k" data-in="'+s.expiry_at+'">('+cd(s.expires_in)+')</span>'
        :'';
      return '<div class="sig"><span class="tag '+s.action+'">'+s.action+'</span>'
        +'<span>'+s.asset+' <span class="k">'+s.label+'</span>'
        +'<div class="why">'+why+'</div>'
        +(when?('<div class="why">'+when+'</div>'):'')
        +'</span><span class="m">'+strength+'</span></div>';
    }).join('');
  }catch(e){document.getElementById('scanHint').textContent='scan failed — retrying';}
  finally{btn.disabled=false;scanning=false;}
}

async function tick(){
  try{
    const a=live.asset;
    const r=await fetch('/api/state?asset='+encodeURIComponent(a||'')+'&period=60');
    const s=await r.json();
    document.getElementById('dot').className='dot'+(s.connected?' on':'');
    document.getElementById('feedState').textContent=s.connected?'live feed online':'connecting…';
    document.getElementById('hTps').textContent=s.tps!=null?s.tps:'–';
    document.getElementById('hStored').textContent=(s.stored||0).toLocaleString();
    if(a&&s.prices&&s.prices[a]!=null)document.getElementById('hPrice').textContent=s.prices[a];
    if(a)drawLive(s);
    autoScan++; if(autoScan>=14){autoScan=0; if(!scanning)scanAll();}
  }catch(e){document.getElementById('dot').className='dot';}
  setTimeout(tick,1800);
}

function drawLive(s){
  if(lastBundle)return; // ML chart takes precedence
  const a=live.asset,p=60;
  const cs=(s.series||{})[a+'|'+p]||[];
  if(cs.length<2)return;
  document.getElementById('chartTag').textContent=a+' 1m (live)';
  document.getElementById('chartOv').textContent=a+' · live market-qx feed';
  drawCandles(cs);
}

function drawCandles(cs){
  const cv=document.getElementById('chart');
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth, H=cv.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr;
  const g=cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  if(!cs||cs.length<2){g.fillStyle='#5d6a80';g.font='13px Inter,sans-serif';g.fillText('collecting candles…',18,H/2);return;}
  cs=cs.slice(-120);
  const pad=14, plotW=W-pad*2, plotH=H-pad*2;
  let hi=Math.max(...cs.map(c=>c[3])), lo=Math.min(...cs.map(c=>c[4]));
  const rng=(hi-lo)||1; hi+=rng*0.06; lo-=rng*0.06;
  const y=v=>pad+plotH*(1-(v-lo)/(hi-lo)), cw=plotW/cs.length;
  g.strokeStyle='rgba(120,140,180,.1)'; g.lineWidth=1;
  for(let i=0;i<=4;i++){const yy=pad+plotH*i/4;g.beginPath();g.moveTo(pad,yy);g.lineTo(W-pad,yy);g.stroke();}
  g.fillStyle='#5d6a80'; g.font='11px JetBrains Mono,monospace';
  g.fillText(hi.toFixed(5),pad+3,pad+11); g.fillText(lo.toFixed(5),pad+3,H-pad-3);
  cs.forEach((c,i)=>{
    const up=c[2]>=c[1], col=up?'#16e0a0':'#ff5c72';
    const x=pad+cw*i+cw/2, bw=Math.max(1,cw*0.6);
    g.strokeStyle=col; g.fillStyle=col; g.lineWidth=1;
    g.beginPath(); g.moveTo(x,y(c[3])); g.lineTo(x,y(c[4])); g.stroke();
    const yo=y(c[1]), yc=y(c[2]);
    g.fillRect(x-bw/2, Math.min(yo,yc), bw, Math.max(1,Math.abs(yc-yo)));
  });
}

function tickCountdowns(){
  const now=Math.floor(Date.now()/1000);
  document.querySelectorAll('[data-in]').forEach(el=>{
    const t=parseInt(el.getAttribute('data-in')); el.textContent='('+cd(t-now)+')';
  });
}
setInterval(tickCountdowns,1000);

async function loadGate(){
  try{
    const r=await fetch('/api/golive'); const g=await r.json();
    const icon={DEMO:'⏳',"NOT READY":'🔴',"PAPER PASS":'🟡',"LIVE-READY":'🟢'};
    const banner=document.getElementById('glBanner');
    banner.className='hero '+(g.stage==='LIVE-READY'?'up':g.stage==='NOT READY'?'down':'idle');
    document.getElementById('glStage').textContent=(icon[g.stage]||'')+' '+g.stage;
    const msgs={DEMO:'Still collecting on demo — not enough settled signals to judge.',
      "NOT READY":'Stay on demo. No measured edge — do not use real money.',
      "PAPER PASS":'Promising, but keep testing on demo before risking money.',
      "LIVE-READY":'Gate passed. A SMALL real stake is defensible; scale up slowly.'};
    document.getElementById('glSub').textContent=msgs[g.stage]||'';
    document.getElementById('glChecks').innerHTML=(g.checks||[]).map(c=>
      '<div style="margin:3px 0">'+(c.pass?'✅':'⬜')+' '+c.name+' — <span class="k">'+c.detail+'</span></div>').join('');
    const p=g.paper||{};
    if(p.trades!==undefined){
      document.getElementById('glPaper').textContent=
        'paper: '+(p.start||1000).toFixed(0)+' → '+p.final+' ('+(p.return_pct>=0?'+':'')+p.return_pct+'%), max DD '+p.max_drawdown_pct+'%, '+p.trades+' trades';
    }
  }catch(e){}
}
setInterval(loadGate,15000);

async function loadJournal(){
  try{
    const r=await fetch('/api/journal'); const j=await r.json();
    const o=j.overall||{n:0,hit:0}, d=j.decisive||{n:0,hit:0};
    document.getElementById('jStats').innerHTML=
      mi('settled',j.settled||0)+mi('win / loss',(j.wins||0)+' / '+(j.losses||0))+
      mi('hit-rate',pct(o.hit||0))+mi('decisive',pct(d.hit||0));
    let v='';
    if(!j.settled)v='No signals settled yet — the bot needs time to log and settle them.';
    else if(j.verdict==='above')v='🟢 Hit-rate is statistically ABOVE the payout breakeven.';
    else if(j.verdict==='below')v='🔴 Hit-rate is statistically BELOW the payout breakeven — trading this loses money.';
    else v='⚪ Not yet distinguishable from breakeven (needs many more settled signals).';
    if(j.settled&&o.n)v+=' 95% CI ['+pct(o.lo)+', '+pct(o.hi)+'] on n='+o.n+'.';
    document.getElementById('jVerdict').textContent=v;
    const pairs=(j.pairs||[]).filter(p=>p.n>=10).slice(0,10);
    document.getElementById('jPairs').innerHTML=pairs.length?pairs.map(p=>
      '<div class="fineprint">'+p.asset+' '+(p.period>=60?(p.period/60)+'m':p.period+'s')+' — '+p.wins+'/'+p.n+' ('+pct(p.hit)+')</div>').join('')
      :'<div class="fineprint">no pair has 10+ settled signals yet</div>';
  }catch(e){}
}
function mi(k,v){return '<div class="metric"><span>'+k+'</span><b>'+v+'</b></div>';}
setInterval(loadJournal,10000);

boot(); loadGate(); loadJournal();
window.addEventListener('resize',()=>{if(lastBundle)drawBundleChart(lastBundle);});
</script></body></html>
"""


def _ml_catalog() -> dict:
    """Instruments + timeframes the ML engine can train on (Yahoo history)."""
    import market_data
    order = ["5m", "15m", "30m", "1h", "1d", "1wk"]
    labels = {"5m": "5 min", "15m": "15 min", "30m": "30 min",
              "1h": "1 hour", "1d": "1 day", "1wk": "1 week"}
    tfs = [{"value": t, "label": labels[t]} for t in order if t in market_data.TIMEFRAME_PLAN]
    tfs.append({"value": market_data.ALL_TF, "label": "All timeframes (consensus)"})
    return {"groups": market_data.catalog(), "timeframes": tfs}


class Hub:
    """Shared live state behind the HTTP handlers."""

    def __init__(self, assets, periods, store, wait_instruments=75.0, journal=None):
        self.store = store
        self.periods = list(periods)
        self.assets = list(assets)
        self.lock = threading.Lock()
        self._tick_count = 0
        self._last_n = 0
        self._tps = 0.0
        self.feed: Optional[MultiFeed] = None
        self.instruments: dict[str, dict] = {}
        self.ready = threading.Event()
        self.error = ""
        self._wait = wait_instruments
        self.journal = journal
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        try:
            self.feed = MultiFeed(self.assets, self.periods, self.store,
                                  on_closed=self._on_closed, on_tick=self._on_tick,
                                  wait_instruments=self._wait)
            self.assets = self.feed.assets
            self.instruments = self.feed.instruments
            self.ready.set()
            threading.Thread(target=self._tps_loop, daemon=True).start()
            if self.journal is not None:
                threading.Thread(target=self._journal_loop, daemon=True).start()
        except Exception as exc:  # surface, don't hang the server
            self.error = f"{type(exc).__name__}: {exc}"
            print("feed boot failed:", self.error, flush=True)

    def _journal_loop(self):
        """Record live signals and settle expired ones, in the background."""
        from scan import scan_all
        while True:
            time.sleep(30)
            try:
                now = int(time.time())
                settle_open(self.journal, self.store, now)
                for engine in ("rule", "ensemble"):
                    rep = scan_all(self.store, self.periods, engine, top=0)
                    for row in rep["signals"]:
                        cs = self.store.load(row["asset"], row["period"], limit=2)
                        row["price"] = cs[-1][2] if cs else 0.0
                        self.journal.add(row, engine, now)
            except Exception as exc:  # never let the journal kill the feed
                print("journal loop error:", exc, flush=True)

    def _on_closed(self, asset, period, candle):
        pass  # hook for future alerts

    def _on_tick(self, asset, ts, price):
        with self.lock:
            self._tick_count += 1

    def _tps_loop(self):
        while True:
            time.sleep(5)
            with self.lock:
                n = self._tick_count
            self._tps = (n - self._last_n) / 5.0
            self._last_n = n

    def catalog(self):
        if not self.ready.is_set():
            return {"groups": [], "timeframes": [{"seconds": s, "label": l} for s, l in TIMEFRAMES],
                    "default_asset": "", "default_period": self.periods[0], "loading": True}
        return {"groups": asset_catalog(self.instruments, self.assets),
                "timeframes": [{"seconds": s, "label": l} for s, l in TIMEFRAMES],
                "default_asset": self._default_asset(), "default_period": self.periods[0],
                "loading": False}

    def _default_asset(self):
        for want in ("EURUSD_otc", "EURUSD"):
            if want in self.assets:
                return want
        return self.assets[0] if self.assets else ""

    def state(self, series_asset=None, series_period=None):
        with self.lock:
            tps = round(self._tps, 1)
        if not self.ready.is_set():
            return {"now": time.time(), "connected": False, "loading": True,
                    "error": self.error, "assets": len(self.assets),
                    "timeframes": len(self.periods), "stored": self.store.total,
                    "tps": 0.0, "prices": {}, "candle_counts": {},
                    "series": {}, "forming": {}}
        prices = dict(self.feed.last_price)
        counts, series, forming = {}, {}, {}
        if series_asset is not None:
            key = f"{series_asset}|{series_period}"
            cs = self.feed.candles(series_asset, series_period)
            counts[key] = len(cs)
            series[key] = cs[-70:]
            forming[key] = self.feed.forming(series_asset, series_period)
        return {"now": time.time(), "connected": self.feed.connected, "loading": False,
                "assets": len(self.assets), "timeframes": len(self.periods),
                "stored": self.store.total, "tps": tps, "prices": prices,
                "candle_counts": counts, "series": series, "forming": forming}


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/ml_catalog":
            return self._json(_ml_catalog())
        if u.path == "/api/ml_train":
            q = parse_qs(u.query)
            pair = (q.get("pair") or [""])[0]
            tf = (q.get("tf") or ["1h"])[0]
            force = (q.get("force") or ["0"])[0] not in ("0", "", "false")
            if not pair:
                return self._json({"error": "pair is required"}, 400)
            try:
                import ml_engine
                jid = ml_engine.start_job(pair, tf, force=force)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return self._json({"job_id": jid, "pair": pair, "tf": tf})
        if u.path == "/api/ml_job":
            q = parse_qs(u.query)
            jid = (q.get("id") or [""])[0]
            import ml_engine
            st = ml_engine.job_status(jid)
            if st is None:
                return self._json({"error": "unknown job"}, 404)
            return self._json(st)
        if u.path == "/api/scan":
            q = parse_qs(u.query)
            spec = (q.get("periods") or ["5"])[0]
            engine = (q.get("engine") or ["rule"])[0]
            try:
                top = int((q.get("top") or ["60"])[0])
            except ValueError:
                top = 60
            from scan import parse_periods, scan_all
            rep = scan_all(self.hub.store, parse_periods(spec), engine, top=top)
            return self._json(rep)
        if u.path == "/api/golive":
            q = parse_qs(u.query)
            try:
                payout = float((q.get("payout") or ["0.85"])[0])
            except ValueError:
                payout = 0.85
            try:
                min_n = int((q.get("min_n") or ["300"])[0])
            except ValueError:
                min_n = 300
            if self.hub.journal is None:
                return self._json({"stage": "disabled", "checks": [], "paper": {}})
            from golive import gate
            return self._json(gate(self.hub.journal, payout, min_n))
        if u.path == "/api/journal":
            q = parse_qs(u.query)
            asset = (q.get("asset") or [""])[0]
            engine = (q.get("engine") or ["rule"])[0]
            try:
                payout = float((q.get("payout") or ["0.85"])[0])
            except ValueError:
                payout = 0.85
            if self.hub.journal is None:
                return self._json({"logged": 0, "settled": 0, "open": 0,
                                   "verdict": "disabled", "pairs": []})
            return self._json(journal_summary(self.hub.journal, asset, engine, payout))
        if u.path == "/api/catalog":
            return self._json(self.hub.catalog())
        if u.path == "/api/analyze":
            q = parse_qs(u.query)
            default = self.hub.assets[0] if self.hub.assets else ""
            asset = (q.get("asset") or [default])[0]
            try:
                period = int((q.get("period") or [self.hub.periods[0]])[0])
            except ValueError:
                period = self.hub.periods[0]
            candles = self.hub.feed.candles(asset, period) if self.hub.feed else []
            engine = (q.get("engine") or ["rule"])[0]
            from scan import entry_plan
            try:
                expiry = int((q.get("expiry") or ["1"])[0])
            except ValueError:
                expiry = 1
            res = (analyze_smart(asset, period, candles) if engine == "smart"
                   else analyze_ensemble(asset, period, candles) if engine == "ensemble"
                   else analyze(asset, period, candles))
            out = res.__dict__
            if res.enough and candles:
                out.update(entry_plan(period, candles[-1][0], time.time(), expiry))
            return self._json(out)
        if u.path == "/api/intel":
            q = parse_qs(u.query)
            default = self.hub.assets[0] if self.hub.assets else ""
            asset = (q.get("asset") or [default])[0]
            try:
                period = int((q.get("period") or [self.hub.periods[0]])[0])
            except ValueError:
                period = self.hub.periods[0]
            try:
                payout = float((q.get("payout") or [str(DEFAULT_PAYOUT)])[0])
            except ValueError:
                payout = DEFAULT_PAYOUT
            candles = self.hub.feed.candles(asset, period) if self.hub.feed else []
            if not candles:
                candles = self.hub.store.load(asset, period, limit=400)
            from intelligence import analyse as intel_analyse
            from decision import decide
            from provenance import classify_source
            intel = intel_analyse(candles, len(candles) - 1, asset, period)
            if intel is None:
                return self._json({"asset": asset, "period": period,
                                   "error": "not enough candles"})
            out = intel.as_dict()
            decision = decide(
                intel.votes, asset=asset, period=period,
                source=classify_source(asset), now=int(candles[-1][0]),
                payout=payout,
                regime_defined=bool(intel.regime.get("defined", True)))
            out["decision"] = decision.as_dict()
            return self._json(out)
        if u.path == "/api/state":
            q = parse_qs(u.query)
            sa = (q.get("asset") or [None])[0]
            try:
                sp = int((q.get("period") or [0])[0]) or None
            except ValueError:
                sp = None
            return self._json(self.hub.state(sa, sp))
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Multi-pair signal dashboard")
    ap.add_argument("--assets", default="", help="comma list; default = all instruments")
    ap.add_argument("--periods", default="", help="comma list of seconds; default = all")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 12000)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default=os.environ.get("CANDLE_DB", "data/candles.db"))
    ap.add_argument("--journal", default=os.environ.get("JOURNAL_DB", "data/journal.db"),
                    help="signal journal database ('' to disable)")
    args = ap.parse_args()

    store = CandleStore(args.db)
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    periods = [int(p) for p in args.periods.split(",") if p.strip()] or PERIODS

    journal = Journal(args.journal) if args.journal else None
    hub = Hub(assets, periods, store, journal=journal)
    Handler.hub = hub
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}  "
          f"({'all' if not assets else len(assets)} pairs x {len(periods)} timeframes)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
