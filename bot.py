import os, time, threading
from datetime import datetime, timezone
from collections import deque

import requests
from flask import Flask, jsonify, render_template_string

PORT = int(os.getenv("PORT", "10000"))
API_KEY = os.getenv("BDT_API_KEY", "").strip()
PAIR_URL = "https://bangladeshitrader.com/chart/api/pairs"
CANDLE_URL = "https://bangladeshitrader.com/chart/Qx.php"
TIMEFRAME = "M1"
CANDLE_COUNT = 120
REQUEST_INTERVAL = float(os.getenv("REQUEST_INTERVAL", "2.1"))  # <= 30 requests/min trial
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "0"))  # 0 = all available Quotex OTC pairs
MIN_PAYOUT = int(os.getenv("MIN_PAYOUT", "0"))
TIMEOUT = 18

app = Flask(__name__)
http = requests.Session()
http.headers.update({"User-Agent": "Quotex-M1-Market-Scanner/1.0"})
lock = threading.RLock()
state = {
    "status": "Starting", "message": "Loading Quotex OTC pairs...",
    "pairs_total": 0, "scanned": 0, "api_errors": 0, "cycle": 0,
    "last_scan": None, "results": [], "history": deque(maxlen=80)
}

def get_pairs():
    r = http.get(PAIR_URL, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    rows = payload if isinstance(payload, list) else (
        payload.get("data") or payload.get("pairs") or payload.get("markets") or []
    )
    found, seen = [], set()
    for x in rows:
        if isinstance(x, str):
            name, payout, opened, broker = x, 0, True, "quotex"
        elif isinstance(x, dict):
            name = str(x.get("pair") or x.get("symbol") or x.get("name") or x.get("asset") or x.get("ticker") or "")
            payout = x.get("payout", 0) or 0
            opened = x.get("is_open", x.get("open", x.get("available", True)))
            broker = str(x.get("broker", x.get("platform", "quotex"))).lower().replace(" ", "_")
        else:
            continue
        if not name or "_otc" not in name.lower() or name in seen:
            continue
        if broker not in ("quotex", "qx", ""):
            continue
        if isinstance(opened, str):
            opened = opened.lower() in ("1", "true", "yes", "open", "active")
        if not opened:
            continue
        try:
            payout = int(float(payout))
        except (TypeError, ValueError):
            payout = 0
        if payout < MIN_PAYOUT:
            continue
        found.append({"pair": name, "payout": payout})
        seen.add(name)
    return found

def get_candles(pair):
    params = {"pair": pair, "timeframe": TIMEFRAME, "count": CANDLE_COUNT}
    r = http.get(CANDLE_URL, params=params, headers={"X-API-Key": API_KEY}, timeout=TIMEOUT)
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMITED: wait before next API requests")
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(str(j.get("code") or j.get("error") or "API success=false"))
    candles = []
    for c in j.get("data", []):
        try:
            candles.append({
                "time": c.get("time", ""),
                "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"])
            })
        except (KeyError, TypeError, ValueError):
            pass
    candles.reverse()  # API sends newest candle first
    return candles

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = v * k + value * (1-k)
    return value

def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(v, 0) for v in changes]
    losses = [max(-v, 0) for v in changes]
    ag, al = sum(gains[:period])/period, sum(losses[:period])/period
    for i in range(period, len(changes)):
        ag = (ag*(period-1)+gains[i])/period
        al = (al*(period-1)+losses[i])/period
    return 100.0 if al == 0 else 100 - 100/(1+ag/al)

def analyze(candles):
    if len(candles) < 35:
        return None
    closes = [c["close"] for c in candles]
    e9, e21, rv = ema(closes, 9), ema(closes, 21), rsi(closes)
    macd_values = []
    for n in range(26, len(closes)+1):
        v = closes[:n]
        macd_values.append(ema(v, 12)-ema(v, 26))
    macd = macd_values[-1]
    macd_signal = ema(macd_values, 9) if len(macd_values) >= 9 else None
    score, reasons = 0, []
    if e9 > e21:
        score += 1; reasons.append("EMA9 > EMA21")
    elif e9 < e21:
        score -= 1; reasons.append("EMA9 < EMA21")
    if macd_signal is not None:
        if macd > macd_signal:
            score += 1; reasons.append("MACD bullish")
        elif macd < macd_signal:
            score -= 1; reasons.append("MACD bearish")
    if rv is not None:
        if 52 <= rv <= 68:
            score += 1; reasons.append("RSI bullish")
        elif 32 <= rv <= 48:
            score -= 1; reasons.append("RSI bearish")
    last = candles[-1]
    span = max(last["high"]-last["low"], 1e-12)
    body = last["close"]-last["open"]
    if abs(body)/span >= 0.55:
        if body > 0:
            score += 1; reasons.append("Strong green candle")
        elif body < 0:
            score -= 1; reasons.append("Strong red candle")
    signal = "UP" if score >= 2 else "DOWN" if score <= -2 else "WAIT"
    confidence = min(85, 45+abs(score)*10) if signal != "WAIT" else 35+abs(score)*8
    return {
        "signal": signal, "confidence": confidence, "score": score,
        "rsi": round(rv, 2) if rv is not None else None,
        "price": last["close"], "candle_time": last["time"],
        "reasons": reasons
    }

