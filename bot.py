import os
import time
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

PORT = int(os.getenv("PORT", "10000"))
API_KEY = os.getenv("BDT_API_KEY", "").strip()

BASE_URL = "https://bangladeshitrader.com/chart"
PAIR_URL = BASE_URL + "/api/pairs"
CANDLE_URL = BASE_URL + "/Qx.php"

TIMEFRAME = "M1"
CANDLE_COUNT = 100
SCAN_INTERVAL = 3

lock = threading.Lock()
state = {
    "status": "Starting",
    "last_update": "",
    "pairs": [],
    "signals": [],
    "error": ""
}


def api_get(url, params=None):
    if not API_KEY:
        raise Exception("BDT_API_KEY environment variable missing")

    params = params or {}
    params["key"] = API_KEY

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def get_pairs():
    result = api_get(PAIR_URL)

    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = (
            result.get("data")
            or result.get("pairs")
            or result.get("result")
            or []
        )
    else:
        items = []

    pairs = []

    for item in items:
        if isinstance(item, str):
            name = item
            broker = "quotex"
            opened = True
        elif isinstance(item, dict):
            name = (
                item.get("display")
                or item.get("pair")
                or item.get("symbol")
                or item.get("name")
            )
            broker = str(item.get("broker", "quotex")).lower()
            opened = item.get("open", item.get("active", True))
        else:
            continue

        if not name:
            continue

        if "_otc" not in str(name).lower():
            continue

        if "quotex" not in broker:
            continue

        if opened is False:
            continue

        pairs.append(str(name))

    return list(dict.fromkeys(pairs))


def get_candles(pair):
    result = api_get(
        CANDLE_URL,
        {
            "pair": pair,
            "timeframe": TIMEFRAME,
            "count": CANDLE_COUNT
        }
    )

    if not isinstance(result, dict) or not result.get("success", True):
        return []

    candles = result.get("data", [])

    if not isinstance(candles, list):
        return []

    cleaned = []

    for candle in candles:
        try:
            cleaned.append({
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })
        except (KeyError, TypeError, ValueError):
            continue

    return cleaned


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def rsi(values, period=14):
    if len(values) <= period:
        return 50

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(pair, candles):
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]

    fast = ema(closes, 9)
    slow = ema(closes, 21)
    strength = rsi(closes)

    if fast is None or slow is None:
        return None

    score = 0
    reasons = []

    if fast > slow:
        score += 1
        reasons.append("EMA bullish")
    elif fast < slow:
        score -= 1
        reasons.append("EMA bearish")

    if strength >= 55 and strength < 75:
        score += 1
        reasons.append("RSI bullish")
    elif strength <= 45 and strength > 25:
        score -= 1
        reasons.append("RSI bearish")

    previous = candles[-2]
    latest = candles[-1]

    if latest["close"] > latest["open"]:
        score += 1
        reasons.append("Bullish candle")
    elif latest["close"] < latest["open"]:
        score -= 1
        reasons.append("Bearish candle")

    if score >= 2:
        signal = "UP"
    elif score <= -2:
        signal = "DOWN"
    else:
        signal = "WAIT"

    confidence = min(95, 50 + abs(score) * 12)

    if signal == "WAIT":
        confidence = 50

    return {
        "pair": pair,
        "timeframe": TIMEFRAME,
        "signal": signal,
        "confidence": confidence,
        "rsi": round(strength, 2),
        "ema9": round(fast, 6),
        "ema21": round(slow, 6),
        "price": latest["close"],
        "reasons": reasons,
        "updated": datetime.now().strftime("%H:%M:%S")
    }


def scanner():
    while True:
        try:
            with lock:
                state["status"] = "Scanning"
                state["error"] = ""

            pairs = get_pairs()
            results = []

            for pair in pairs:
                try:
                    candles = get_candles(pair)
                    result = analyze(pair, candles)

                    if result:
                        results.append(result)

                except Exception as exc:
                    print("Pair error:", pair, str(exc))

                time.sleep(SCAN_INTERVAL)

            results.sort(
                key=lambda x: x["confidence"],
                reverse=True
            )

            with lock:
                state["pairs"] = pairs
                state["signals"] = results
                state["last_update"] = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                state["status"] = "Active"

            print(
                "Scan complete:",
                len(pairs),
                "pairs;",
                len(results),
                "signals"
            )

        except Exception as exc:
            with lock:
                state["status"] = "API Error"
                state["error"] = str(exc)

            print("Scanner error:", str(exc))

        time.sleep(10)


HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUOTEX OTC LIVE SCANNER</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#101522;color:#fff;font-family:Arial;padding:15px}
h2{color:#32d9a0}
.card{background:#1c2638;padding:15px;border-radius:12px;margin:12px 0}
.stat{display:flex;justify-content:space-between;gap:10px}
.green{color:#32d9a0}
.red{color:#ff647c}
.yellow{color:#ffd166}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:10px 5px;border-bottom:1px solid #39445a;text-align:left}
button{background:#32d9a0;border:0;padding:10px 15px;border-radius:7px;font-weight:bold}
.small{color:#aab6ca;font-size:12px}
</style>
</head>
<body>
<h2>QUOTEX OTC LIVE SCANNER</h2>

<div class="card">
<div class="stat">
<b>Status</b>
<b id="status">Loading...</b>
</div>
<p class="small">Last update: <span id="updated">--</span></p>
<div class="stat">
<span>OTC Pairs</span>
<b id="pairs">0</b>
</div>
<div class="stat">
<span>Analyzed</span>
<b id="analyzed">0</b>
</div>
</div>

<div class="card">
<h3>M1 LIVE SIGNALS</h3>
<p class="small">UP / DOWN / WAIT</p>
<table>
<thead>
<tr>
<th>PAIR</th>
<th>SIGNAL</th>
<th>CONF.</th>
<th>RSI</th>
</tr>
</thead>
<tbody id="signals">
<tr><td colspan="4">Waiting for data...</td></tr>
</tbody>
</table>
</div>

<div class="card">
<b>API Information</b>
<p class="small" id="error">No error</p>
<p class="small">Signals are statistical estimates, not guaranteed predictions.</p>
</div>

<button onclick="loadData()">Refresh Now</button>

<script>
async function loadData(){
 try{
  const response=await fetch('/api/state');
  const data=await response.json();

  document.getElementById('status').textContent=data.status;
  document.getElementById('updated').textContent=data.last_update||'--';
  document.getElementById('pairs').textContent=data.pairs.length;
  document.getElementById('analyzed').textContent=data.signals.length;
  document.getElementById('error').textContent=data.error||'No error';

  const tbody=document.getElementById('signals');
  tbody.innerHTML='';

  if(!data.signals.length){
   tbody.innerHTML='<tr><td colspan="4">No signals yet</td></tr>';
  }

  data.signals.forEach(item=>{
   const tr=document.createElement('tr');
   const color=item.signal==='UP'?'green':
               item.signal==='DOWN'?'red':'yellow';

   [item.pair,item.signal,item.confidence+'%',item.rsi].forEach((value,index)=>{
    const td=document.createElement('td');
    td.textContent=value;
    if(index===1)td.className=color;
    tr.appendChild(td);
   });

   tbody.appendChild(tr);
  });
 }catch(e){
  document.getElementById('error').textContent=e.message;
 }
}

loadData();
setInterval(loadData,5000);
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/state")
def api_state():
    with lock:
        return jsonify(state)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    threading.Thread(target=scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)