#!/usr/bin/env python3
import os, time, logging, threading, hashlib, json
from typing import List, Optional
from pathlib import Path
import requests, ccxt
from flask import Flask, jsonify, render_template_string, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PORT         = int(os.getenv("PORT", "10000"))
RENDER_URL   = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000")
SCAN_INTERVAL = 15
TRIANGLES = [
    ["USDT","BTC","ETH"],  ["USDT","BTC","BNB"],
    ["USDT","BTC","SOL"],  ["USDT","BTC","XRP"],
    ["USDT","BTC","ADA"],  ["USDT","BTC","DOGE"],
    ["USDT","ETH","BNB"],  ["USDT","ETH","SOL"],
    ["USDT","ETH","LINK"],
]
FEES = {"bybit": 0.001, "binance": 0.001}

# ── Ficheiro de configuração local ──────────────────────────
CFG_FILE = Path("config.json")

def load_cfg() -> dict:
    if CFG_FILE.exists():
        try:
            return json.loads(CFG_FILE.read_text())
        except:
            pass
    return {
        "bybit_key": "", "bybit_secret": "",
        "binance_key": "", "binance_secret": "",
        "amount": 10.0, "min_profit": 0.3,
        "auto_trade": False,
    }

def save_cfg(cfg: dict):
    CFG_FILE.write_text(json.dumps(cfg, indent=2))

CFG = load_cfg()

# ── Estado global ────────────────────────────────────────────
class State:
    def __init__(self):
        self.running      = False
        self.opportunities: List[dict] = []
        self.trade_history: List[dict] = []
        self.lock         = threading.Lock()
        self.stats = {
            "scans": 0, "opps_found": 0,
            "trades_ok": 0, "trades_fail": 0,
            "total_profit": 0.0, "errors": 0,
            "last_scan": None, "start_time": time.time(),
        }
        self.exchange_status = {
            "bybit":   {"connected": False, "balance": 0.0, "error": ""},
            "binance": {"connected": False, "balance": 0.0, "error": ""},
        }

G = State()

# ── Exchanges ────────────────────────────────────────────────
class Exchanges:
    def __init__(self):
        self.ex: dict      = {}
        self.markets: dict = {}

    def connect(self):
        self.ex = {}
        self.markets = {}
        for name, cls, key, secret in [
            ("bybit",   ccxt.bybit,   CFG["bybit_key"],   CFG["bybit_secret"]),
            ("binance", ccxt.binance, CFG["binance_key"], CFG["binance_secret"]),
        ]:
            if not key or not secret:
                G.exchange_status[name] = {"connected": False, "balance": 0.0, "error": "Chave não configurada"}
                continue
            try:
                self.ex[name] = cls({
                    "apiKey": key, "secret": secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                })
                log.info("✅ %s conectado", name)
            except Exception as e:
                G.exchange_status[name]["error"] = str(e)
                log.error("❌ %s: %s", name, e)

    def load_markets(self):
        for name, ex in self.ex.items():
            try:
                self.markets[name] = ex.load_markets()
                G.exchange_status[name]["connected"] = True
                log.info("📊 %s: %d pares", name, len(self.markets[name]))
            except Exception as e:
                G.exchange_status[name]["error"] = str(e)

    def has_pair(self, ex, sym):
        return sym in self.markets.get(ex, {})

    def best_ask(self, ex, sym):
        try:
            ob = self.ex[ex].fetch_order_book(sym, 3)
            asks = ob.get("asks", [])
            return float(asks[0][0]) if asks else None
        except: return None

    def best_bid(self, ex, sym):
        try:
            ob = self.ex[ex].fetch_order_book(sym, 3)
            bids = ob.get("bids", [])
            return float(bids[0][0]) if bids else None
        except: return None

    def get_balance(self, ex_name):
        try:
            bal  = self.ex[ex_name].fetch_balance()
            usdt = bal.get("USDT", {}).get("free", 0.0)
            G.exchange_status[ex_name]["balance"] = round(float(usdt), 2)
            G.exchange_status[ex_name]["connected"] = True
            G.exchange_status[ex_name]["error"] = ""
            return bal
        except Exception as e:
            G.exchange_status[ex_name]["error"] = str(e)
            return {}

    def execute_order(self, ex, symbol, side, amount):
        try:
            order = self.ex[ex].create_market_order(symbol, side, amount)
            return {"ok": True, "order": order}
        except Exception as e:
            return {"ok": False, "error": str(e)}

