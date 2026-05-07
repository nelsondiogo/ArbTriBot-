#!/usr/bin/env python3
import os
import time
import logging
import threading
import hashlib
from typing import List, Optional

import requests
import ccxt
from flask import Flask, jsonify, render_template_string

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

BYBIT_API_KEY      = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.getenv("BYBIT_API_SECRET", "")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TRADE_AMOUNT_USDT  = max(10.0, float(os.getenv("TRADE_AMOUNT_USDT", "10")))
MIN_PROFIT_PCT     = float(os.getenv("MIN_PROFIT_PERCENT", "0.3"))
AUTO_TRADE         = os.getenv("AUTO_TRADE", "false").lower() == "true"
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))
RENDER_URL         = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000")
PORT               = int(os.getenv("PORT", "10000"))

FEES = {"bybit": 0.001, "binance": 0.001}

TRIANGLES = [
    ["USDT", "BTC", "ETH"],
    ["USDT", "BTC", "BNB"],
    ["USDT", "BTC", "SOL"],
    ["USDT", "BTC", "XRP"],
    ["USDT", "BTC", "ADA"],
    ["USDT", "BTC", "DOGE"],
    ["USDT", "ETH", "BNB"],
    ["USDT", "ETH", "SOL"],
    ["USDT", "ETH", "LINK"],
]


class State:
    def __init__(self):
        self.running       = True
        self.auto_trade    = AUTO_TRADE
        self.amount        = TRADE_AMOUNT_USDT
        self.min_profit    = MIN_PROFIT_PCT
        self.opportunities: List[dict] = []
        self.trade_history: List[dict] = []
        self.lock          = threading.Lock()
        self.stats = {
            "scans":        0,
            "opps_found":   0,
            "trades_ok":    0,
            "trades_fail":  0,
            "total_profit": 0.0,
            "errors":       0,
            "last_scan":    None,
            "start_time":   time.time(),
        }
        self.exchange_status = {
            "bybit":   {"connected": False, "balance": 0.0, "error": ""},
            "binance": {"connected": False, "balance": 0.0, "error": ""},
        }


G = State()


class Exchanges:
    def __init__(self):
        self.ex: dict      = {}
        self.markets: dict = {}
        self._init()

    def _init(self):
        for name, cls, key, secret in [
            ("bybit",   ccxt.bybit,   BYBIT_API_KEY,   BYBIT_API_SECRET),
            ("binance", ccxt.binance, BINANCE_API_KEY, BINANCE_API_SECRET),
        ]:
            try:
                self.ex[name] = cls({
                    "apiKey": key,
                    "secret": secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                })
                log.info("✅ %s OK", name)
            except Exception as e:
                log.error("❌ %s: %s", name, e)
                G.exchange_status[name]["error"] = str(e)

    def load_markets(self):
        for name, ex in self.ex.items():
            try:
                self.markets[name] = ex.load_markets()
                G.exchange_status[name]["connected"] = True
                log.info("📊 %s: %d pares", name, len(self.markets[name]))
            except Exception as e:
                log.error("❌ mercados %s: %s", name, e)
                G.exchange_status[name]["error"] = str(e)

    def has_pair(self, ex: str, symbol: str) -> bool:
        return symbol in self.markets.get(ex, {})

    def best_ask(self, ex: str, sym: str) -> Optional[float]:
        try:
            ob   = self.ex[ex].fetch_order_book(sym, 3)
            asks = ob.get("asks", [])
            return float(asks[0][0]) if asks else None
        except:
            return None

    def best_bid(self, ex: str, sym: str) -> Optional[float]:
        try:
            ob   = self.ex[ex].fetch_order_book(sym, 3)
            bids = ob.get("bids", [])
            return float(bids[0][0]) if bids else None
        except:
            return None

    def get_balance(self, ex_name: str) -> dict:
        try:
            bal  = self.ex[ex_name].fetch_balance()
            usdt = bal.get("USDT", {}).get("free", 0.0)
            G.exchange_status[ex_name]["balance"] = round(float(usdt), 2)
            return bal
        except Exception as e:
            G.exchange_status[ex_name]["error"] = str(e)
            return {}

    def execute_order(self, ex: str, symbol: str, side: str, amount: float) -> dict:
        try:
            order = self.ex[ex].create_market_order(symbol, side, amount)
            return {"ok": True, "order": order}
        except Exception as e:
            return {"ok": False, "error": str(e)}


