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
SCAN_INTERVAL = 10
TRIANGLES = [
    ["USDT","BTC","ETH"],  ["USDT","BTC","BNB"],
    ["USDT","BTC","SOL"],  ["USDT","BTC","XRP"],
    ["USDT","BTC","ADA"],  ["USDT","BTC","DOGE"],
    ["USDT","ETH","BNB"],  ["USDT","ETH","SOL"],
    ["USDT","ETH","LINK"], ["USDT","BTC","AVAX"],
    ["USDT","ETH","MATIC"],["USDT","BTC","DOT"],
]
FEES = {"bybit": 0.001, "binance": 0.001}

CFG_FILE = Path("config.json")

def load_cfg():
    if CFG_FILE.exists():
        try: return json.loads(CFG_FILE.read_text())
        except: pass
    return {
        "bybit_key":"","bybit_secret":"",
        "binance_key":"","binance_secret":"",
        "amount": 10.0,
        "min_profit": 0.01,   # 0.01% — detecta mais oportunidades
        "auto_trade": False,
    }

def save_cfg(c):
    CFG_FILE.write_text(json.dumps(c, indent=2))

CFG = load_cfg()

class State:
    def __init__(self):
        self.running = False
        self.opportunities: List[dict] = []
        self.trade_history: List[dict] = []
        self.lock = threading.Lock()
        self.stats = {
            "scans":0,"opps_found":0,"trades_ok":0,"trades_fail":0,
            "total_profit":0.0,"errors":0,"last_scan":None,
            "start_time":time.time(),
        }
        self.exchange_status = {
            "bybit":   {"connected":False,"balance":0.0,"error":"Não configurado"},
            "binance": {"connected":False,"balance":0.0,"error":"Não configurado"},
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
                    "apiKey":key, "secret":secret,
                    "enableRateLimit":True,
                    "options":{"defaultType":"spot"},
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
                log.error("❌ mercados %s: %s", name, e)

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
        if name not in self.ex:
            return {}
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
            return {"ok":True, "order":self.ex[ex].create_market_order(sym,side,amount)}
        except Exception as e:
            return {"ok":False, "error":str(e)}

EX = Exchanges()

def _step(ex, base, quote, amt):
    fee  = FEES.get(ex, 0.001)
    sym1 = f"{base}/{quote}"
    sym2 = f"{quote}/{base}"
    if EX.has_pair(ex, sym1):
        p = EX.best_ask(ex, sym1)
        if p: return {"pair":sym1,"side":"buy","price":p,
                      "in":amt,"out":(amt/p)*(1-fee)}
    if EX.has_pair(ex, sym2):
        p = EX.best_bid(ex, sym2)
        if p: return {"pair":sym2,"side":"sell","price":p,
                      "in":amt,"out":amt*p*(1-fee)}
    return None

def calc_triangle(ex, tri, amount):
    A,B,C = tri
    best  = None
    for nodes in ([A,B,C,A],[A,C,B,A]):
        cur,steps,fail = amount,[],False
        for i in range(3):
            s = _step(ex, nodes[i+1], nodes[i], cur)
            if not s: fail=True; break
            steps.append(s); cur=s["out"]
        if fail: continue
        pct = ((cur-amount)/amount)*100
        # Guarda mesmo negativos para debug
        route = "→".join(nodes)
        opp   = {
            "exchange":ex,"route":route,"steps":steps,
            "amount":amount,"profit_pct":pct,
            "profit_usdt":cur-amount,"timestamp":time.time(),
            "id":hashlib.md5(f"{ex}{route}{round(pct,2)}".encode()).hexdigest()[:8],
        }
        if pct >= CFG["min_profit"]:
            if best is None or pct > best["profit_pct"]: best=opp
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
                    log.info("💰 Opp: %s %s +%.4f%%", ex, opp['route'], opp['profit_pct'])
            except Exception as e:
                G.stats["errors"] += 1
                log.debug("Err %s %s: %s", ex, tri, e)
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
    log.info("🔄 Scan loop iniciado")
    while True:
        try:
            if G.running and EX.ex:
                with G.lock:
                    opps             = scan_all()
                    G.opportunities  = opps
                    G.stats["scans"] += 1
                    G.stats["last_scan"] = time.time()
                if CFG["auto_trade"]:
                    for opp in opps[:1]:
                        execute_trade(opp)
            # Actualiza saldos de 5 em 5 scans
            if G.stats["scans"] > 0 and G.stats["scans"] % 5 == 0:
                for ex in list(EX.ex.keys()):
                    EX.get_balance(ex)
        except Exception as e:
            G.stats["errors"] += 1
            log.error("Scan error: %s", e)
        time.sleep(SCAN_INTERVAL)

def keep_alive():
    time.sleep(60)
    while True:
        try: requests.get(f"{RENDER_URL}/health", timeout=8)
        except: pass
        time.sleep(270)

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
.nav{position:fixed;bottom:0;left:0;right:0;background:#0f1422;
     border-top:1px solid var(--border);display:flex;z-index:100}
.nav-btn{flex:1;display:flex;flex-direction:column;align-items:center;
         justify-content:center;padding:8px 4px 12px;font-size:.6em;
         color:var(--muted);cursor:pointer;border:none;background:none;gap:3px}
.nav-btn.active{color:var(--accent)}
.nav-btn span{font-size:1.4em;line-height:1}
.page{display:none;padding:12px}
.page.active{display:block}
.topbar{background:linear-gradient(135deg,#131929,#0a0e1a);
        border-bottom:1px solid var(--border);padding:12px 16px;
        display:flex;align-items:center;justify-content:space-between}
.topbar h1{font-size:1.05em;color:var(--accent);font-weight:700}
.topbar p{font-size:.65em;color:var(--muted);margin-top:1px}
.sdot{width:10px;height:10px;border-radius:50%;background:var(--red);flex-shrink:0}
.sdot.on{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.kpi-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.kc{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px}
.kl{font-size:.62em;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.kv{font-size:1.75em;font-weight:700;line-height:1}
.ks{font-size:.63em;color:var(--muted);margin-top:4px}
.sec{background:var(--card);border:1px solid var(--border);
     border-radius:12px;padding:14px;margin-bottom:10px}
.st{font-size:.8em;font-weight:700;color:#c4b5fd;margin-bottom:10px;
    padding-bottom:7px;border-bottom:1px solid var(--border)}
.eg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.ec{background:var(--card2);border:1px solid var(--border);
    border-radius:10px;padding:12px;text-align:center}
.en{font-size:.83em;font-weight:700;margin-bottom:5px;
    display:flex;align-items:center;justify-content:center;gap:5px}
.eb{font-size:1.4em;font-weight:700;color:var(--green);margin:3px 0}
.ee{font-size:.62em;color:var(--red);margin-top:3px;word-break:break-word}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot.g{background:var(--green)}.dot.r{background:var(--red)}
.opp{background:var(--card2);border:1px solid var(--border);
     border-left:3px solid var(--green);border-radius:10px;padding:12px;margin-bottom:8px}
.opp.hot{border-left-color:var(--yellow)}
.oh{display:flex;justify-content:space-between;align-items:center;
    flex-wrap:wrap;gap:4px;margin-bottom:6px}
.ox{background:#1e2d48;padding:2px 8px;border-radius:10px;font-size:.7em;font-weight:600}
.op{font-size:1.1em;font-weight:700;color:var(--green)}
.op.hot{color:var(--yellow)}
.ou{color:var(--yellow);font-size:.78em}
.or{font-family:monospace;font-size:.72em;background:var(--bg);
    color:var(--muted);padding:5px 8px;border-radius:5px;margin-bottom:4px}
.os{font-family:monospace;font-size:.67em;color:#94a3b8}
.os div{padding:1px 0}
.tbl-w{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:.75em;min-width:380px}
.tbl th,.tbl td{padding:7px 8px;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:600}
.pos{color:var(--green)}.neg{color:var(--red)}
.pill{padding:2px 6px;border-radius:5px;font-size:.78em;font-weight:600}
.pill.ok{background:#0a2a1a;color:var(--green)}
.pill.fail{background:#2a0a0a;color:var(--red)}
.empty{text-align:center;padding:28px 16px;color:var(--muted);
       background:var(--card2);border-radius:10px;
       border:1px dashed var(--border);font-size:.83em}
.fg{margin-bottom:12px}
.fg label{font-size:.73em;color:var(--muted);display:block;margin-bottom:4px;font-weight:500}
.fi{width:100%;background:var(--card2);border:1px solid var(--border);
    border-radius:8px;padding:10px 12px;color:var(--text);
    font-size:.87em;font-family:monospace}
.fi:focus{outline:1px solid var(--accent)}
.btn{padding:12px;border-radius:10px;border:none;cursor:pointer;
     font-size:.84em;font-weight:700;width:100%;margin-bottom:8px;
     display:flex;align-items:center;justify-content:center;gap:6px}
.btn:active{opacity:.7}
.btn.g{background:var(--green);color:#000}
.btn.r{background:var(--red);color:#fff}
.btn.b{background:var(--blue);color:#fff}
.btn.p{background:#4c1d95;color:#c4b5fd}
.brow{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.bsm{padding:10px;border-radius:8px;border:none;cursor:pointer;
     font-weight:600;font-size:.8em;width:100%}
.bsm.g{background:var(--green);color:#000}
.bsm.b{background:var(--blue);color:#fff}
.irow{display:flex;justify-content:space-between;align-items:center;
      padding:10px 0;border-bottom:1px solid var(--border);font-size:.82em}
.irow:last-child{border-bottom:none}
.il{color:var(--muted)}.iv{font-weight:600}
.toggle{position:relative;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#2d3748;border-radius:24px;cursor:pointer;transition:.3s}
.sl::before{content:"";position:absolute;width:18px;height:18px;
            left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
input:checked+.sl{background:var(--green)}
input:checked+.sl::before{transform:translateX(20px)}
.alert{padding:10px 12px;border-radius:8px;font-size:.77em;
       margin-bottom:10px;display:none;font-weight:500}
.alert.ok{background:#0a2a1a;color:var(--green);border:1px solid #10b98133;display:block}
.alert.err{background:#2a0a0a;color:var(--red);border:1px solid #ef444433;display:block}
.pt{font-size:.98em;font-weight:700;color:#c4b5fd;margin-bottom:12px}

/* Info box */
.info-box{background:#1e2d48;border:1px solid #3b82f633;border-radius:10px;
          padding:12px;margin-bottom:10px;font-size:.78em;color:#93c5fd;line-height:1.5}
</style>
</head>
<body>

<div class="topbar">
  <div>
    <h1>🤖 Arbitrage Bot</h1>
    <p>Bybit &amp; Binance · Triangular</p>
  </div>
  <div id="sdot" class="sdot"></div>
</div>

<!-- ════════════════════════════════ -->
<!-- DASHBOARD                       -->
<!-- ════════════════════════════════ -->
<div class="page active" id="page-home">
  <div style="height:10px"></div>

  <div class="kpi-grid">
    <div class="kc" style="border-left:2px solid var(--blue)">
      <div class="kl">🔄 Scans</div>
      <div class="kv" style="color:var(--blue)" id="k-sc">0</div>
      <div class="ks">Total realizados</div>
    </div>
    <div class="kc" style="border-left:2px solid #c4b5fd">
      <div class="kl">💡 Oportunidades</div>
      <div class="kv" style="color:#c4b5fd" id="k-op">0</div>
      <div class="ks">Detectadas</div>
    </div>
    <div class="kc" style="border-left:2px solid var(--green)">
      <div class="kl">✅ Trades OK</div>
      <div class="kv" style="color:var(--green)" id="k-tok">0</div>
      <div class="ks" id="k-fail">0 falhados</div>
    </div>
    <div class="kc" style="border-left:2px solid var(--yellow)">
      <div class="kl">💵 Lucro Total</div>
      <div class="kv" style="color:var(--yellow)" id="k-pr">$0.0000</div>
      <div class="ks">USDT</div>
    </div>
  </div>

  <div class="sec">
    <div class="st">🏦 Exchanges &amp; Saldos</div>
    <div class="eg" id="ex-grid">
      <div class="ec"><div class="en">A carregar...</div></div>
    </div>
  </div>

  <div class="sec">
    <div class="st">🎯 Oportunidades <span id="opp-ct" style="color:var(--muted);font-weight:400"></span></div>
    <div id="opp-list"><div class="empty">🔍 A aguardar scan...</div></div>
  </div>
</div>

<!-- ════════════════════════════════ -->
<!-- HISTÓRICO                        -->
<!-- ════════════════════════════════ -->
<div class="page" id="page-history">
  <div style="height:10px"></div>
  <div class="pt">📜 Histórico de Trades</div>
  <div class="sec">
    <div id="hist"><div class="empty">Sem trades executados ainda</div></div>
  </div>
</div>

<!-- ════════════════════════════════ -->
<!-- CONTROLO                         -->
<!-- ════════════════════════════════ -->
<div class="page" id="page-control">
  <div style="height:10px"></div>
  <div class="pt">🎮 Controlo do Bot</div>

  <div class="sec">
    <div class="st">Estado Actual</div>
    <div class="irow"><span class="il">Status</span><span class="iv" id="c-st">—</span></div>
    <div class="irow"><span class="il">Modo</span><span class="iv" id="c-md">—</span></div>
    <div class="irow"><span class="il">Último Scan</span><span class="iv" id="c-sc">—</span></div>
    <div class="irow"><span class="il">Uptime</span><span class="iv" id="c-up">—</span></div>
    <div class="irow"><span class="il">Erros</span><span class="iv" id="c-er">—</span></div>
    <div class="irow"><span class="il">Lucro Mínimo</span><span class="iv" id="c-pf">—</span></div>
  </div>

  <div class="sec">
    <div class="st">Acções</div>
    <button class="btn g" onclick="act('start')">▶️ Iniciar Bot</button>
    <button class="btn r" onclick="act('stop')">⏹️ Parar Bot</button>
    <button class="btn b" onclick="doScan()">🔍 Scan Manual Agora</button>
  </div>

  <div class="sec">
    <div class="st">🤖 Auto-Trade</div>
    <div class="info-box">
      ⚠️ Auto-Trade executa ordens reais automaticamente.<br>
      Só activa se souberes o que estás a fazer.
    </div>
    <div class="irow">
      <div>
        <div style="font-size:.85em;font-weight:600">Executar trades automaticamente</div>
        <div style="font-size:.7em;color:var(--muted);margin-top:2px">Usa dinheiro real</div>
      </div>
      <label class="toggle">
        <input type="checkbox" id="auto-cb" onchange="toggleAuto()">
        <span class="sl"></span>
      </label>
    </div>
  </div>
</div>

<!-- ════════════════════════════════ -->
<!-- CONFIGURAÇÕES                    -->
<!-- ════════════════════════════════ -->
<div class="page" id="page-settings">
  <div style="height:10px"></div>
  <div class="pt">⚙️ Configurações</div>

  <div class="sec">
    <div class="st">💵 Volume de Trade</div>
    <div class="fg">
      <label>Valor em USDT (mínimo 10)</label>
      <input class="fi" type="number" id="s-amt" min="10" step="1" value="10">
    </div>
    <div class="st" style="margin-top:8px">📈 Lucro Mínimo para Detectar</div>
    <div class="info-box">
      💡 Recomendado: <strong>0.01%</strong> para detectar mais oportunidades.<br>
      Valores altos como 0.3% raramente aparecem no mercado.
    </div>
    <div class="fg">
      <label>Percentagem mínima de lucro</label>
      <input class="fi" type="number" id="s-pft" min="0.01" step="0.01" value="0.01">
    </div>
    <div id="msg-cfg" class="alert"></div>
    <button class="btn g" onclick="saveCfg()">✅ Guardar</button>
  </div>

  <div class="sec">
    <div class="st">⚡ Volume Rápido</div>
    <div class="brow">
      <button class="bsm g" onclick="setV(10)">$10</button>
      <button class="bsm g" onclick="setV(25)">$25</button>
      <button class="bsm g" onclick="setV(50)">$50</button>
      <button class="bsm g" onclick="setV(100)">$100</button>
    </div>
  </div>

  <div class="sec">
    <div class="st">⚡ Lucro Mínimo Rápido</div>
    <div class="brow">
      <button class="bsm b" onclick="setP(0.01)">0.01%</button>
      <button class="bsm b" onclick="setP(0.05)">0.05%</button>
      <button class="bsm b" onclick="setP(0.1)">0.10%</button>
      <button class="bsm b" onclick="setP(0.3)">0.30%</button>
    </div>
  </div>
</div>

<!-- ════════════════════════════════ -->
<!-- CHAVES API                       -->
<!-- ════════════════════════════════ -->
<div class="page" id="page-keys">
  <div style="height:10px"></div>
  <div class="pt">🔑 Chaves API</div>

  <div class="info-box" style="margin-bottom:10px">
    🔒 As chaves são guardadas no servidor de forma segura.<br>
    Usa apenas chaves com permissão de <strong>Leitura + Spot Trading</strong>.<br>
    <strong>Sem restrição de IP</strong> nas configurações da exchange.
  </div>

  <div class="sec">
    <div class="st">🟡 Bybit</div>
    <div class="fg">
      <label>API Key</label>
      <input class="fi" type="password" id="byk" placeholder="Cole a Bybit API Key aqui">
    </div>
    <div class="fg">
      <label>API Secret</label>
      <input class="fi" type="password" id="bys" placeholder="Cole o Bybit Secret aqui">
    </div>
  </div>

  <div class="sec">
    <div class="st">🟠 Binance</div>
    <div class="fg">
      <label>API Key</label>
      <input class="fi" type="password" id="bnk" placeholder="Cole a Binance API Key aqui">
    </div>
    <div class="fg">
      <label>API Secret</label>
      <input class="fi" type="password" id="bns" placeholder="Cole o Binance Secret aqui">
    </div>
  </div>

  <div id="msg-keys" class="alert"></div>
  <button class="btn g" onclick="saveKeys()">✅ Guardar e Conectar</button>

  <div class="sec" style="margin-top:10px">
    <div class="st">Estado da Ligação</div>
    <div id="key-st"><div class="empty">Sem chaves configuradas</div></div>
  </div>
</div>

<!-- ════════════════════════════════ -->
<!-- NAV                              -->
<!-- ════════════════════════════════ -->
<nav class="nav">
  <button class="nav-btn active" onclick="goTo('home',this)">
    <span>📊</span>Dashboard</button>
  <button class="nav-btn" onclick="goTo('history',this)">
    <span>📜</span>Histórico</button>
  <button class="nav-btn" onclick="goTo('control',this)">
    <span>🎮</span>Controlo</button>
  <button class="nav-btn" onclick="goTo('settings',this)">
    <span>⚙️</span>Config</button>
  <button class="nav-btn" onclick="goTo('keys',this)">
    <span>🔑</span>Chaves</button>
</nav>

<script>
function goTo(p,b){
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(x=>x.classList.remove('active'));
  document.getElementById('page-'+p).classList.add('active');
  b.classList.add('active');
  window.scrollTo(0,0);
}
const f2=n=>Number(n).toFixed(2),f3=n=>Number(n).toFixed(3),f4=n=>Number(n).toFixed(4);
const ago=ts=>{
  if(!ts)return'—';
  const s=Math.round(Date.now()/1e3-ts);
  return s<60?s+'s atrás':s<3600?Math.round(s/60)+'m atrás':Math.round(s/3600)+'h atrás';
};
const upt=s=>{const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m'};

async function act(action,data={}){
  try{
    await fetch('/control',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,...data})});
    setTimeout(load,800);
  }catch(e){console.warn(e)}
}
async function doScan(){ await act('scan') }
async function toggleAuto(){ await act('toggle_auto') }
function setV(v){document.getElementById('s-amt').value=v}
function setP(v){document.getElementById('s-pft').value=v}

async function saveCfg(){
  const a=parseFloat(document.getElementById('s-amt').value)||10;
  const p=parseFloat(document.getElementById('s-pft').value)||0.01;
  if(a<10){showMsg('cfg','❌ Mínimo 10 USDT',false);return}
  await act('settings',{amount:a,profit:p});
  showMsg('cfg','✅ Guardado!',true);
}

function showMsg(id,txt,ok){
  const el=document.getElementById('msg-'+id);
  el.textContent=txt;
  el.className='alert '+(ok?'ok':'err');
  setTimeout(()=>{el.className='alert'},3500);
}

async function saveKeys(){
  const body={
    bybit_key:    document.getElementById('byk').value.trim(),
    bybit_secret: document.getElementById('bys').value.trim(),
    binance_key:  document.getElementById('bnk').value.trim(),
    binance_secret:document.getElementById('bns').value.trim(),
  };
  if(!body.bybit_key && !body.binance_key){
    showMsg('keys','❌ Insere pelo menos uma chave API',false);return;
  }
  try{
    const r=await fetch('/save_keys',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){showMsg('keys','✅ Chaves guardadas! A conectar...',true);setTimeout(load,1500)}
    else showMsg('keys','❌ '+d.error,false);
  }catch(e){showMsg('keys','❌ Erro de ligação',false)}
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

  document.getElementById('sdot').className='sdot '+(s.running?'on':'');

  // KPIs
  document.getElementById('k-sc').textContent  = s.scans||0;
  document.getElementById('k-op').textContent  = s.opps_found||0;
  document.getElementById('k-tok').textContent = s.trades_ok||0;
  document.getElementById('k-fail').textContent= (s.trades_fail||0)+' falhados';
  document.getElementById('k-pr').textContent  = '$'+f4(s.total_profit||0);

  // Exchanges
  document.getElementById('ex-grid').innerHTML=
    Object.entries(ex).map(([n,i])=>`
      <div class="ec">
        <div class="en">
          <span class="dot ${i.connected?'g':'r'}"></span>
          ${n.toUpperCase()}
        </div>
        <div class="eb">${i.connected?'$'+f2(i.balance||0):'—'}</div>
        <div style="font-size:.68em;color:${i.connected?'var(--green)':'var(--red)'}">
          ${i.connected?'✅ Conectado':'❌ Erro'}
        </div>
        ${!i.connected&&i.error?`<div class="ee">${i.error.slice(0,55)}</div>`:''}
      </div>`).join('');

  // Oportunidades
  document.getElementById('opp-ct').textContent=op.length?'('+op.length+')':'';
  const od=document.getElementById('opp-list');
  if(op.length){
    od.innerHTML=op.slice(0,10).map(o=>{
      const hot=o.profit_pct>=0.5;
      const st=(o.steps||[]).map((s,i)=>
        `<div>${['1️⃣','2️⃣','3️⃣'][i]||'•'} ${(s.side||'').toUpperCase()} ${s.pair||''} @ ${Number(s.price||0).toFixed(8)}</div>`
      ).join('');
      return `
      <div class="opp ${hot?'hot':''}">
        <div class="oh">
          <span class="ox">${o.exchange.toUpperCase()}</span>
          <span class="op ${hot?'hot':''}">+${f3(o.profit_pct)}%</span>
          <span class="ou">+$${f4(o.profit_usdt)}</span>
          <span style="font-size:.65em;color:var(--muted)">${ago(o.timestamp)}</span>
        </div>
        <div class="or">🔄 ${o.route}</div>
        <div class="os">${st}</div>
        <div style="font-size:.64em;color:var(--muted);margin-top:4px">
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
    const rows=[...tr].reverse().slice(0,30).map(t=>`
      <tr>
        <td>${new Date((t.ts||0)*1e3).toLocaleTimeString('pt-PT')}</td>
        <td>${(t.exchange||'').toUpperCase()}</td>
        <td style="font-family:monospace;font-size:.85em">${t.route||'—'}</td>
        <td class="${(t.profit_usdt||0)>=0?'pos':'neg'}">
          ${(t.profit_usdt||0)>=0?'+':''}$${f4(t.profit_usdt||0)}</td>
        <td><span class="pill ${t.success?'ok':'fail'}">${t.success?'OK':'FAIL'}</span></td>
      </tr>`).join('');
    hw.innerHTML=`<div class="tbl-w"><table class="tbl">
      <thead><tr><th>Hora</th><th>Exchange</th><th>Rota</th><th>Lucro</th><th>OK</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }else{
    hw.innerHTML='<div class="empty">Sem trades executados ainda</div>';
  }

  // Controlo
  document.getElementById('c-st').innerHTML=s.running
    ?'<span style="color:var(--green)">🟢 ACTIVO</span>'
    :'<span style="color:var(--red)">🔴 PARADO</span>';
  document.getElementById('c-md').textContent=cf.auto_trade?'🤖 Auto-Trade':'👁️ Monitor';
  document.getElementById('c-sc').textContent=ago(s.last_scan);
  document.getElementById('c-up').textContent=s.start_time?upt(Math.round(Date.now()/1e3-s.start_time)):'—';
  document.getElementById('c-er').textContent=s.errors||0;
  document.getElementById('c-pf').textContent=f2(cf.min_profit||0.01)+'%';
  document.getElementById('auto-cb').checked=!!cf.auto_trade;

  // Config inputs
  if(cf.amount)     document.getElementById('s-amt').value=cf.amount;
  if(cf.min_profit) document.getElementById('s-pft').value=cf.min_profit;

  // Chaves estado
  document.getElementById('key-st').innerHTML=
    Object.entries(ex).map(([n,i])=>`
      <div class="irow">
        <span class="il">${n.toUpperCase()}</span>
        <span class="iv" style="color:${i.connected?'var(--green)':'var(--red)'}">
          ${i.connected?'✅ Conectado':'❌ '+(i.error||'Sem chave').slice(0,35)}
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
        if data.get("bybit_key"):      CFG["bybit_key"]     = data["bybit_key"]
        if data.get("bybit_secret"):   CFG["bybit_secret"]  = data["bybit_secret"]
        if data.get("binance_key"):    CFG["binance_key"]   = data["binance_key"]
        if data.get("binance_secret"): CFG["binance_secret"]= data["binance_secret"]
        save_cfg(CFG)
        EX.connect()
        EX.load_markets()
        for ex in list(EX.ex.keys()): EX.get_balance(ex)
        G.running = True
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/control", methods=["POST"])
def control():
    global CFG
    data  = request.get_json() or {}
    action= data.get("action","")
    if action=="start":
        G.running=True
    elif action=="stop":
        G.running=False
    elif action=="scan":
        def _s():
            if not EX.ex: return
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
        if a>=10:       CFG["amount"]=a
        if 0.01<=p<=20: CFG["min_profit"]=p
        save_cfg(CFG)
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return jsonify({"ok":True,"ts":time.time()})

def main():
    log.info("🚀 A iniciar...")
    if CFG.get("bybit_key") or CFG.get("binance_key"):
        EX.connect()
        EX.load_markets()
        for ex in list(EX.ex.keys()): EX.get_balance(ex)
        G.running=True
    threading.Thread(target=scan_loop,daemon=True).start()
    threading.Thread(target=keep_alive,daemon=True).start()
    log.info("🌐 Porta %d", PORT)
    app.run(host="0.0.0.0",port=PORT,debug=False,use_reloader=False)

if __name__=="__main__":
    main()