EX = Exchanges()

# ── Arbitragem ───────────────────────────────────────────────
def _step(ex, base, quote, amount_in):
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

def calc_triangle(ex, tri, amount):
    A, B, C = tri
    best = None
    for nodes in ([A,B,C,A], [A,C,B,A]):
        cur, steps, fail = amount, [], False
        for i in range(3):
            s = _step(ex, nodes[i+1], nodes[i], cur)
            if not s: fail = True; break
            steps.append(s); cur = s["out"]
        if fail: continue
        pct = ((cur - amount) / amount) * 100
        if pct < CFG["min_profit"]: continue
        route = "→".join(nodes)
        opp = {
            "exchange": ex, "route": route, "steps": steps,
            "amount": amount, "profit_pct": pct,
            "profit_usdt": cur - amount, "timestamp": time.time(),
            "id": hashlib.md5(f"{ex}{route}{round(pct,2)}".encode()).hexdigest()[:8],
        }
        if best is None or pct > best["profit_pct"]: best = opp
    return best

def scan_all():
    results = []
    for ex in list(EX.ex.keys()):
        for tri in TRIANGLES:
            try:
                opp = calc_triangle(ex, tri, CFG["amount"])
                if opp:
                    results.append(opp)
                    G.stats["opps_found"] += 1
            except Exception as e:
                G.stats["errors"] += 1
    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results

def execute_trade(opp):
    for step in opp["steps"]:
        r = EX.execute_order(opp["exchange"], step["pair"], step["side"], step["in"])
        if not r["ok"]:
            G.stats["trades_fail"] += 1
            G.trade_history.append({**opp, "success": False, "error": r["error"], "ts": time.time()})
            return
        time.sleep(0.15)
    G.stats["trades_ok"]    += 1
    G.stats["total_profit"] += opp["profit_usdt"]
    G.trade_history.append({**opp, "success": True, "ts": time.time()})

def scan_loop():
    log.info("🔄 Scan loop iniciado")
    while True:
        try:
            if G.running and EX.ex:
                with G.lock:
                    opps = scan_all()
                    G.opportunities  = opps
                    G.stats["scans"] += 1
                    G.stats["last_scan"] = time.time()
                if CFG["auto_trade"]:
                    for opp in opps[:1]:
                        execute_trade(opp)
            if G.stats["scans"] % 5 == 0 and EX.ex:
                for ex in list(EX.ex.keys()):
                    EX.get_balance(ex)
        except Exception as e:
            G.stats["errors"] += 1
            log.error("Scan: %s", e)
        time.sleep(SCAN_INTERVAL)

def keep_alive():
    time.sleep(60)
    while True:
        try: requests.get(f"{RENDER_URL}/health", timeout=8)
        except: pass
        time.sleep(270)

# ── Flask ────────────────────────────────────────────────────
app = Flask(__name__)

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