EX = Exchanges()


def _step(ex: str, base: str, quote: str, amount_in: float) -> Optional[dict]:
    fee  = FEES.get(ex, 0.001)
    sym1 = f"{base}/{quote}"
    sym2 = f"{quote}/{base}"
    if EX.has_pair(ex, sym1):
        price = EX.best_ask(ex, sym1)
        if price:
            return {"pair": sym1, "side": "buy", "price": price,
                    "in": amount_in, "out": (amount_in / price) * (1 - fee)}
    if EX.has_pair(ex, sym2):
        price = EX.best_bid(ex, sym2)
        if price:
            return {"pair": sym2, "side": "sell", "price": price,
                    "in": amount_in, "out": amount_in * price * (1 - fee)}
    return None


def calc_triangle(ex: str, tri: list, amount: float) -> Optional[dict]:
    A, B, C = tri
    best = None
    for route_nodes in ([A, B, C, A], [A, C, B, A]):
        cur, steps, failed = amount, [], False
        for i in range(3):
            step = _step(ex, base=route_nodes[i+1], quote=route_nodes[i], amount_in=cur)
            if not step:
                failed = True
                break
            steps.append(step)
            cur = step["out"]
        if failed:
            continue
        profit_pct = ((cur - amount) / amount) * 100
        if profit_pct < G.min_profit:
            continue
        route = "→".join(route_nodes)
        opp = {
            "exchange":    ex,
            "route":       route,
            "steps":       steps,
            "amount":      amount,
            "profit_pct":  profit_pct,
            "profit_usdt": cur - amount,
            "timestamp":   time.time(),
            "id": hashlib.md5(
                f"{ex}{route}{round(profit_pct,2)}".encode()
            ).hexdigest()[:8],
        }
        if best is None or profit_pct > best["profit_pct"]:
            best = opp
    return best


def scan_all() -> List[dict]:
    results = []
    for ex in list(EX.ex.keys()):
        for tri in TRIANGLES:
            try:
                opp = calc_triangle(ex, tri, G.amount)
                if opp:
                    results.append(opp)
                    G.stats["opps_found"] += 1
            except Exception as e:
                G.stats["errors"] += 1
                log.debug("Erro %s %s: %s", ex, tri, e)
    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


def execute_trade(opp: dict) -> dict:
    for step in opp["steps"]:
        r = EX.execute_order(opp["exchange"], step["pair"], step["side"], step["in"])
        if not r["ok"]:
            G.stats["trades_fail"] += 1
            rec = {**opp, "success": False, "error": r["error"], "ts": time.time()}
            G.trade_history.append(rec)
            return rec
        time.sleep(0.15)
    G.stats["trades_ok"]    += 1
    G.stats["total_profit"] += opp["profit_usdt"]
    rec = {**opp, "success": True, "ts": time.time()}
    G.trade_history.append(rec)
    return rec


def scan_loop():
    log.info("🔄 Scan loop a iniciar...")
    EX.load_markets()
    EX.get_balance("bybit")
    EX.get_balance("binance")
    while True:
        try:
            if G.running:
                with G.lock:
                    opps             = scan_all()
                    G.opportunities  = opps
                    G.stats["scans"] += 1
                    G.stats["last_scan"] = time.time()
                if G.auto_trade:
                    for opp in opps[:1]:
                        execute_trade(opp)
            if G.stats["scans"] % 5 == 0:
                EX.get_balance("bybit")
                EX.get_balance("binance")
        except Exception as e:
            G.stats["errors"] += 1
            log.error("Scan error: %s", e)
        time.sleep(SCAN_INTERVAL)


def keep_alive():
    time.sleep(90)
    while True:
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
            log.info("🏓 keep-alive OK")
        except Exception as e:
            log.warning("keep-alive: %s", e)
        time.sleep(270)


# ══════════════════════════════════════════
# FLASK DASHBOARD
# ══════════════════════════════════════════

flask_app = Flask(__name__)

