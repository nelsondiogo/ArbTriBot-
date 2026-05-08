#!/usr/bin/env python3
import os, time, logging, threading, hashlib, json
from typing import List, Optional
from pathlib import Path
import requests, ccxt
from flask import Flask, jsonify, render_template_string, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PORT          = int(os.getenv("PORT", "10000"))
RENDER_URL    = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000")
SCAN_INTERVAL = 15
TRIANGLES = [
    ["USDT","BTC","ETH"],  ["USDT","BTC","BNB"],
    ["USDT","BTC","SOL"],  ["USDT","BTC","XRP"],
    ["USDT","BTC","ADA"],  ["USDT","BTC","DOGE"],
    ["USDT","ETH","BNB"],  ["USDT","ETH","SOL"],
    ["USDT","ETH","LINK"],
]
FEES = {"bybit": 0.001, "binance": 0.001}

CFG_FILE = Path("config.json")

def load_cfg():
    if CFG_FILE.exists():
        try: return json.loads(CFG_FILE.read_text())
        except: pass
    return {"bybit_key":"","bybit_secret":"","binance_key":"","binance_secret":"",
            "amount":10.0,"min_profit":0.3,"auto_trade":False}

def save_cfg(c):
    CFG_FILE.write_text(json.dumps(c, indent=2))

CFG = load_cfg()

class State:
    def __init__(self):
        self.running = False
        self.opportunities: List[dict] = []
        self.trade_history: List[dict] = []
        self.lock = threading.Lock()
        self.stats = {"scans":0,"opps_found":0,"trades_ok":0,"trades_fail":0,
                      "total_profit":0.0,"errors":0,"last_scan":None,"start_time":time.time()}
        self.exchange_status = {
            "bybit":   {"connected":False,"balance":0.0,"error":"Chave não configurada"},
            "binance": {"connected":False,"balance":0.0,"error":"Chave não configurada"},
        }

G = State()

class Exchanges:
    def __init__(self):
        self.ex:dict = {}
        self.markets:dict = {}

    def connect(self):
        self.ex = {}
        self.markets = {}
        for name, cls, key, secret in [
            ("bybit",   ccxt.bybit,   CFG["bybit_key"],   CFG["bybit_secret"]),
            ("binance", ccxt.binance, CFG["binance_key"], CFG["binance_secret"]),
        ]:
            G.exchange_status[name] = {"connected":False,"balance":0.0,"error":""}
            if not key or not secret:
                G.exchange_status[name]["error"] = "Chave não configurada"
                continue
            try:
                self.ex[name] = cls({
                    "apiKey":key,"secret":secret,
                    "enableRateLimit":True,
                    "options":{"defaultType":"spot"},
                })
                log.info("✅ %s conectado", name)
            except Exception as e:
                G.exchange_status[name]["error"] = str(e)

    def load_markets(self):
        for name, ex in self.ex.items():
            try:
                self.markets[name] = ex.load_markets()
                G.exchange_status[name]["connected"] = True
            except Exception as e:
                G.exchange_status[name]["error"] = str(e)

    def has_pair(self, ex, sym):
        return sym in self.markets.get(ex, {})

    def best_ask(self, ex, sym):
        try:
            ob = self.ex[ex].fetch_order_book(sym, 3)
            a  = ob.get("asks", [])
            return float(a[0][0]) if a else None
        except: return None

    def best_bid(self, ex, sym):
        try:
            ob = self.ex[ex].fetch_order_book(sym, 3)
            b  = ob.get("bids", [])
            return float(b[0][0]) if b else None
        except: return None

    def get_balance(self, name):
        try:
            b    = self.ex[name].fetch_balance()
            usdt = float(b.get("USDT",{}).get("free", 0.0))
            G.exchange_status[name]["balance"]   = round(usdt, 2)
            G.exchange_status[name]["connected"] = True
            G.exchange_status[name]["error"]     = ""
            return b
        except Exception as e:
            G.exchange_status[name]["error"] = str(e)
            return {}

    def execute_order(self, ex, sym, side, amount):
        try:
            return {"ok":True,"order":self.ex[ex].create_market_order(sym, side, amount)}
        except Exception as e:
            return {"ok":False,"error":str(e)}

EX = Exchanges()