/* HEADER */
header{background:linear-gradient(135deg,#1a1f35,#0d1117);
       border-bottom:1px solid var(--border);padding:14px 18px;
       display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
header h1{font-size:1.15em;color:var(--accent)}
header p{font-size:.72em;color:var(--muted);margin-top:2px}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{padding:3px 10px;border-radius:20px;font-size:.73em;font-weight:600}
.bg{background:#1a3a2a;color:var(--green)}
.bb{background:#1e2a4a;color:var(--blue)}
.bp{background:#2d1a4a;color:#c4b5fd}
.br{background:#3a1a1a;color:var(--red)}

/* LAYOUT */
main{max-width:1100px;margin:0 auto;padding:12px}

/* BOTÕES */
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.btn{padding:8px 14px;border-radius:8px;border:none;cursor:pointer;
     font-size:.8em;font-weight:600;transition:opacity .2s;white-space:nowrap}
.btn:hover{opacity:.8}
.btn-g{background:var(--green);color:#000}
.btn-r{background:var(--red);color:#fff}
.btn-b{background:var(--blue);color:#fff}
.btn-y{background:var(--yellow);color:#000}
.btn-p{background:#4c1d95;color:#c4b5fd}

/* KPI */
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
     gap:8px;margin-bottom:12px}
.kc{background:var(--card);border:1px solid var(--border);
    border-radius:10px;padding:12px}
.kl{font-size:.65em;color:var(--muted);text-transform:uppercase;
    letter-spacing:.6px;margin-bottom:4px}
.kv{font-size:1.6em;font-weight:700}
.ks{font-size:.65em;color:var(--muted);margin-top:3px}
.cg .kv{color:var(--green)}.cb .kv{color:var(--blue)}
.cy .kv{color:var(--yellow)}.cp .kv{color:#c4b5fd}

/* GRID */
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
@media(max-width:640px){.two{grid-template-columns:1fr}}

/* SECTION */
.sec{background:var(--card);border:1px solid var(--border);
     border-radius:10px;padding:14px;margin-bottom:10px}
.st{font-size:.83em;color:#c4b5fd;font-weight:600;margin-bottom:10px;
    padding-bottom:6px;border-bottom:1px solid var(--border)}

/* EXCHANGES */
.eg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ec{background:var(--bg);border:1px solid var(--border);
    border-radius:8px;padding:12px;text-align:center}
.en{font-size:.88em;font-weight:700;margin-bottom:4px}
.eb{font-size:1.35em;font-weight:700;color:var(--green)}
.es{font-size:.68em;color:var(--muted);margin-top:3px}
.dot-g::before{content:"● ";color:var(--green)}
.dot-r::before{content:"● ";color:var(--red)}

/* CONFIG TABLE */
.ct{width:100%;border-collapse:collapse;font-size:.8em}
.ct td{padding:6px 4px;border-bottom:1px solid var(--border)}
.ct tr:last-child td{border-bottom:none}
.ct td:first-child{color:var(--muted);width:45%}

/* OPORTUNIDADES */
.opp{background:var(--bg);border:1px solid var(--border);
     border-left:3px solid var(--green);border-radius:8px;
     padding:11px;margin-bottom:8px}
.opp.hot{border-left-color:var(--yellow)}
.oh{display:flex;justify-content:space-between;align-items:center;
    flex-wrap:wrap;gap:4px;margin-bottom:6px}
.xt{background:#1f2d45;padding:2px 8px;border-radius:10px;
    font-size:.7em;font-weight:600}
.pp{font-size:1.1em;font-weight:700;color:var(--green)}
.pp.hot{color:var(--yellow)}
.pu{color:var(--yellow);font-size:.8em}
.rt{font-family:monospace;font-size:.73em;color:var(--muted);
    background:#090d1a;padding:5px 8px;border-radius:5px;margin-bottom:4px}
.sp{font-family:monospace;font-size:.68em;color:#9ca3af}
.sp div{padding:1px 0}

/* TABELA HISTÓRICO */
.tbl{width:100%;border-collapse:collapse;font-size:.78em}
.tbl th,.tbl td{padding:7px 8px;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:600}
.tbl tr:last-child td{border-bottom:none}
.pos{color:var(--green)}.neg{color:var(--red)}
.pl{padding:2px 7px;border-radius:6px;font-size:.73em;font-weight:600}
.ok2{background:#0a2a1a;color:var(--green)}
.fail{background:#2a0a0a;color:var(--red)}

/* EMPTY */
.empty{text-align:center;padding:26px;color:var(--muted);
       background:var(--bg);border-radius:8px;border:1px dashed var(--border)}

/* MODAL */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
         z-index:200;align-items:center;justify-content:center;padding:16px}
.overlay.show{display:flex}
.box{background:var(--card);border:1px solid var(--border);
     border-radius:14px;padding:22px;width:100%;max-width:420px}
.box h2{font-size:1em;color:#c4b5fd;margin-bottom:16px}
.ig{margin-bottom:12px}
.ig label{font-size:.75em;color:var(--muted);display:block;margin-bottom:4px}
.ig input{width:100%;background:var(--bg);border:1px solid var(--border);
          border-radius:6px;padding:9px 11px;color:var(--text);
          font-size:.85em;font-family:monospace}
.ig input:focus{outline:1px solid var(--accent)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mbtns{display:flex;gap:8px;margin-top:16px}
.msg{padding:8px 12px;border-radius:6px;font-size:.78em;margin-top:10px;display:none}
.msg.ok{background:#0a2a1a;color:var(--green);display:block}
.msg.err{background:#2a0a0a;color:var(--red);display:block}

/* FOOTER */
footer{text-align:center;padding:10px;color:var(--muted);font-size:.7em}
.prog{height:2px;background:var(--border);border-radius:2px;overflow:hidden;margin-top:4px}
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
    <span class="badge bp" id="hmd">👁️ —</span>
    <span class="badge bp" id="hvl">💵 —</span>
  </div>
</header>

<main>

  <!-- CONTROLOS -->
  <div class="btns">
    <button class="btn btn-g" onclick="act('start')">▶️ Iniciar</button>
    <button class="btn btn-r" onclick="act('stop')">⏹️ Parar</button>
    <button class="btn btn-b" onclick="act('scan')">🔍 Scan</button>
    <button class="btn btn-p" onclick="openModal('cfg')">⚙️ Configurar</button>
    <button class="btn btn-p" onclick="openModal('keys')">🔑 Chaves API</button>
    <button class="btn btn-y" id="btn-auto" onclick="act('toggle_auto')">🤖 Auto OFF</button>
  </div>

  <!-- KPIs -->
  <div class="kpi">
    <div class="kc cb"><div class="kl">🔄 Scans</div>
      <div class="kv" id="ksc">—</div><div class="ks">Total</div></div>
    <div class="kc cp"><div class="kl">💡 Oportunidades</div>
      <div class="kv" id="kop">—</div><div class="ks">Detectadas</div></div>
    <div class="kc cg"><div class="kl">✅ Trades OK</div>
      <div class="kv" id="ktk">—</div><div class="ks" id="ktf">—</div></div>
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
      <div class="st">⚙️ Configuração</div>
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
    <div class="st">🎯 Oportunidades <span id="oct" style="color:var(--muted);font-weight:400"></span></div>
    <div id="opls"><div class="empty">🔍 A aguardar scan...</div></div>
  </div>

  <!-- Histórico -->
  <div class="sec">
    <div class="st">📜 Histórico de Trades</div>
    <div id="hist"><div class="empty">Sem trades executados</div></div>
  </div>

</main>

<!-- MODAL: CHAVES API -->
<div class="overlay" id="modal-keys">
  <div class="box">
    <h2>🔑 Chaves API</h2>
    <div class="ig"><label>Bybit — API Key</label>
      <input id="byk" type="password" placeholder="Bybit API Key"></div>
    <div class="ig"><label>Bybit — Secret</label>
      <input id="bys" type="password" placeholder="Bybit Secret"></div>
    <div class="ig"><label>Binance — API Key</label>
      <input id="bnk" type="password" placeholder="Binance API Key"></div>
    <div class="ig"><label>Binance — Secret</label>
      <input id="bns" type="password" placeholder="Binance Secret"></div>
    <div class="mbtns">
      <button class="btn btn-g" onclick="saveKeys()">✅ Guardar e Conectar</button>
      <button class="btn btn-r" onclick="closeModal('keys')">❌ Fechar</button>
    </div>
    <div class="msg" id="msg-keys"></div>
  </div>
</div>

<!-- MODAL: CONFIGURAÇÕES -->
<div class="overlay" id="modal-cfg">
  <div class="box">
    <h2>⚙️ Configurações</h2>
    <div class="row">
      <div class="ig"><label>Volume USDT (min 10)</label>
        <input id="inp-amt" type="number" min="10" step="1" value="10"></div>
      <div class="ig"><label>Lucro Mínimo %</label>
        <input id="inp-pft" type="number" min="0.1" step="0.1" value="0.3"></div>
    </div>
    <div class="mbtns">
      <button class="btn btn-g" onclick="saveCfg()">✅ Guardar</button>
      <button class="btn btn-r" onclick="closeModal('cfg')">❌ Fechar</button>
    </div>
    <div class="msg" id="msg-cfg"></div>
  </div>
</div>

<footer>
  Actualiza a cada 15s
  <div class="prog"><div class="pf"></div></div>
</footer>

<script>
const f2=n=>Number(n).toFixed(2),f3=n=>Number(n).toFixed(3),f4=n=>Number(n).toFixed(4);
const ago=ts=>{const s=Math.round(Date.now()/1e3-ts);
  return s<60?s+'s':s<3600?Math.round(s/60)+'m':Math.round(s/3600)+'h'};
const upt=s=>{const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m'};

function openModal(id){document.getElementById('modal-'+id).classList.add('show')}
function closeModal(id){document.getElementById('modal-'+id).classList.remove('show')}

function showMsg(id,text,ok){
  const el=document.getElementById('msg-'+id);
  el.textContent=text;
  el.className='msg '+(ok?'ok':'err');
  setTimeout(()=>el.className='msg',3000);
}

async function act(action,data={}){
  try{
    await fetch('/control',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,...data})});
    setTimeout(load,600);
  }catch(e){console.warn(e)}
}

async function saveKeys(){
  const body={
    bybit_key:   document.getElementById('byk').value.trim(),
    bybit_secret:document.getElementById('bys').value.trim(),
    binance_key: document.getElementById('bnk').value.trim(),
    binance_secret:document.getElementById('bns').value.trim(),
  };
  if(!body.bybit_key && !body.binance_key){
    showMsg('keys','Insere pelo menos uma chave API',false); return;
  }
  try{
    const r=await fetch('/save_keys',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){showMsg('keys','✅ Chaves guardadas! A conectar...',true);setTimeout(()=>{closeModal('keys');load()},1500)}
    else showMsg('keys','❌ Erro: '+d.error,false);
  }catch(e){showMsg('keys','❌ Erro de ligação',false)}
}

async function saveCfg(){
  const amount=parseFloat(document.getElementById('inp-amt').value);
  const profit=parseFloat(document.getElementById('inp-pft').value);
  if(amount<10){showMsg('cfg','❌ Mínimo 10 USDT',false);return}
  await act('settings',{amount,profit});
  showMsg('cfg','✅ Guardado!',true);
  setTimeout(()=>closeModal('cfg'),1000);
}

async function load(){
  try{
    const r=await fetch('/api');
    const d=await r.json();
    render(d);
  }catch(e){console.warn(e)}
}

function render(d){
  const s=d.stats||{},ex=d.exchanges||{},
        op=d.opportunities||[],tr=d.trades||[],cf=d.config||{};

  // Header
  document.getElementById('hst').textContent=s.running?'⬤ ACTIVO':'⬤ PARADO';
  document.getElementById('hst').className='badge '+(s.running?'bg live':'br');
  document.getElementById('hsc').textContent=s.last_scan?'🔄 '+ago(s.last_scan)+' atrás':'🔄 —';
  document.getElementById('hmd').textContent=cf.auto_trade?'🤖 Auto-Trade':'👁️ Monitor';
  document.getElementById('hvl').textContent='💵 $'+(cf.amount||0);

  // KPIs
  document.getElementById('ksc').textContent=s.scans||0;
  document.getElementById('kop').textContent=s.opps_found||0;
  document.getElementById('ktk').textContent=s.trades_ok||0;
  document.getElementById('ktf').textContent=(s.trades_fail||0)+' falhados';
  document.getElementById('kpr').textContent='$'+f4(s.total_profit||0);
  document.getElementById('ker').textContent=s.errors||0;
  if(s.start_time)
    document.getElementById('kup').textContent=upt(Math.round(Date.now()/1e3-s.start_time));

  // Botão auto
  document.getElementById('btn-auto').textContent=cf.auto_trade?'🤖 Auto ON':'🤖 Auto OFF';

  // Exchanges
  const eg=document.getElementById('exg');
  eg.innerHTML=Object.entries(ex).map(([n,i])=>`
    <div class="ec">
      <div class="en ${i.connected?'dot-g':'dot-r'}">${n.toUpperCase()}</div>
      <div class="eb">${i.connected?'$'+f2(i.balance||0):'—'}</div>
      <div class="es">${i.connected?'✅ Conectado':'❌ '+((i.error||'Sem chave').slice(0,40))}</div>
    </div>`).join('');

  // Config
  document.getElementById('cvl').textContent='$'+f2(cf.amount||0)+' USDT';
  document.getElementById('cpf').textContent=f2(cf.min_profit||0)+'%';
  document.getElementById('cat').innerHTML=cf.auto_trade
    ?'<span class="pl ok2">ON ✅</span>':'<span class="pl fail">OFF ❌</span>';
  document.getElementById('cit').textContent=(cf.scan_interval||15)+'s';
  document.getElementById('ctr').textContent=(cf.triangles||0)+' pares';
  document.getElementById('cls').textContent=s.last_scan?ago(s.last_scan)+' atrás':'—';
  document.getElementById('inp-amt').value=cf.amount||10;
  document.getElementById('inp-pft').value=cf.min_profit||0.3;

  // Oportunidades
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
          <span style="font-size:.68em;color:var(--muted)">${ago(o.timestamp)} atrás</span>
        </div>
        <div class="rt">🔄 ${o.route}</div>
        <div class="sp">${st}</div>
        <div style="font-size:.66em;color:var(--muted);margin-top:4px">
          Vol: $${f2(o.amount)} · ID: ${o.id}
        </div>
      </div>`;
    }).join('');
  }else{
    od.innerHTML='<div class="empty">🔍 Sem oportunidades lucrativas neste momento</div>';
  }

  // Histórico
  const hw=document.getElementById('hist');
  if(tr.length){
    const rows=[...tr].reverse().slice(0,20).map(t=>`
      <tr>
        <td>${new Date((t.ts||0)*1e3).toLocaleTimeString('pt-PT')}</td>
        <td>${(t.exchange||'').toUpperCase()}</td>
        <td style="font-family:monospace;font-size:.76em">${t.route||'—'}</td>
        <td class="${(t.profit_usdt||0)>=0?'pos':'neg'}">
          ${(t.profit_usdt||0)>=0?'+':''}$${f4(t.profit_usdt||0)}</td>
        <td class="${(t.profit_pct||0)>=0?'pos':'neg'}">${f3(t.profit_pct||0)}%</td>
        <td><span class="pl ${t.success?'ok2':'fail'}">${t.success?'OK':'FAIL'}</span></td>
      </tr>`).join('');
    hw.innerHTML=`<table class="tbl">
      <thead><tr><th>Hora</th><th>Exchange</th><th>Rota</th>
      <th>Lucro</th><th>%</th><th>Estado</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
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

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api")
def api():
    return jsonify({
        "stats":         {**G.stats, "running": G.running},
        "exchanges":     G.exchange_status,
        "opportunities": G.opportunities[:10],
        "trades":        G.trade_history[-50:],
        "config": {
            "amount":        CFG["amount"],
            "min_profit":    CFG["min_profit"],
            "auto_trade":    CFG["auto_trade"],
            "scan_interval": SCAN_INTERVAL,
            "triangles":     len(TRIANGLES),
        },
    })

@app.route("/save_keys", methods=["POST"])
def save_keys():
    global CFG
    try:
        data = request.get_json() or {}
        if data.get("bybit_key"):    CFG["bybit_key"]      = data["bybit_key"]
        if data.get("bybit_secret"): CFG["bybit_secret"]   = data["bybit_secret"]
        if data.get("binance_key"):  CFG["binance_key"]    = data["binance_key"]
        if data.get("binance_secret"):CFG["binance_secret"]= data["binance_secret"]
        save_cfg(CFG)
        EX.connect()
        EX.load_markets()
        for ex in list(EX.ex.keys()):
            EX.get_balance(ex)
        G.running = True
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/control", methods=["POST"])
def control():
    global CFG
    data   = request.get_json() or {}
    action = data.get("action","")
    if action == "start":
        G.running = True
    elif action == "stop":
        G.running = False
    elif action == "scan":
        threading.Thread(target=lambda:[
            scan_all().__class__,
            setattr(G,'opportunities', scan_all())
        ], daemon=True).start()
    elif action == "toggle_auto":
        CFG["auto_trade"] = not CFG["auto_trade"]
        save_cfg(CFG)
    elif action == "settings":
        amt = float(data.get("amount", CFG["amount"]))
        pft = float(data.get("profit", CFG["min_profit"]))
        if amt >= 10:    CFG["amount"]     = amt
        if 0.05<=pft<=20: CFG["min_profit"] = pft
        save_cfg(CFG)
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})

# ── Main ─────────────────────────────────────────────────────
def main():
    log.info("🚀 A iniciar...")
    if CFG.get("bybit_key") or CFG.get("binance_key"):
        EX.connect()
        EX.load_markets()
        for ex in list(EX.ex.keys()):
            EX.get_balance(ex)
        G.running = True
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    log.info("🌐 Dashboard porta %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
