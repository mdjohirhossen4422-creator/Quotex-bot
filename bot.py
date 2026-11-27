import os
import time
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# SETTINGS
PORT = int(os.getenv("PORT", "10000"))
API_KEY = os.getenv("BDT_API_KEY", "").strip()

BASE_URL = "https://bangladeshitrader.com/chart"
PAIR_URL = os.getenv("PAIR_URL", BASE_URL + "/api/pairs")
CANDLE_URL = os.getenv("CANDLE_URL", BASE_URL + "/Qx.php")

TIMEFRAME = "M1"
CANDLE_COUNT = 120
REQUEST_DELAY = 2.1

lock = threading.Lock()

state = {
    "status": "Starting",
    "timeframe": TIMEFRAME,
    "last_update": "",
    "pairs": [],
    "signals": [],
    "error": ""
}


# API REQUEST
def api_request(url, params=None):
    if not API_KEY:
        raise Exception("BDT_API_KEY is missing in Render Environment")

    params = dict(params or {})
    params["key"] = API_KEY

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get("success") is False:
        raise Exception(str(data.get("message", "API request failed")))

    return data


# GET QUOTEX OTC PAIRS
def get_pairs():
    data = api_request(PAIR_URL)

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("data")
            or data.get("pairs")
            or data.get("result")
            or []
        )
    else:
        items = []

    pairs = []

    for item in items:
        if isinstance(item, str):
            name = item
            broker = "quotex"
            active = True
        elif isinstance(item, dict):
            name = (
                item.get("display")
                or item.get("pair")
                or item.get("symbol")
                or item.get("name")
            )

            broker = str(item.get("broker", "quotex")).lower()
            active = item.get("open", item.get("active", True))
        else:
            continue

        if not name:
            continue

        if "_otc" not in str(name).lower():
            continue

        if "quotex" not in broker:
            continue

        if active is False:
            continue

        pairs.append(str(name))

    return list(dict.fromkeys(pairs))


# GET M1 CANDLES
def get_candles(pair):
    data = api_request(
        CANDLE_URL,
        {
            "pair": pair,
            "timeframe": TIMEFRAME,
            "count": CANDLE_COUNT
        }
    )

    if isinstance(data, dict):
        candles = data.get("data", [])
    elif isinstance(data, list):
        candles = data
    else:
        candles = []

    cleaned = []

    for candle in candles:
        try:
            cleaned.append({
                "time": candle.get("time"),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"])
            })
        except (KeyError, TypeError, ValueError):
            continue

    return cleaned


# EMA
def calculate_ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


# RSI
def calculate_rsi(values, period=14):
    if len(values) <= period:
        return 50

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# MACD
def calculate_macd(values):
    if len(values) < 35:
        return 0, 0

    fast = calculate_ema(values, 12)
    slow = calculate_ema(values, 26)

    if fast is None or slow is None:
        return 0, 0

    macd = fast - slow

    previous_fast = calculate_ema(values[:-1], 12)
    previous_slow = calculate_ema(values[:-1], 26)

    previous_macd = (
        previous_fast - previous_slow
        if previous_fast is not None and previous_slow is not None
        else 0
    )

    return macd, macd - previous_macd