def _step(ex, base, quote, amt):
    fee  = FEES.get(ex, 0.001)
    sym1 = f"{base}/{quote}"
    sym2 = f"{quote}/{base}"
    if EX.has_pair(ex, sym1):
        p = EX.best_ask(ex, sym1)
        if p: return {"pair":sym1,"side":"buy","price":p,"in":amt,"out":(amt/p)*(1-fee)}
    if EX.has_pair(ex, sym2):
        p = EX.best_bid(ex, sym2)
        if p: return {"pair":sym2,"side":"sell","price":p,"in":amt,"out":amt*p*(1-fee)}
    return None

def calc_triangle(ex, tri, amount):
    A,B,C = tri
    best  = None
    for nodes in ([A,B,C,A],[A,C,B,A]):
        cur,steps,fail = amount,[],False
        for i in range(3):
            s = _step(ex, nodes[i+1], nodes[i], cur)
            if not s: fail=True; break
            steps.append(s); cur = s["out"]
        if fail: continue
        pct = ((cur-amount)/amount)*100
        if pct < CFG["min_profit"]: continue
        route = "→".join(nodes)
        opp = {"exchange":ex,"route":route,"steps":steps,"amount":amount,
               "profit_pct":pct,"profit_usdt":cur-amount,"timestamp":time.time(),
               "id":hashlib.md5(f"{ex}{route}{round(pct,2)}".encode()).hexdigest()[:8]}
        if best is None or pct > best["profit_pct"]: best = opp
    return best

def scan_all():
    results = []
    for ex in list(EX.ex.keys()):
        for tri in TRIANGLES:
            try:
                opp = calc_triangle(ex, tri, CFG["amount"])
                if opp: results.append(opp); G.stats["opps_found"] += 1
            except: G.stats["errors"] += 1
    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results

def execute_trade(opp):
    for step in opp["steps"]:
        r = EX.execute_order(opp["exchange"],step["pair"],step["side"],step["in"])
        if not r["ok"]:
            G.stats["trades_fail"] += 1
            G.trade_history.append({**opp,"success":False,"error":r["error"],"ts":time.time()})
            return
        time.sleep(0.15)
    G.stats["trades_ok"]    += 1
    G.stats["total_profit"] += opp["profit_usdt"]
    G.trade_history.append({**opp,"success":True,"ts":time.time()})

def scan_loop():
    while True:
        try:
            if G.running and EX.ex:
                with G.lock:
                    opps = scan_all()
                    G.opportunities  = opps
                    G.stats["scans"] += 1
                    G.stats["last_scan"] = time.time()
                if CFG["auto_trade"]:
                    for opp in opps[:1]: execute_trade(opp)
            if G.stats["scans"] % 5 == 0:
                for ex in list(EX.ex.keys()): EX.get_balance(ex)
        except Exception as e:
            G.stats["errors"] += 1; log.error("Scan: %s", e)
        time.sleep(SCAN_INTERVAL)

def keep_alive():
    time.sleep(60)
    while True:
        try: requests.get(f"{RENDER_URL}/health", timeout=8)
        except: pass
        time.sleep(270)

# ═══════════════════════════════════════════════════════
# FLASK + HTML
# ═══════════════════════════════════════════════════════
app = Flask(__name__)