HTML = r"""
<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbitrage Bot</title>
<style>
:root{--bg:#090d1a;--card:#111827;--border:#1f2d45;--accent:#7c3aed;
      --green:#10b981;--red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;
      --text:#e2e8f0;--muted:#6b7280}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
header{background:linear-gradient(135deg,#1a1f35,#0d1117);
       border-bottom:1px solid var(--border);padding:16px 20px;
       display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
header h1{font-size:1.2em;color:var(--accent)}
header p{font-size:.75em;color:var(--muted);margin-top:3px}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{padding:4px 12px;border-radius:20px;font-size:.75em;font-weight:600;cursor:pointer}
.bg{background:#1a3a2a;color:var(--green)}
.bb{background:#1e2a4a;color:var(--blue)}
.bp{background:#2d1a4a;color:#c4b5fd}
.br{background:#3a1a1a;color:var(--red)}
.by{background:#3a2a00;color:var(--yellow)}
main{max-width:1200px;margin:0 auto;padding:14px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.kc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;transition:transform .2s}
.kc:hover{transform:translateY(-2px)}
.kl{font-size:.68em;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:5px}
.kv{font-size:1.7em;font-weight:700}
.ks{font-size:.68em;color:var(--muted);margin-top:3px}
.cg .kv{color:var(--green)}.cb .kv{color:var(--blue)}
.cy .kv{color:var(--yellow)}.cp .kv{color:#c4b5fd}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:680px){.two{grid-template-columns:1fr}}
.sec{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}
.st{font-size:.85em;color:#c4b5fd;font-weight:600;margin-bottom:10px;
    padding-bottom:7px;border-bottom:1px solid var(--border);
    display:flex;justify-content:space-between;align-items:center}
.eg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ec{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center}
.en{font-size:.9em;font-weight:700;margin-bottom:5px}
.eb{font-size:1.4em;font-weight:700;color:var(--green)}
.es{font-size:.7em;color:var(--muted);margin-top:3px}
.ok::before{content:"● ";color:var(--green)}.er::before{content:"● ";color:var(--red)}
.ct{width:100%;border-collapse:collapse;font-size:.82em}
.ct td{padding:7px 5px;border-bottom:1px solid var(--border)}
.ct tr:last-child td{border-bottom:none}
.ct td:first-child{color:var(--muted);width:45%}
.btn{padding:5px 12px;border-radius:8px;border:none;cursor:pointer;
     font-size:.78em;font-weight:600;transition:opacity .2s}
.btn:hover{opacity:.8}
.btn-g{background:var(--green);color:#000}
.btn-r{background:var(--red);color:#fff}
.btn-y{background:var(--yellow);color:#000}
.btn-b{background:var(--blue);color:#fff}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.opp{background:var(--bg);border:1px solid var(--border);
     border-left:3px solid var(--green);border-radius:8px;
     padding:12px;margin-bottom:8px}
.opp.hot{border-left-color:var(--yellow)}
.oh{display:flex;justify-content:space-between;align-items:center;
    flex-wrap:wrap;gap:5px;margin-bottom:7px}
.xt{background:#1f2d45;padding:2px 8px;border-radius:10px;font-size:.72em;font-weight:600}
.pp{font-size:1.15em;font-weight:700;color:var(--green)}
.pp.hot{color:var(--yellow)}
.pu{color:var(--yellow);font-size:.82em}
.rt{font-family:monospace;font-size:.75em;color:var(--muted);
    background:#090d1a;padding:5px 8px;border-radius:5px;margin-bottom:5px}
.sp{font-family:monospace;font-size:.7em;color:#9ca3af}
.sp div{padding:1px 0}
.tbl{width:100%;border-collapse:collapse;font-size:.8em}
.tbl th,.tbl td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:600}
.tbl tr:last-child td{border-bottom:none}
.pos{color:var(--green)}.neg{color:var(--red)}
.pl{padding:2px 7px;border-radius:8px;font-size:.75em;font-weight:600}
.ok2{background:#0a2a1a;color:var(--green)}.fail{background:#2a0a0a;color:var(--red)}
.empty{text-align:center;padding:28px;color:var(--muted);
       background:var(--bg);border-radius:8px;border:1px dashed var(--border)}
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
       background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-box{background:var(--card);border:1px solid var(--border);border-radius:12px;
           padding:24px;max-width:400px;width:90%}
.modal-title{font-size:1em;color:#c4b5fd;margin-bottom:16px;font-weight:600}
.input-group{margin-bottom:12px}
.input-group label{font-size:.78em;color:var(--muted);display:block;margin-bottom:4px}
.input-group input{width:100%;background:var(--bg);border:1px solid var(--border);
                   border-radius:6px;padding:8px 10px;color:var(--text);font-size:.9em}
.modal-btns{display:flex;gap:8px;margin-top:16px}
footer{text-align:center;padding:12px;color:var(--muted);font-size:.72em}
.prog{height:2px;background:var(--border);border-radius:2px;overflow:hidden;margin-top:5px}
.pf{height:100%;background:var(--accent);animation:pr 15s linear infinite}
@keyframes pr{from{width:0}to{width:100%}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.live{animation:pulse 2s infinite}
</style>
</head>
<body>

<header>
  <div>
    <h1>🤖 Arbitrage Bot</h1>
    <p>Bybit &amp; Binance · Triangular · 24h</p>
  </div>
  <div class="badges">
    <span class="badge bg live" id="hst">⬤ —</span>
    <span class="badge bb" id="hsc">🔄 —</span>
    <span class="badge bp" id="hmd">👁️ Monitor</span>
    <span class="badge bp" id="hvl">💵 —</span>
  </div>
</header>

<main>

  <!-- BOTÕES DE CONTROLO -->
  <div class="btns">
    <button class="btn btn-g" onclick="botAction('start')">▶️ Iniciar</button>
    <button class="btn btn-r" onclick="botAction('stop')">⏹️ Parar</button>
    <button class="btn btn-b" onclick="botAction('scan')">🔍 Scan Agora</button>
    <button class="btn btn-y" onclick="showSettings()">⚙️ Configurar</button>
    <button class="btn" style="background:#2d1a4a;color:#c4b5fd"
            onclick="botAction('toggle_auto')" id="btn-auto">🤖 Auto-Trade OFF</button>
  </div>

  <!-- KPIs -->
  <div class="kpi">
    <div class="kc cb"><div class="kl">🔄 Scans</div>
      <div class="kv" id="ksc">—</div><div class="ks">Total</div></div>
    <div class="kc cp"><div class="kl">💡 Oportunidades</div>
      <div class="kv" id="kop">—</div><div class="ks">Detectadas</div></div>
    <div class="kc cg"><div class="kl">✅ Trades OK</div>
      <div class="kv" id="ktk">—</div><div class="ks" id="ktf">0 falhados</div></div>
    <div class="kc cy"><div class="kl">💵 Lucro</div>
      <div class="kv" id="kpr">—</div><div class="ks">USDT</div></div>
    <div class="kc cb"><div class="kl">⚠️ Erros</div>
      <div class="kv" id="ker">—</div><div class="ks">Total</div></div>
    <div class="kc cg"><div class="kl">⏱️ Uptime</div>
      <div class="kv" id="kup">—</div><div class="ks">Desde início</div></div>
  </div>

  <!-- Exchanges + Config -->
  <div class="two">
    <div class="sec">
      <div class="st">🏦 Exchanges</div>
      <div class="eg" id="exg">
        <div class="ec"><div class="en">A carregar...</div></div>
      </div>
    </div>
    <div class="sec">
      <div class="st">⚙️ Configuração Actual</div>
      <table class="ct">
        <tr><td>Volume</td><td id="cvl">—</td></tr>
        <tr><td>Lucro Mínimo</td><td id="cpf">—</td></tr>
        <tr><td>Auto-Trade</td><td id="cat">—</td></tr>
        <tr><td>Interval</td><td id="cit">—</td></tr>
        <tr><td>Triângulos</td><td id="ctr">—</td></tr>
        <tr><td>Último Scan</td><td id="cls">—</td></tr>
      </table>
    </div>
  </div>

  <!-- Oportunidades -->
  <div class="sec">
    <div class="st">
      <span>🎯 Oportunidades <span id="oct" style="color:var(--muted);font-weight:400"></span></span>
    </div>
    <div id="opls"><div class="empty">🔍 A aguardar scan...</div></div>
  </div>

  <!-- Histórico -->
  <div class="sec">
    <div class="st">📜 Histórico de Trades</div>
    <div id="hist"><div class="empty">Sem trades executados</div></div>
  </div>

</main>

<!-- MODAL CONFIGURAÇÕES -->
<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-title">⚙️ Configurações</div>
    <div class="input-group">
      <label>Volume (USDT) — mínimo 10</label>
      <input type="number" id="inp-amount" min="10" step="1" value="10">
    </div>
    <div class="input-group">
      <label>Lucro Mínimo (%)</label>
      <input type="number" id="inp-profit" min="0.1" step="0.1" value="0.3">
    </div>
    <div class="modal-btns">
      <button class="btn btn-g" onclick="saveSettings()">✅ Guardar</button>
      <button class="btn btn-r" onclick="closeModal()">❌ Cancelar</button>
    </div>
  </div>
</div>

<footer>
  Actualiza a cada 15s
  <div class="prog"><div class="pf"></div></div>
</footer>

<script>
const f2=n=>Number(n).toFixed(2),f3=n=>Number(n).toFixed(3),f4=n=>Number(n).toFixed(4);
const ago=ts=>{
  const s=Math.round(Date.now()/1e3-ts);
  return s<60?s+'s atrás':s<3600?Math.round(s/60)+'m atrás':Math.round(s/3600)+'h atrás'
};
const upt=s=>{
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h+'h '+m+'m'
};

async function load(){
  try{
    const r=await fetch('/api');
    const d=await r.json();
    render(d);
  }catch(e){console.warn(e)}
}

async function botAction(action, data={}){
  try{
    await fetch('/control',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,...data})
    });
    setTimeout(load,500);
  }catch(e){console.warn(e)}
}

function showSettings(){
  document.getElementById('modal').classList.add('show');
}
function closeModal(){
  document.getElementById('modal').classList.remove('show');
}
async function saveSettings(){
  const amount=parseFloat(document.getElementById('inp-amount').value);
  const profit=parseFloat(document.getElementById('inp-profit').value);
  if(amount<10){alert('Mínimo 10 USDT');return}
  await botAction('settings',{amount,profit});
  closeModal();
}

function render(d){
  const s=d.stats||{},ex=d.exchanges||{},op=d.opportunities||[],
        tr=d.trades||[],cf=d.config||{};
  const run=s.running;

  document.getElementById('hst').textContent=run?'⬤ ACTIVO':'⬤ PARADO';
  document.getElementById('hst').className='badge '+(run?'bg live':'br');
  document.getElementById('hsc').textContent=s.last_scan?'🔄 '+ago(s.last_scan):'🔄 —';
  document.getElementById('hmd').textContent=cf.auto_trade?'🤖 Auto-Trade':'👁️ Monitor';
  document.getElementById('hvl').textContent='💵 $'+(cf.amount||0);

  document.getElementById('ksc').textContent=s.scans||0;
  document.getElementById('kop').textContent=s.opps_found||0;
  document.getElementById('ktk').textContent=s.trades_ok||0;
  document.getElementById('ktf').textContent=(s.trades_fail||0)+' falhados';
  document.getElementById('kpr').textContent='$'+f4(s.total_profit||0);
  document.getElementById('ker').textContent=s.errors||0;
  if(s.start_time)
    document.getElementById('kup').textContent=upt(Math.round(Date.now()/1e3-s.start_time));

  document.getElementById('btn-auto').textContent=
    cf.auto_trade?'🤖 Auto-Trade ON':'🤖 Auto-Trade OFF';

  const eg=document.getElementById('exg');
  if(Object.keys(ex).length){
    eg.innerHTML=Object.entries(ex).map(([n,i])=>`
      <div class="ec">
        <div class="en ${i.connected?'ok':'er'}">${n.toUpperCase()}</div>
        <div class="eb">$${f2(i.balance||0)}</div>
        <div class="es">${i.connected?'✅ Conectado':'❌ Desconectado'}</div>
        ${i.error?`<div style="font-size:.65em;color:var(--red);margin-top:3px">${i.error.slice(0,60)}</div>`:''}
      </div>`).join('');
  }

  document.getElementById('cvl').textContent='$'+f2(cf.amount||0)+' USDT';
  document.getElementById('cpf').textContent=f2(cf.min_profit||0)+'%';
  document.getElementById('cat').innerHTML=cf.auto_trade
    ?'<span class="pl ok2">ON ✅</span>'
    :'<span class="pl fail">OFF ❌</span>';
  document.getElementById('cit').textContent=(cf.scan_interval||15)+'s';
  document.getElementById('ctr').textContent=(cf.triangles||0)+' pares';
  document.getElementById('cls').textContent=s.last_scan?ago(s.last_scan):'—';

  document.getElementById('inp-amount').value=cf.amount||10;
  document.getElementById('inp-profit').value=cf.min_profit||0.3;

  document.getElementById('oct').textContent=op.length?'('+op.length+')':'';
  const od=document.getElementById('opls');
  if(op.length){
    od.innerHTML=op.slice(0,10).map(o=>{
      const hot=o.profit_pct>=1;
      const st=(o.steps||[]).map((s,i)=>
        `<div>${['1️⃣','2️⃣','3️⃣'][i]||'•'} ${(s.side||'').toUpperCase()} ${s.pair||''} @ ${Number(s.price||0).toFixed(8)}</div>`
      ).join('');
      return `<div class="opp ${hot?'hot':''}">
        <div class="oh">
          <span class="xt">${o.exchange.toUpperCase()}</span>
          <span class="pp ${hot?'hot':''}">+${f3(o.profit_pct)}%</span>
          <span class="pu">+$${f4(o.profit_usdt)}</span>
          <span style="font-size:.7em;color:var(--muted)">${ago(o.timestamp)}</span>
        </div>
        <div class="rt">🔄 ${o.route}</div>
        <div class="sp">${st}</div>
        <div style="font-size:.68em;color:var(--muted);margin-top:5px">
          Vol: $${f2(o.amount)} · ID: ${o.id}
        </div>
      </div>`;
    }).join('');
  }else{
    od.innerHTML='<div class="empty">🔍 Sem oportunidades lucrativas neste momento</div>';
  }

  const hw=document.getElementById('hist');
  if(tr.length){
    const rows=[...tr].reverse().slice(0,20).map(t=>`
      <tr>
        <td>${new Date((t.ts||0)*1e3).toLocaleTimeString('pt-PT')}</td>
        <td>${(t.exchange||'').toUpperCase()}</td>
        <td style="font-family:monospace;font-size:.78em">${t.route||'—'}</td>
        <td class="${(t.profit_usdt||0)>=0?'pos':'neg'}">
          ${(t.profit_usdt||0)>=0?'+':''}$${f4(t.profit_usdt||0)}
        </td>
        <td class="${(t.profit_pct||0)>=0?'pos':'neg'}">${f3(t.profit_pct||0)}%</td>
        <td><span class="pl ${t.success?'ok2':'fail'}">${t.success?'OK':'FAIL'}</span></td>
      </tr>`).join('');
    hw.innerHTML=`<table class="tbl">
      <thead><tr>
        <th>Hora</th><th>Exchange</th><th>Rota</th>
        <th>Lucro $</th><th>%</th><th>Estado</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }else{
    hw.innerHTML='<div class="empty">Sem trades ainda</div>';
  }
}

load();
setInterval(load,15000);
</script>
</body>
</html>
"""