# MARKET ANALYSIS ENGINE
def analyze_market(pair, candles):
    if len(candles) < 35:
        return None

    closes = [c["close"] for c in candles]

    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
    macd, macd_signal = calculate_macd(closes)

    latest = candles[-1]
    previous = candles[-2]

    score = 0
    reasons = []

    # EMA TREND
    if ema9 > ema21:
        score += 1
        reasons.append("EMA bullish")
    elif ema9 < ema21:
        score -= 1
        reasons.append("EMA bearish")

    if ema21 > ema50:
        score += 1
        reasons.append("Long trend bullish")
    elif ema21 < ema50:
        score -= 1
        reasons.append("Long trend bearish")

    # RSI
    if 52 <= rsi < 70:
        score += 1
        reasons.append("RSI bullish")
    elif 30 < rsi <= 48:
        score -= 1
        reasons.append("RSI bearish")
    elif rsi >= 75:
        reasons.append("RSI overbought")
    elif rsi <= 25:
        reasons.append("RSI oversold")

    # MACD
    if macd > macd_signal:
        score += 1
        reasons.append("MACD bullish")
    elif macd < macd_signal:
        score -= 1
        reasons.append("MACD bearish")

    # CANDLE ANALYSIS
    if latest["close"] > latest["open"]:
        score += 1
        reasons.append("Green candle")
    elif latest["close"] < latest["open"]:
        score -= 1
        reasons.append("Red candle")

    # SUPPORT AND RESISTANCE
    recent = candles[-20:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    # FINAL SIGNAL
    if score >= 3:
        signal = "CALL"
        trend = "BULLISH"
    elif score <= -3:
        signal = "PUT"
        trend = "BEARISH"
    else:
        signal = "WAIT"
        trend = "SIDEWAYS"

    confidence = min(95, 50 + abs(score) * 8)

    if signal == "WAIT":
        confidence = 50

    return {
        "pair": pair,
        "timeframe": TIMEFRAME,
        "signal": signal,
        "trend": trend,
        "confidence": confidence,
        "score": score,
        "rsi": round(rsi, 2),
        "ema9": round(ema9, 6),
        "ema21": round(ema21, 6),
        "macd": round(macd, 6),
        "support": round(support, 6),
        "resistance": round(resistance, 6),
        "price": latest["close"],
        "reasons": reasons,
        "updated": datetime.now().strftime("%H:%M:%S")
    }


# BACKGROUND SCANNER
def scanner():
    while True:
        try:
            with lock:
                state["status"] = "Scanning"
                state["error"] = ""

            pairs = get_pairs()
            results = []

            if not pairs:
                raise Exception(
                    "No Quotex OTC pairs found. Check pair API response."
                )

            for pair in pairs:
                try:
                    candles = get_candles(pair)
                    result = analyze_market(pair, candles)

                    if result:
                        results.append(result)

                    print(
                        pair,
                        result["signal"] if result else "Not enough candles",
                        flush=True
                    )

                except Exception as exc:
                    print("Pair error:", pair, str(exc), flush=True)

                time.sleep(REQUEST_DELAY)

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
                state["status"] = "ACTIVE"

            print(
                "SCAN COMPLETE:",
                len(pairs),
                "pairs;",
                len(results),
                "analyzed",
                flush=True
            )

        except Exception as exc:
            with lock:
                state["status"] = "API ERROR"
                state["error"] = str(exc)

            print("SCANNER ERROR:", str(exc), flush=True)

        time.sleep(5)


# DASHBOARD
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUOTEX MARKET AI SCANNER</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:15px;background:#09111f;color:white;font-family:Arial}
.main{max-width:850px;margin:auto}
h2{text-align:center;color:#35e5b0}
.card{background:#17243a;padding:16px;border-radius:13px;margin:12px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:#0c1729;padding:13px;border-radius:9px}
.stat b{display:block;font-size:22px;margin-top:7px}
.green{color:#32e6a5}
.red{color:#ff637b}
.yellow{color:#ffd166}
.muted{color:#aab9cf;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:10px 5px;border-bottom:1px solid #35445d}
</style>
</head>
<body>
<div class="main">
<h2>⚡ QUOTEX OTC MARKET AI SCANNER</h2>

<div class="card">
<div class="grid">
<div class="stat">STATUS<b id="status">Loading</b></div>
<div class="stat">TIMEFRAME<b>M1</b></div>
<div class="stat">OTC PAIRS<b id="pairs">0</b></div>
<div class="stat">ANALYZED<b id="analyzed">0</b></div>
</div>
<p class="muted">Last update: <span id="updated">--</span></p>
</div>

<div class="card">
<h3>LIVE MARKET SIGNALS</h3>
<table>
<thead>
<tr>
<th>PAIR</th>
<th>SIGNAL</th>
<th>TREND</th>
<th>CONF.</th>
<th>RSI</th>
</tr>
</thead>
<tbody id="signals">
<tr><td colspan="5">Waiting for live data...</td></tr>
</tbody>
</table>
</div>

<div class="card">
<h3>MARKET DETAILS</h3>
<div id="details" class="muted">Select a signal to view details.</div>
</div>

<div class="card">
<h3>API STATUS</h3>
<p id="error" class="muted">No error</p>
<p class="muted">Statistical analysis only. Signals are not guaranteed predictions.</p>
</div>
</div>

<script>
async function loadData(){
 try{
  const response=await fetch('/api/state');
  const data=await response.json();

  document.getElementById('status').textContent=data.status;
  document.getElementById('pairs').textContent=data.pairs.length;
  document.getElementById('analyzed').textContent=data.signals.length;
  document.getElementById('updated').textContent=data.last_update||'--';
  document.getElementById('error').textContent=data.error||'No error';

  const tbody=document.getElementById('signals');
  tbody.innerHTML='';

  if(!data.signals.length){
   tbody.innerHTML='<tr><td colspan="5">No analyzed candles yet</td></tr>';
  }

  data.signals.forEach(item=>{
   const tr=document.createElement('tr');
   const color=item.signal==='CALL'?'green':
               item.signal==='PUT'?'red':'yellow';

   const values=[
    item.pair,
    item.signal,
    item.trend,
    item.confidence+'%',
    item.rsi
   ];

   values.forEach((value,index)=>{
    const td=document.createElement('td');
    td.textContent=value;
    if(index===1)td.className=color;
    tr.appendChild(td);
   });

   tr.onclick=()=>{
    document.getElementById('details').textContent=
      item.pair+' | '+item.signal+
      ' | RSI '+item.rsi+
      ' | EMA9 '+item.ema9+
      ' | EMA21 '+item.ema21+
      ' | MACD '+item.macd+
      ' | Support '+item.support+
      ' | Resistance '+item.resistance+
      ' | '+item.reasons.join(', ');
   };

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