HTML = r"""
<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Arbitrage Bot</title>
<style>
:root{
  --bg:#07090f;--card:#0f1422;--card2:#151c2e;--border:#1e2d48;
  --accent:#7c3aed;--green:#10b981;--red:#ef4444;
  --yellow:#f59e0b;--blue:#3b82f6;--text:#e2e8f0;--muted:#64748b;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--text);
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     min-height:100vh;padding-bottom:80px}

/* ── NAV BOTTOM ── */
.nav{
  position:fixed;bottom:0;left:0;right:0;
  background:#0f1422;border-top:1px solid var(--border);
  display:flex;z-index:100;
}
.nav-btn{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:10px 4px 14px;
  font-size:.62em;color:var(--muted);cursor:pointer;
  border:none;background:none;transition:color .2s;gap:4px;
}
.nav-btn.active{color:var(--accent)}
.nav-btn span{font-size:1.5em;line-height:1}

/* ── PAGES ── */
.page{display:none;padding:14px}
.page.active{display:block}

/* ── HEADER ── */
.topbar{
  background:linear-gradient(135deg,#131929,#0a0e1a);
  border-bottom:1px solid var(--border);
  padding:12px 16px;
  display:flex;align-items:center;justify-content:space-between;
}
.topbar h1{font-size:1.05em;color:var(--accent);font-weight:700}
.topbar p{font-size:.65em;color:var(--muted);margin-top:1px}
.status-dot{
  width:10px;height:10px;border-radius:50%;
  background:var(--red);flex-shrink:0;
}
.status-dot.on{background:var(--green);
  box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── CARDS KPI ── */
.kpi-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.kpi-card{background:var(--card);border:1px solid var(--border);
          border-radius:12px;padding:14px}
.kpi-label{font-size:.63em;color:var(--muted);text-transform:uppercase;
           letter-spacing:.7px;margin-bottom:5px}
.kpi-val{font-size:1.8em;font-weight:700;line-height:1}
.kpi-sub{font-size:.65em;color:var(--muted);margin-top:4px}
.c-green .kpi-val{color:var(--green)}
.c-blue  .kpi-val{color:var(--blue)}
.c-yellow.kpi-val{color:var(--yellow)}
.c-purple.kpi-val{color:#c4b5fd}

/* ── SECTION ── */
.sec{background:var(--card);border:1px solid var(--border);
     border-radius:12px;padding:14px;margin-bottom:12px}
.sec-title{font-size:.8em;font-weight:700;color:#c4b5fd;
           margin-bottom:12px;padding-bottom:8px;
           border-bottom:1px solid var(--border);
           display:flex;align-items:center;gap:6px}

/* ── EXCHANGE CARDS ── */
.ex-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ex-card{background:var(--card2);border:1px solid var(--border);
         border-radius:10px;padding:14px;text-align:center}
.ex-name{font-size:.85em;font-weight:700;margin-bottom:6px;
         display:flex;align-items:center;justify-content:center;gap:5px}
.ex-bal{font-size:1.5em;font-weight:700;color:var(--green);margin:4px 0}
.ex-status{font-size:.68em;margin-top:4px}
.ex-err{font-size:.62em;color:var(--red);margin-top:4px;
        word-break:break-word;text-align:center}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.g{background:var(--green)}
.dot.r{background:var(--red)}

/* ── OPORTUNIDADE ── */
.opp-card{background:var(--card2);border:1px solid var(--border);
          border-left:3px solid var(--green);border-radius:10px;
          padding:12px;margin-bottom:10px}
.opp-card.hot{border-left-color:var(--yellow)}
.opp-top{display:flex;justify-content:space-between;
         align-items:center;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.opp-ex{background:#1e2d48;padding:2px 9px;border-radius:10px;
        font-size:.7em;font-weight:600}
.opp-pct{font-size:1.15em;font-weight:700;color:var(--green)}
.opp-pct.hot{color:var(--yellow)}
.opp-usdt{color:var(--yellow);font-size:.78em}
.opp-route{font-family:monospace;font-size:.72em;background:var(--bg);
           color:var(--muted);padding:5px 8px;border-radius:6px;margin-bottom:5px}
.opp-steps{font-family:monospace;font-size:.68em;color:#94a3b8}
.opp-steps div{padding:1px 0}

/* ── TABELA ── */
.tbl-wrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:.75em;min-width:400px}
.tbl th,.tbl td{padding:8px 10px;text-align:left;
                border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:600;white-space:nowrap}
.pos{color:var(--green)}.neg{color:var(--red)}
.pill{padding:2px 7px;border-radius:6px;font-size:.8em;font-weight:600}
.pill.ok{background:#0a2a1a;color:var(--green)}
.pill.fail{background:#2a0a0a;color:var(--red)}

/* ── EMPTY ── */
.empty{text-align:center;padding:30px 16px;color:var(--muted);
       background:var(--card2);border-radius:10px;
       border:1px dashed var(--border);font-size:.85em}

/* ── FORM ── */
.form-group{margin-bottom:14px}
.form-group label{font-size:.75em;color:var(--muted);
                  display:block;margin-bottom:5px;font-weight:500}
.form-input{width:100%;background:var(--card2);border:1px solid var(--border);
            border-radius:8px;padding:11px 13px;color:var(--text);
            font-size:.88em;font-family:monospace;transition:border .2s}
.form-input:focus{outline:none;border-color:var(--accent)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}

/* ── BOTÕES ── */
.btn{padding:12px 18px;border-radius:10px;border:none;cursor:pointer;
     font-size:.85em;font-weight:700;transition:opacity .15s;
     display:flex;align-items:center;justify-content:center;gap:6px}
.btn:active{opacity:.7}
.btn-green{background:var(--green);color:#000;width:100%;margin-bottom:8px}
.btn-red{background:var(--red);color:#fff;width:100%;margin-bottom:8px}
.btn-blue{background:var(--blue);color:#fff;width:100%;margin-bottom:8px}
.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn-sm{padding:9px;font-size:.8em;border-radius:8px;border:none;
        cursor:pointer;font-weight:600;width:100%}
.btn-sm.g{background:var(--green);color:#000}
.btn-sm.r{background:var(--red);color:#fff}
.btn-sm.b{background:var(--blue);color:#fff}
.btn-sm.y{background:var(--yellow);color:#000}
.btn-sm.p{background:#4c1d95;color:#c4b5fd}

/* ── TOGGLE ── */
.toggle-row{display:flex;align-items:center;justify-content:space-between;
            padding:12px 0;border-bottom:1px solid var(--border)}
.toggle-row:last-child{border-bottom:none}
.toggle-label{font-size:.85em}
.toggle-sub{font-size:.7em;color:var(--muted);margin-top:2px}
.toggle{position:relative;width:46px;height:26px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#2d3748;border-radius:26px;
        cursor:pointer;transition:.3s}
.slider::before{content:"";position:absolute;width:20px;height:20px;
                left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
input:checked+.slider{background:var(--green)}
input:checked+.slider::before{transform:translateX(20px)}

/* ── ALERT ── */
.alert{padding:10px 14px;border-radius:8px;font-size:.78em;
       margin-bottom:10px;display:none;font-weight:500}
.alert.ok{background:#0a2a1a;color:var(--green);border:1px solid var(--green);display:block}
.alert.err{background:#2a0a0a;color:var(--red);border:1px solid var(--red);display:block}

/* ── STATS INFO ── */
.info-row{display:flex;justify-content:space-between;align-items:center;
          padding:10px 0;border-bottom:1px solid var(--border);font-size:.83em}
.info-row:last-child{border-bottom:none}
.info-label{color:var(--muted)}
.info-val{font-weight:600}

/* ── PAGE TITLE ── */
.page-title{font-size:1em;font-weight:700;color:#c4b5fd;margin-bottom:14px}
</style>
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div>
    <h1>🤖 Arbitrage Bot</h1>
    <p>Bybit &amp; Binance · Triangular</p>
  </div>
  <div id="top-dot" class="status-dot"></div>
</div>

<!-- ═══════════════════════════════════════ -->
<!-- PÁGINA 1 — DASHBOARD                   -->
<!-- ═══════════════════════════════════════ -->
<div class="page active" id="page-home">

  <!-- KPIs -->
  <div style="height:12px"></div>
  <div class="kpi-grid">
    <div class="kpi-card c-blue">
      <div class="kpi-label">🔄 Scans</div>
      <div class="kpi-val" id="k-sc">0</div>
      <div class="kpi-sub">Total realizados</div>
    </div>
    <div class="kpi-card" style="border-left:2px solid #c4b5fd">
      <div class="kpi-label">💡 Oportunidades</div>
      <div class="kpi-val" style="color:#c4b5fd" id="k-op">0</div>
      <div class="kpi-sub">Detectadas</div>
    </div>
    <div class="kpi-card c-green">
      <div class="kpi-label">✅ Trades OK</div>
      <div class="kpi-val" id="k-tok">0</div>
      <div class="kpi-sub" id="k-fail">0 falhados</div>
    </div>
    <div class="kpi-card" style="border-left:2px solid var(--yellow)">
      <div class="kpi-label">💵 Lucro Total</div>
      <div class="kpi-val" style="color:var(--yellow)" id="k-profit">$0.00</div>
      <div class="kpi-sub">USDT</div>
    </div>
  </div>

  <!-- Exchanges -->
  <div class="sec">
    <div class="sec-title">🏦 Exchanges</div>
    <div class="ex-grid" id="ex-grid">
      <div class="ex-card"><div class="ex-name">A carregar...</div></div>
    </div>
  </div>

  <!-- Oportunidades -->
  <div class="sec">
    <div class="sec-title">
      🎯 Oportunidades
      <span id="opp-count" style="color:var(--muted);font-size:.85em;font-weight:400"></span>
    </div>
    <div id="opp-list">
      <div class="empty">🔍 A aguardar scan...</div>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════ -->
<!-- PÁGINA 2 — HISTÓRICO                   -->
<!-- ═══════════════════════════════════════ -->
<div class="page" id="page-history">
  <div style="height:12px"></div>
  <div class="page-title">📜 Histórico de Trades</div>
  <div class="sec">
    <div id="history-wrap">
      <div class="empty">Sem trades executados ainda</div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════ -->
<!-- PÁGINA 3 — CONTROLO                    -->
<!-- ═══════════════════════════════════════ -->
<div class="page" id="page-control">
  <div style="height:12px"></div>
  <div class="page-title">🎮 Controlo do Bot</div>

  <!-- Estado -->
  <div class="sec">
    <div class="sec-title">Estado Actual</div>
    <div class="info-row">
      <span class="info-label">Status</span>
      <span class="info-val" id="ctrl-status">—</span>
    </div>
    <div class="info-row">
      <span class="info-label">Modo</span>
      <span class="info-val" id="ctrl-mode">—</span>
    </div>
    <div class="info-row">
      <span class="info-label">Último Scan</span>
      <span class="info-val" id="ctrl-scan">—</span>
    </div>
    <div class="info-row">
      <span class="info-label">Uptime</span>
      <span class="info-val" id="ctrl-uptime">—</span>
    </div>
    <div class="info-row">
      <span class="info-label">Erros</span>
      <span class="info-val" id="ctrl-errors">—</span>
    </div>
  </div>

  <!-- Botões -->
  <div class="sec">
    <div class="sec-title">Acções</div>
    <button class="btn btn-green" onclick="act('start')">▶️ Iniciar Bot</button>
    <button class="btn btn-red"   onclick="act('stop')">⏹️ Parar Bot</button>
    <button class="btn btn-blue"  onclick="doScan()">🔍 Scan Manual Agora</button>
  </div>

  <!-- Auto-Trade -->
  <div class="sec">
    <div class="sec-title">Auto-Trade</div>
    <div class="toggle-row">
      <div>
        <div class="toggle-label">🤖 Executar trades automaticamente</div>
        <div class="toggle-sub">⚠️ Usa dinheiro real. Activa com cuidado.</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="auto-toggle" onchange="toggleAuto()">
        <span class="slider"></span>
      </label>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════ -->
<!-- PÁGINA 4 — CONFIGURAÇÕES               -->
<!-- ═══════════════════════════════════════ -->
<div class="page" id="page-settings">
  <div style="height:12px"></div>
  <div class="page-title">⚙️ Configurações</div>

  <div class="sec">
    <div class="sec-title">💵 Volume de Trade</div>
    <div class="form-group">
      <label>Valor em USDT (mínimo 10)</label>
      <input class="form-input" type="number" id="s-amount" min="10" step="1" value="10">
    </div>
    <div class="form-group">
      <label>Lucro mínimo para alertar (%)</label>
      <input class="form-input" type="number" id="s-profit" min="0.1" step="0.1" value="0.3">
    </div>
    <div id="msg-cfg" class="alert"></div>
    <button class="btn btn-green" onclick="saveCfg()">✅ Guardar Configurações</button>
  </div>

  <!-- Seleção rápida -->
  <div class="sec">
    <div class="sec-title">⚡ Seleção Rápida — Volume</div>
    <div class="btn-row">
      <button class="btn-sm g" onclick="setAmt(10)">$10</button>
      <button class="btn-sm g" onclick="setAmt(25)">$25</button>
      <button class="btn-sm g" onclick="setAmt(50)">$50</button>
      <button class="btn-sm g" onclick="setAmt(100)">$100</button>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">⚡ Seleção Rápida — Lucro Mínimo</div>
    <div class="btn-row">
      <button class="btn-sm b" onclick="setPft(0.2)">0.2%</button>
      <button class="btn-sm b" onclick="setPft(0.3)">0.3%</button>
      <button class="btn-sm b" onclick="setPft(0.5)">0.5%</button>
      <button class="btn-sm b" onclick="setPft(1.0)">1.0%</button>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════ -->
<!-- PÁGINA 5 — CHAVES API                  -->
<!-- ═══════════════════════════════════════ -->
<div class="page" id="page-keys">
  <div style="height:12px"></div>
  <div class="page-title">🔑 Chaves API</div>

  <div class="sec">
    <div class="sec-title">🟡 Bybit</div>
    <div class="form-group">
      <label>API Key</label>
      <input class="form-input" type="password" id="byk" placeholder="Bybit API Key">
    </div>
    <div class="form-group">
      <label>API Secret</label>
      <input class="form-input" type="password" id="bys" placeholder="Bybit Secret">
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">🟠 Binance</div>
    <div class="form-group">
      <label>API Key</label>
      <input class="form-input" type="password" id="bnk" placeholder="Binance API Key">
    </div>
    <div class="form-group">
      <label>API Secret</label>
      <input class="form-input" type="password" id="bns" placeholder="Binance Secret">
    </div>
  </div>

  <div id="msg-keys" class="alert"></div>
  <button class="btn btn-green" onclick="saveKeys()">✅ Guardar e Conectar</button>

  <!-- Estado das chaves -->
  <div class="sec" style="margin-top:12px">
    <div class="sec-title">Estado da Ligação</div>
    <div id="key-status">
      <div class="empty">Sem chaves configuradas</div>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════ -->
<!-- NAVEGAÇÃO INFERIOR                     -->
<!-- ═══════════════════════════════════════ -->
<nav class="nav">
  <button class="nav-btn active" onclick="goTo('home',this)">
    <span>📊</span>Dashboard
  </button>
  <button class="nav-btn" onclick="goTo('history',this)">
    <span>📜</span>Histórico
  </button>
  <button class="nav-btn" onclick="goTo('control',this)">
    <span>🎮</span>Controlo
  </button>
  <button class="nav-btn" onclick="goTo('settings',this)">
    <span>⚙️</span>Config
  </button>
  <button class="nav-btn" onclick="goTo('keys',this)">
    <span>🔑</span>Chaves
  </button>
</nav>

<script>
/* ── NAVEGAÇÃO ── */
function goTo(page, btn){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  btn.classList.add('active');
  window.scrollTo(0,0);
}

/* ── FORMATTERS ── */
const f2=n=>Number(n).toFixed(2);
const f3=n=>Number(n).toFixed(3);
const f4=n=>Number(n).toFixed(4);
const ago=ts=>{
  if(!ts) return '—';
  const s=Math.round(Date.now()/1e3-ts);
  if(s<60) return s+'s atrás';
  if(s<3600) return Math.round(s/60)+'m atrás';
  return Math.round(s/3600)+'h atrás';
};
const upt=s=>{
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h+'h '+m+'m';
};

/* ── API ── */
async function act(action, data={}){
  try{
    await fetch('/control',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,...data})});
    setTimeout(load,800);
  }catch(e){console.warn(e)}
}

async function doScan(){
  await act('scan');
}

async function toggleAuto(){
  await act('toggle_auto');
}

function setAmt(v){
  document.getElementById('s-amount').value=v;
}
function setPft(v){
  document.getElementById('s-profit').value=v;
}

async function saveCfg(){
  const amount=parseFloat(document.getElementById('s-amount').value)||10;
  const profit=parseFloat(document.getElementById('s-profit').value)||0.3;
  if(amount<10){showMsg('cfg','❌ Mínimo 10 USDT',false);return}
  await act('settings',{amount,profit});
  showMsg('cfg','✅ Configurações guardadas!',true);
}

function showMsg(id,text,ok){
  const el=document.getElementById('msg-'+id);
  el.textContent=text;
  el.className='alert '+(ok?'ok':'err');
  setTimeout(()=>{el.className='alert'},3000);
}

async function saveKeys(){
  const body={
    bybit_key:    document.getElementById('byk').value.trim(),
    bybit_secret: document.getElementById('bys').value.trim(),
    binance_key:  document.getElementById('bnk').value.trim(),
    binance_secret:document.getElementById('bns').value.trim(),
  };
  if(!body.bybit_key && !body.binance_key){
    showMsg('keys','❌ Insere pelo menos uma chave API',false);
    return;
  }
  try{
    const r=await fetch('/save_keys',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){
      showMsg('keys','✅ Chaves guardadas! Bot a conectar...',true);
      setTimeout(load,1500);
    }else{
      showMsg('keys','❌ '+d.error,false);
    }
  }catch(e){
    showMsg('keys','❌ Erro de ligação',false);
  }
}

/* ── RENDER ── */
async function load(){
  try{
    const r=await fetch('/api');
    const d=await r.json();
    render(d);
  }catch(e){console.warn(e)}
}

function render(d){
  const s=d.stats||{}, ex=d.exchanges||{},
        op=d.opportunities||[], tr=d.trades||[], cf=d.config||{};

  /* top dot */
  document.getElementById('top-dot').className=
    'status-dot '+(s.running?'on':'');

  /* ── KPIs ── */
  document.getElementById('k-sc').textContent     = s.scans||0;
  document.getElementById('k-op').textContent     = s.opps_found||0;
  document.getElementById('k-tok').textContent    = s.trades_ok||0;
  document.getElementById('k-fail').textContent   = (s.trades_fail||0)+' falhados';
  document.getElementById('k-profit').textContent = '$'+f4(s.total_profit||0);

  /* ── EXCHANGES ── */
  const eg=document.getElementById('ex-grid');
  eg.innerHTML=Object.entries(ex).map(([name,info])=>{
    const conn=info.connected;
    const errShort=(info.error||'').slice(0,60);
    return `
    <div class="ex-card">
      <div class="ex-name">
        <span class="dot ${conn?'g':'r'}"></span>
        ${name.toUpperCase()}
      </div>
      <div class="ex-bal">${conn?'$'+f2(info.balance||0):'—'}</div>
      <div class="ex-status" style="color:${conn?'var(--green)':'var(--red)'}">
        ${conn?'✅ Conectado':'❌ Erro'}
      </div>
      ${!conn&&errShort?`<div class="ex-err">${errShort}</div>`:''}
    </div>`;
  }).join('');

  /* ── OPORTUNIDADES ── */
  document.getElementById('opp-count').textContent=
    op.length?'('+op.length+')':'';
  const od=document.getElementById('opp-list');
  if(op.length){
    od.innerHTML=op.slice(0,10).map(o=>{
      const hot=o.profit_pct>=1;
      const steps=(o.steps||[]).map((s,i)=>
        `<div>${['1️⃣','2️⃣','3️⃣'][i]||'•'} ${(s.side||'').toUpperCase()} ${s.pair||''} @ ${Number(s.price||0).toFixed(8)}</div>`
      ).join('');
      return `
      <div class="opp-card ${hot?'hot':''}">
        <div class="opp-top">
          <span class="opp-ex">${o.exchange.toUpperCase()}</span>
          <span class="opp-pct ${hot?'hot':''}">+${f3(o.profit_pct)}%</span>
          <span class="opp-usdt">+$${f4(o.profit_usdt)}</span>
          <span style="font-size:.66em;color:var(--muted)">${ago(o.timestamp)}</span>
        </div>
        <div class="opp-route">🔄 ${o.route}</div>
        <div class="opp-steps">${steps}</div>
        <div style="font-size:.65em;color:var(--muted);margin-top:5px">
          Vol: $${f2(o.amount)} · ID: ${o.id}
        </div>
      </div>`;
    }).join('');
  }else{
    od.innerHTML='<div class="empty">🔍 Sem oportunidades lucrativas neste momento</div>';
  }

  /* ── HISTÓRICO ── */
  const hw=document.getElementById('history-wrap');
  if(tr.length){
    const rows=[...tr].reverse().slice(0,30).map(t=>`
      <tr>
        <td>${new Date((t.ts||0)*1e3).toLocaleTimeString('pt-PT')}</td>
        <td>${(t.exchange||'').toUpperCase()}</td>
        <td style="font-family:monospace">${(t.route||'—').replace(/→/g,' → ')}</td>
        <td class="${(t.profit_usdt||0)>=0?'pos':'neg'}">
          ${(t.profit_usdt||0)>=0?'+':''}$${f4(t.profit_usdt||0)}</td>
        <td><span class="pill ${t.success?'ok':'fail'}">${t.success?'OK':'FAIL'}</span></td>
      </tr>`).join('');
    hw.innerHTML=`
      <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr>
          <th>Hora</th><th>Exchange</th><th>Rota</th><th>Lucro</th><th>Estado</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }else{
    hw.innerHTML='<div class="empty">Sem trades executados ainda</div>';
  }

  /* ── CONTROLO ── */
  document.getElementById('ctrl-status').innerHTML=
    s.running
      ?'<span style="color:var(--green)">🟢 ACTIVO</span>'
      :'<span style="color:var(--red)">🔴 PARADO</span>';
  document.getElementById('ctrl-mode').textContent=
    cf.auto_trade?'🤖 Auto-Trade':'👁️ Monitor';
  document.getElementById('ctrl-scan').textContent=ago(s.last_scan);
  document.getElementById('ctrl-uptime').textContent=
    s.start_time?upt(Math.round(Date.now()/1e3-s.start_time)):'—';
  document.getElementById('ctrl-errors').textContent=s.errors||0;
  document.getElementById('auto-toggle').checked=!!cf.auto_trade;

  /* ── CONFIG inputs ── */
  if(cf.amount)  document.getElementById('s-amount').value=cf.amount;
  if(cf.min_profit) document.getElementById('s-profit').value=cf.min_profit;

  /* ── KEY STATUS ── */
  const ks=document.getElementById('key-status');
  ks.innerHTML=Object.entries(ex).map(([name,info])=>`
    <div class="info-row">
      <span class="info-label">${name.toUpperCase()}</span>
      <span class="info-val" style="color:${info.connected?'var(--green)':'var(--red)'}">
        ${info.connected?'✅ Conectado':'❌ '+(info.error||'Sem chave').slice(0,30)}
      </span>
    </div>`).join('');
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
        "stats":         {**G.stats,"running":G.running},
        "exchanges":     G.exchange_status,
        "opportunities": G.opportunities[:10],
        "trades":        G.trade_history[-50:],
        "config":{
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
        data=request.get_json() or {}
        if data.get("bybit_key"):     CFG["bybit_key"]     = data["bybit_key"]
        if data.get("bybit_secret"):  CFG["bybit_secret"]  = data["bybit_secret"]
        if data.get("binance_key"):   CFG["binance_key"]   = data["binance_key"]
        if data.get("binance_secret"):CFG["binance_secret"]= data["binance_secret"]
        save_cfg(CFG)
        EX.connect()
        EX.load_markets()
        for ex in list(EX.ex.keys()): EX.get_balance(ex)
        G.running=True
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/control", methods=["POST"])
def control():
    global CFG
    data=request.get_json() or {}
    action=data.get("action","")
    if action=="start":
        G.running=True
    elif action=="stop":
        G.running=False
    elif action=="scan":
        def _s():
            opps=scan_all()
            G.opportunities=opps
            G.stats["scans"]+=1
            G.stats["last_scan"]=time.time()
        threading.Thread(target=_s,daemon=True).start()
    elif action=="toggle_auto":
        CFG["auto_trade"]=not CFG["auto_trade"]
        save_cfg(CFG)
    elif action=="settings":
        a=float(data.get("amount",CFG["amount"]))
        p=float(data.get("profit",CFG["min_profit"]))
        if a>=10:      CFG["amount"]=a
        if .05<=p<=20: CFG["min_profit"]=p
        save_cfg(CFG)
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return jsonify({"ok":True,"ts":time.time()})

def main():
    log.info("🚀 A iniciar...")
    if CFG.get("bybit_key") or CFG.get("binance_key"):
        EX.connect(); EX.load_markets()
        for ex in list(EX.ex.keys()): EX.get_balance(ex)
        G.running=True
    threading.Thread(target=scan_loop,daemon=True).start()
    threading.Thread(target=keep_alive,daemon=True).start()
    log.info("🌐 Porta %d",PORT)
    app.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False)

if __name__=="__main__":
    main()