@flask_app.route("/")
def dashboard():
    return render_template_string(HTML)


@flask_app.route("/api")
def api():
    return jsonify({
        "stats":         {**G.stats, "running": G.running},
        "exchanges":     G.exchange_status,
        "opportunities": G.opportunities[:10],
        "trades":        G.trade_history[-50:],
        "config": {
            "amount":        G.amount,
            "min_profit":    G.min_profit,
            "auto_trade":    G.auto_trade,
            "scan_interval": SCAN_INTERVAL,
            "triangles":     len(TRIANGLES),
        },
    })


@flask_app.route("/control", methods=["POST"])
def control():
    data   = request.get_json() or {}
    action = data.get("action", "")

    if action == "start":
        G.running = True
    elif action == "stop":
        G.running = False
    elif action == "scan":
        threading.Thread(target=lambda: scan_all(), daemon=True).start()
    elif action == "toggle_auto":
        G.auto_trade = not G.auto_trade
    elif action == "settings":
        amount = float(data.get("amount", G.amount))
        profit = float(data.get("profit", G.min_profit))
        if amount >= 10:
            G.amount = amount
        if 0.05 <= profit <= 20:
            G.min_profit = profit

    return jsonify({"ok": True})


@flask_app.route("/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

def main():
    log.info("🚀 A iniciar...")

    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    log.info("🌐 Dashboard em porta %d", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