def scanner():
    while True:
        if not API_KEY:
            with lock:
                state["status"] = "Missing API key"
                state["message"] = "Render Environment Variable BDT_API_KEY is not set."
            time.sleep(10); continue
        try:
            pairs = get_pairs()
            if MAX_PAIRS > 0:
                pairs = pairs[:MAX_PAIRS]
            with lock:
                state["pairs_total"] = len(pairs)
                state["status"] = "Scanning" if pairs else "No OTC pairs"
                state["message"] = f"{len(pairs)} open Quotex OTC pairs found."
            if not pairs:
                time.sleep(30); continue
            for item in pairs:
                try:
                    candles = get_candles(item["pair"])
                    result = analyze(candles)
                    if result:
                        row = {"pair": item["pair"], "payout": item["payout"], **result,
                               "updated": datetime.now(timezone.utc).isoformat()}
                        with lock:
                            latest = {x["pair"]: x for x in state["results"]}
                            latest[item["pair"]] = row
                            state["results"] = sorted(latest.values(),
                                key=lambda x: (x["signal"] != "WAIT", x["confidence"]), reverse=True)
                            state["history"].appendleft({
                                "pair": item["pair"], "signal": result["signal"],
                                "confidence": result["confidence"],
                                "time": datetime.now().strftime("%H:%M:%S")
                            })
                            state["scanned"] += 1
                            state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    with lock:
                        state["api_errors"] += 1
                        state["message"] = f"{item['pair']}: {str(e)[:160]}"
                    if "RATE_LIMITED" in str(e):
                        time.sleep(60); break
                time.sleep(max(2.05, REQUEST_INTERVAL))
            with lock:
                state["cycle"] += 1
                state["status"] = "Cycle complete"
                state["message"] = "Technical estimates only; no signal is guaranteed."
        except Exception as e:
            with lock:
                state["status"] = "API error"
                state["message"] = str(e)[:220]
                state["api_errors"] += 1
            time.sleep(15)

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quotex AI Market Scanner</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#081321;color:#eaf2ff;font:15px Arial}
main{max-width:1100px;margin:auto;padding:16px}.hero,.panel,.stat{background:#10233b;border:1px solid #254363;border-radius:15px;padding:16px;margin-bottom:13px}
h1{font-size:24px;margin:0 0 8px}.muted,.small{color:#a6bad2}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
.stat b{display:block;font-size:23px;margin-top:6px}table{width:100%;border-collapse:collapse;min-width:680px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #29405c;font-size:13px}
.wrap{overflow:auto}.up{color:#35e6a0;font-weight:bold}.down{color:#ff778a;font-weight:bold}.wait{color:#ffd36a;font-weight:bold}
@media(max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}main{padding:9px}}
</style></head><body><main>
<div class="hero"><h1>⚡ Quotex OTC Live Market Scanner</h1>
<div class="muted">M1 candles · RSI · EMA · MACD · Candle momentum</div>
<p id="status">Connecting...</p><div class="small" id="message"></div></div>
<div class="stats">
<div class="stat">OTC pairs<b id="pairs">0</b></div><div class="stat">Scanned<b id="scanned">0</b></div>
<div class="stat">API errors<b id="errors">0</b></div><div class="stat">Cycles<b id="cycle">0</b></div></div>
<div class="panel"><h3>Live signal ranking</h3><p class="small">Confidence is a heuristic indicator-strength score, not a probability of winning.</p>
<div class="wrap"><table><thead><tr><th>Pair</th><th>Signal</th><th>Confidence</th><th>RSI</th><th>Price</th><th>Payout</th><th>Candle UTC</th></tr></thead><tbody id="rows"><tr><td colspan="7">Waiting for scan...</td></tr></tbody></table></div></div>
<div class="panel"><h3>Recent scan history</h3><div id="history" class="small">No results yet.</div></div>
<p class="small">For information only, not financial advice. OTC and binary-options trading carry a high risk of loss. Verify signals and test on demo first.</p>
</main><script>
const cls=s=>s==='UP'?'up':s==='DOWN'?'down':'wait';
async function refresh(){try{let d=await(await fetch('/api/state')).json();
document.getElementById('status').textContent=d.status+' · Last scan: '+(d.last_scan||'—');
document.getElementById('message').textContent=d.message||'';
for(let [id,k] of [['pairs','pairs_total'],['scanned','scanned'],['errors','api_errors'],['cycle','cycle']])document.getElementById(id).textContent=d[k];
document.getElementById('rows').innerHTML=d.results.length?d.results.map(x=>`<tr><td>${x.pair}</td><td class="${cls(x.signal)}">${x.signal}</td><td>${x.confidence}%</td><td>${x.rsi??'—'}</td><td>${x.price}</td><td>${x.payout||'—'}%</td><td>${x.candle_time||'—'}</td></tr>`).join(''):'<tr><td colspan="7">Waiting for candle data...</td></tr>';
document.getElementById('history').innerHTML=d.history.slice(0,12).map(x=>`${x.time} · ${x.pair} · <b class="${cls(x.signal)}">${x.signal}</b> · ${x.confidence}%<br>`).join('')||'No results yet.';
}catch(e){document.getElementById('status').textContent='Reconnecting...'}}
refresh();setInterval(refresh,5000);
</script></body></html>"""

@app.get("/")
def home():
    return render_template_string(PAGE)

@app.get("/health")
def health():
    return jsonify({"ok": True, "app": "quotex-m1-market-scanner"})

@app.get("/api/state")
def api_state():
    with lock:
        return jsonify({**{k:v for k,v in state.items() if k != "history"},
                        "history": list(state["history"])})

if __name__ == "__main__":
    threading.Thread(target=scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
