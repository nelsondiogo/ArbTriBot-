#!/usr/bin/env python3
"""
Triangular Arbitrage Bot
Bybit + Binance | Telegram + Dashboard
"""

import os
import time
import logging
import threading
import asyncio
import hashlib
from typing import List, Optional

import requests
import ccxt
from flask import Flask, jsonify, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ══════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# CONFIGURAÇÕES — lidas das variáveis de ambiente
# ══════════════════════════════════════════════════
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_CHAT_IDS     = [
    x.strip()
    for x in os.getenv("ADMIN_CHAT_IDS", "").split(",")
    if x.strip()
]
BYBIT_API_KEY      = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET   = os.getenv("BYBIT_API_SECRET", "")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TRADE_AMOUNT_USDT  = max(10.0, float(os.getenv("TRADE_AMOUNT_USDT", "10")))
MIN_PROFIT_PCT     = float(os.getenv("MIN_PROFIT_PERCENT", "0.3"))
AUTO_TRADE         = os.getenv("AUTO_TRADE", "false").lower() == "true"
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))
RENDER_URL         = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8080")
PORT               = int(os.getenv("PORT", "8080"))

FEES = {"bybit": 0.001, "binance": 0.001}

TRIANGLES = [
    ["USDT", "BTC",  "ETH"],
    ["USDT", "BTC",  "BNB"],
    ["USDT", "BTC",  "SOL"],
    ["USDT", "BTC",  "XRP"],
    ["USDT", "BTC",  "ADA"],
    ["USDT", "BTC",  "DOGE"],
    ["USDT", "BTC",  "AVAX"],
    ["USDT", "ETH",  "BNB"],
    ["USDT", "ETH",  "SOL"],
    ["USDT", "ETH",  "LINK"],
    ["USDT", "ETH",  "UNI"],
    ["USDT", "ETH",  "MATIC"],
]

# ══════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════
class State:
    def __init__(self):
        self.running       = False
        self.auto_trade    = AUTO_TRADE
        self.amount        = TRADE_AMOUNT_USDT
        self.min_profit    = MIN_PROFIT_PCT
        self.opportunities: List[dict] = []
        self.trade_history: List[dict] = []
        self.alert_cache   = set()
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

# ══════════════════════════════════════════════════
# EXCHANGES
# ══════════════════════════════════════════════════
class Exchanges:
    def __init__(self):
        self.ex: dict      = {}
        self.markets: dict = {}
        self._init()

    # --------------------------------------------------
    def _init(self):
        configs = [
            ("bybit",   ccxt.bybit,   BYBIT_API_KEY,   BYBIT_API_SECRET),
            ("binance", ccxt.binance, BINANCE_API_KEY, BINANCE_API_SECRET),
        ]
        for name, cls, key, secret in configs:
            try:
                instance = cls(
                    {
                        "apiKey": key,
                        "secret": secret,
                        "enableRateLimit": True,
                        "options": {"defaultType": "spot"},
                    }
                )
                self.ex[name] = instance
                log.info("✅ %s inicializado", name)
            except Exception as exc:
                log.error("❌ %s: %s", name, exc)
                G.exchange_status[name]["error"] = str(exc)

    # --------------------------------------------------
    def load_markets(self):
        for name, ex in self.ex.items():
            try:
                self.markets[name] = ex.load_markets()
                G.exchange_status[name]["connected"] = True
                log.info("📊 %s: %d pares", name, len(self.markets[name]))
            except Exception as exc:
                log.error("❌ mercados %s: %s", name, exc)
                G.exchange_status[name]["error"] = str(exc)

    # --------------------------------------------------
    def has_pair(self, ex: str, symbol: str) -> bool:
        return symbol in self.markets.get(ex, {})

    # --------------------------------------------------
    def _ob_price(self, ex: str, symbol: str, side: str) -> Optional[float]:
        """side = 'ask' | 'bid'"""
        try:
            ob = self.ex[ex].fetch_order_book(symbol, 3)
            if side == "ask":
                lst = ob.get("asks", [])
            else:
                lst = ob.get("bids", [])
            return float(lst[0][0]) if lst else None
        except Exception:
            return None

    def best_ask(self, ex: str, sym: str) -> Optional[float]:
        return self._ob_price(ex, sym, "ask")

    def best_bid(self, ex: str, sym: str) -> Optional[float]:
        return self._ob_price(ex, sym, "bid")

    # --------------------------------------------------
    def get_balance(self, ex_name: str) -> dict:
        try:
            bal  = self.ex[ex_name].fetch_balance()
            usdt = bal.get("USDT", {}).get("free", 0.0)
            G.exchange_status[ex_name]["balance"] = round(float(usdt), 2)
            return bal
        except Exception as exc:
            G.exchange_status[ex_name]["error"] = str(exc)
            return {}

    # --------------------------------------------------
    def execute_order(
        self, ex: str, symbol: str, side: str, amount: float
    ) -> dict:
        try:
            order = self.ex[ex].create_market_order(symbol, side, amount)
            return {"ok": True, "order": order}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


EX = Exchanges()

# ══════════════════════════════════════════════════
# MOTOR DE ARBITRAGEM
# ══════════════════════════════════════════════════
def _step(
    ex: str, base: str, quote: str, amount_in: float
) -> Optional[dict]:
    """
    Compra BASE pagando QUOTE.
    Tenta  BASE/QUOTE  (ask)  ou  QUOTE/BASE  (bid).
    """
    fee  = FEES.get(ex, 0.001)
    sym1 = f"{base}/{quote}"
    sym2 = f"{quote}/{base}"

    if EX.has_pair(ex, sym1):
        price = EX.best_ask(ex, sym1)
        if price:
            return {
                "pair":  sym1,
                "side":  "buy",
                "price": price,
                "in":    amount_in,
                "out":   (amount_in / price) * (1 - fee),
            }

    if EX.has_pair(ex, sym2):
        price = EX.best_bid(ex, sym2)
        if price:
            return {
                "pair":  sym2,
                "side":  "sell",
                "price": price,
                "in":    amount_in,
                "out":   amount_in * price * (1 - fee),
            }

    return None


def calc_triangle(ex: str, tri: list, amount: float) -> Optional[dict]:
    A, B, C = tri
    best = None

    for route_nodes in ([A, B, C, A], [A, C, B, A]):
        cur    = amount
        steps  = []
        failed = False

        for i in range(3):
            frm  = route_nodes[i]
            to   = route_nodes[i + 1]
            step = _step(ex, base=to, quote=frm, amount_in=cur)
            if step is None:
                failed = True
                break
            steps.append(step)
            cur = step["out"]

        if failed:
            continue

        profit_usdt = cur - amount
        profit_pct  = (profit_usdt / amount) * 100

        if profit_pct < G.min_profit:
            continue

        route = "→".join(route_nodes)
        opp   = {
            "exchange":    ex,
            "triangle":    tri,
            "route":       route,
            "steps":       steps,
            "amount":      amount,
            "profit_pct":  profit_pct,
            "profit_usdt": profit_usdt,
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
            except Exception as exc:
                G.stats["errors"] += 1
                log.debug("Erro %s %s: %s", ex, tri, exc)

    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


def execute_trade(opp: dict) -> dict:
    ex = opp["exchange"]
    for step in opp["steps"]:
        r = EX.execute_order(ex, step["pair"], step["side"], step["in"])
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

# ══════════════════════════════════════════════════
# SCAN LOOP
# ══════════════════════════════════════════════════
def scan_loop(tg_app: Application):
    log.info("🔄 Scan loop a iniciar…")
    EX.load_markets()
    EX.get_balance("bybit")
    EX.get_balance("binance")

    loop = asyncio.new_event_loop()

    while True:
        try:
            if G.running:
                with G.lock:
                    opps              = scan_all()
                    G.opportunities   = opps
                    G.stats["scans"] += 1
                    G.stats["last_scan"] = time.time()

                for opp in opps[:3]:
                    oid = opp["id"]
                    if oid not in G.alert_cache:
                        G.alert_cache.add(oid)
                        threading.Timer(
                            300, G.alert_cache.discard, [oid]
                        ).start()

                        for cid in ADMIN_CHAT_IDS:
                            asyncio.run_coroutine_threadsafe(
                                _send_alert(tg_app, cid, opp), loop
                            )

                        if G.auto_trade:
                            execute_trade(opp)

            # Actualiza saldos de 5 em 5 scans
            if G.stats["scans"] % 5 == 0:
                EX.get_balance("bybit")
                EX.get_balance("binance")

        except Exception as exc:
            G.stats["errors"] += 1
            log.error("Scan error: %s", exc)

        time.sleep(SCAN_INTERVAL)


async def _send_alert(app: Application, chat_id: str, opp: dict):
    try:
        emoji = "🔥" if opp["profit_pct"] > 1 else "💰"
        steps = "\n".join(
            f"  {'1️⃣2️⃣3️⃣'[i]} {s['side'].upper()} {s['pair']} @ {s['price']:.8f}"
            for i, s in enumerate(opp["steps"])
        )
        text = (
            f"{emoji} *Oportunidade #{opp['id']}*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏦 `{opp['exchange'].upper()}`\n"
            f"🔄 `{opp['route']}`\n"
            f"📈 `+{opp['profit_pct']:.3f}%`\n"
            f"💵 `+${opp['profit_usdt']:.4f}`\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{steps}"
        )
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("🌐 Dashboard", callback_data="cb_dashboard"),
                InlineKeyboardButton("✅ OK",         callback_data="cb_dismiss"),
            ]]
        )
        await app.bot.send_message(
            chat_id, text, parse_mode="Markdown", reply_markup=kb
        )
    except Exception as exc:
        log.error("Alert error: %s", exc)

# ══════════════════════════════════════════════════
# TELEGRAM — helpers
# ══════════════════════════════════════════════════
def is_admin(uid) -> bool:
    return str(uid) in ADMIN_CHAT_IDS


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Iniciar" if not G.running else "✅ Activo",
                    callback_data="cb_start",
                ),
                InlineKeyboardButton("⏹️ Parar", callback_data="cb_stop"),
            ],
            [
                InlineKeyboardButton("🔍 Scan Agora",    callback_data="cb_scan"),
                InlineKeyboardButton("💰 Oportunidades", callback_data="cb_opps"),
            ],
            [
                InlineKeyboardButton("📈 Estatísticas",  callback_data="cb_stats"),
                InlineKeyboardButton("💼 Saldos",        callback_data="cb_balances"),
            ],
            [
                InlineKeyboardButton("⚙️ Configurações", callback_data="cb_settings"),
                InlineKeyboardButton("🌐 Dashboard",     callback_data="cb_dashboard"),
            ],
            [
                InlineKeyboardButton(
                    f"🤖 Auto-Trade: {'ON ✅' if G.auto_trade else 'OFF ❌'}",
                    callback_data="cb_toggle_auto",
                )
            ],
        ]
    )


def status_text() -> str:
    uptime = int(time.time() - G.stats["start_time"])
    h, rem = divmod(uptime, 3600)
    m      = rem // 60
    last   = (
        f"{int(time.time() - G.stats['last_scan'])}s atrás"
        if G.stats["last_scan"]
        else "—"
    )
    return (
        f"🤖 *Arbitrage Bot*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Status:  {'🟢 ACTIVO' if G.running else '🔴 PARADO'}\n"
        f"Modo:    {'🤖 Auto-Trade' if G.auto_trade else '👁️ Monitor'}\n"
        f"Volume:  `${G.amount} USDT`\n"
        f"Profit:  `≥ {G.min_profit}%`\n"
        f"Uptime:  `{h}h {m}m`\n"
        f"Scan:    `{last}`\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

# ══════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        status_text(), parse_mode="Markdown", reply_markup=main_kb()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 *Comandos*\n\n"
        "/start — Menu principal\n"
        "/scan — Scan manual\n"
        "/opps — Oportunidades actuais\n"
        "/stats — Estatísticas\n"
        "/balances — Saldos\n"
        "/status — Estado\n"
        "/setamount `<valor>` — Volume (min 10)\n"
        "/setprofit `<%>` — Lucro mínimo\n"
        "/autotrade — Liga/desliga auto-trade",
        parse_mode="Markdown",
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg  = await update.message.reply_text("🔍 A fazer scan…")
    opps = scan_all()
    G.opportunities = opps
    if opps:
        lines = [f"✅ *{len(opps)} oportunidade(s)*\n"]
        for i, o in enumerate(opps[:5], 1):
            lines.append(
                f"*{i}.* `{o['exchange'].upper()}` — `{o['route']}`\n"
                f"   +{o['profit_pct']:.3f}% (+${o['profit_usdt']:.4f})\n"
            )
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    else:
        await msg.edit_text("❌ Sem oportunidades lucrativas agora.")


async def cmd_opps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    opps = G.opportunities
    if not opps:
        await update.message.reply_text("🔍 Sem oportunidades. Usa /scan.")
        return
    lines = ["💰 *Top Oportunidades*\n"]
    for i, o in enumerate(opps[:5], 1):
        lines.append(
            f"*{i}.* `{o['exchange'].upper()}` — `{o['route']}`\n"
            f"   +{o['profit_pct']:.3f}% (+${o['profit_usdt']:.4f})\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s  = G.stats
    ex = G.exchange_status
    await update.message.reply_text(
        f"📈 *Estatísticas*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Scans:         `{s['scans']}`\n"
        f"Oportunidades: `{s['opps_found']}`\n"
        f"Trades OK:     `{s['trades_ok']}`\n"
        f"Trades Fail:   `{s['trades_fail']}`\n"
        f"Lucro Total:   `${s['total_profit']:.4f}`\n"
        f"Erros:         `{s['errors']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bybit:   `${ex['bybit']['balance']} USDT`\n"
        f"Binance: `${ex['binance']['balance']} USDT`",
        parse_mode="Markdown",
    )


async def cmd_balances(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg   = await update.message.reply_text("💼 A obter saldos…")
    lines = ["💼 *Saldos*\n━━━━━━━━━━━━━━━━━━"]
    for ex_name in ["bybit", "binance"]:
        try:
            bal   = EX.get_balance(ex_name)
            total = bal.get("total", {})
            lines.append(f"\n🏦 *{ex_name.upper()}*")
            shown = 0
            for coin, amt in total.items():
                if isinstance(amt, (int, float)) and amt > 0.0001:
                    lines.append(f"  `{coin}`: {amt:.6f}")
                    shown += 1
                    if shown >= 10:
                        break
            if shown == 0:
                lines.append("  Sem saldo relevante")
        except Exception as exc:
            lines.append(f"\n🔴 *{ex_name.upper()}*: {exc}")
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        status_text() + f"\n🌐 `{RENDER_URL}`",
        parse_mode="Markdown",
    )


async def cmd_set_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        val = float(ctx.args[0])
        if val < 10:
            await update.message.reply_text("❌ Mínimo: 10 USDT")
            return
        if val > 10_000:
            await update.message.reply_text("❌ Máximo: 10 000 USDT")
            return
        G.amount = val
        await update.message.reply_text(
            f"✅ Volume: *${val} USDT*", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Uso: `/setamount 10`", parse_mode="Markdown"
        )


async def cmd_set_profit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        val = float(ctx.args[0])
        if not 0.05 <= val <= 20:
            await update.message.reply_text("❌ Entre 0.05% e 20%")
            return
        G.min_profit = val
        await update.message.reply_text(
            f"✅ Profit mínimo: *{val}%*", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Uso: `/setprofit 0.3`", parse_mode="Markdown"
        )


async def cmd_autotrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    G.auto_trade = not G.auto_trade
    label = "🤖 *ACTIVADO*" if G.auto_trade else "👁️ *DESACTIVADO*"
    warn  = "\n\n⚠️ Usa dinheiro REAL!" if G.auto_trade else ""
    await update.message.reply_text(
        f"Auto-Trade: {label}{warn}", parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return

    d = q.data

    # ── start / stop ──────────────────────────────
    if d == "cb_start":
        G.running = True
        await q.edit_message_text(
            "✅ *Bot INICIADO!*\nA monitorar 24h/7d…",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    elif d == "cb_stop":
        G.running = False
        await q.edit_message_text(
            "⏹️ *Bot PARADO.*",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── scan ──────────────────────────────────────
    elif d == "cb_scan":
        await q.edit_message_text("🔍 A fazer scan…", parse_mode="Markdown")
        opps            = scan_all()
        G.opportunities = opps
        if opps:
            lines = [f"✅ *{len(opps)} oportunidade(s)*\n"]
            for i, o in enumerate(opps[:5], 1):
                lines.append(
                    f"*{i}.* `{o['exchange'].upper()}` — `{o['route']}`\n"
                    f"   +{o['profit_pct']:.3f}% (+${o['profit_usdt']:.4f})\n"
                )
            txt = "\n".join(lines)
        else:
            txt = "❌ Sem oportunidades agora."
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=main_kb())

    # ── opps ──────────────────────────────────────
    elif d == "cb_opps":
        opps = G.opportunities
        if opps:
            lines = ["💰 *Oportunidades Actuais*\n"]
            for i, o in enumerate(opps[:5], 1):
                lines.append(
                    f"*{i}.* `{o['exchange'].upper()}` — `{o['route']}`\n"
                    f"   +{o['profit_pct']:.3f}% (+${o['profit_usdt']:.4f})\n"
                )
            txt = "\n".join(lines)
        else:
            txt = "🔍 Sem oportunidades. Faz um scan."
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=main_kb())

    # ── stats ─────────────────────────────────────
    elif d == "cb_stats":
        s  = G.stats
        ex = G.exchange_status
        await q.edit_message_text(
            f"📈 *Estatísticas*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Scans: `{s['scans']}`\n"
            f"Opps:  `{s['opps_found']}`\n"
            f"Trades OK:   `{s['trades_ok']}`\n"
            f"Trades Fail: `{s['trades_fail']}`\n"
            f"Lucro: `${s['total_profit']:.4f}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Bybit:   `${ex['bybit']['balance']} USDT`\n"
            f"Binance: `${ex['binance']['balance']} USDT`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── balances ──────────────────────────────────
    elif d == "cb_balances":
        await q.edit_message_text("💼 A obter saldos…", parse_mode="Markdown")
        EX.get_balance("bybit")
        EX.get_balance("binance")
        ex = G.exchange_status
        await q.edit_message_text(
            f"💼 *Saldos USDT*\n"
            f"🏦 Bybit:   `${ex['bybit']['balance']}`\n"
            f"🏦 Binance: `${ex['binance']['balance']}`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── settings ──────────────────────────────────
    elif d == "cb_settings":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("💵 $10",  callback_data="cb_amt_10"),
                    InlineKeyboardButton("💵 $25",  callback_data="cb_amt_25"),
                    InlineKeyboardButton("💵 $50",  callback_data="cb_amt_50"),
                    InlineKeyboardButton("💵 $100", callback_data="cb_amt_100"),
                ],
                [
                    InlineKeyboardButton("📊 0.2%", callback_data="cb_pft_02"),
                    InlineKeyboardButton("📊 0.3%", callback_data="cb_pft_03"),
                    InlineKeyboardButton("📊 0.5%", callback_data="cb_pft_05"),
                    InlineKeyboardButton("📊 1.0%", callback_data="cb_pft_10"),
                ],
                [InlineKeyboardButton("🔙 Voltar", callback_data="cb_back")],
            ]
        )
        await q.edit_message_text(
            f"⚙️ *Configurações*\n\n"
            f"Volume:  `${G.amount} USDT`\n"
            f"Profit:  `{G.min_profit}%`\n\n"
            f"Selecciona:",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif d.startswith("cb_amt_"):
        G.amount = float(d.replace("cb_amt_", ""))
        await q.edit_message_text(
            f"✅ Volume: `${G.amount} USDT`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    elif d.startswith("cb_pft_"):
        mapping = {"02": 0.2, "03": 0.3, "05": 0.5, "10": 1.0}
        G.min_profit = mapping.get(d.replace("cb_pft_", ""), 0.3)
        await q.edit_message_text(
            f"✅ Profit mínimo: `{G.min_profit}%`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── auto-trade ────────────────────────────────
    elif d == "cb_toggle_auto":
        G.auto_trade = not G.auto_trade
        label = "ON ✅" if G.auto_trade else "OFF ❌"
        warn  = "\n\n⚠️ Usa dinheiro REAL!" if G.auto_trade else ""
        await q.edit_message_text(
            f"Auto-Trade: *{label}*{warn}",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── dashboard ─────────────────────────────────
    elif d == "cb_dashboard":
        await q.edit_message_text(
            f"🌐 *Dashboard Web*\n\n`{RENDER_URL}`",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )

    # ── dismiss / back ────────────────────────────
    elif d == "cb_dismiss":
        try:
            await q.delete_message()
        except Exception:
            pass

    elif d == "cb_back":
        await q.edit_message_text(
            status_text(), parse_mode="Markdown", reply_markup=main_kb()
        )

# ══════════════════════════════════════════════════
# WEB DASHBOARD
# ══════════════════════════════════════════════════
flask_app = Flask(__name__)

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arbitrage Bot</title>
<style>
:root{
  --bg:#090d1a;--card:#111827;--border:#1f2d45;
  --accent:#7c3aed;--green:#10b981;--red:#ef4444;
  --yellow:#f59e0b;--blue:#3b82f6;--text:#e2e8f0;--muted:#6b7280;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
header{
  background:linear-gradient(135deg,#1a1f35,#0d1117);
  border-bottom:1px solid var(--border);padding:16px 24px;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
}
header h1{font-size:1.3em;color:var(--accent)}
header h1 span{font-size:.7em;color:var(--muted);display:block;font-weight:400}
.badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{padding:4px 12px;border-radius:20px;font-size:.78em;font-weight:600}
.bg{background:#1a3a2a;color:var(--green)}
.bb{background:#1e2a4a;color:var(--blue)}
.bp{background:#2d1a4a;color:#c4b5fd}
.br{background:#3a1a1a;color:var(--red)}
main{max-width:1280px;margin:0 auto;padding:16px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;transition:transform .2s}
.kpi-card:hover{transform:translateY(-2px)}
.kpi-label{font-size:.7em;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.kpi-val{font-size:1.8em;font-weight:700}
.kpi-sub{font-size:.7em;color:var(--muted);margin-top:4px}
.c-g .kpi-val{color:var(--green)}.c-b .kpi-val{color:var(--blue)}
.c-y .kpi-val{color:var(--yellow)}.c-p .kpi-val{color:#c4b5fd}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:720px){.two{grid-template-columns:1fr}}
.sec{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px}
.sec-t{font-size:.88em;color:#c4b5fd;font-weight:600;letter-spacing:.4px;
       margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.ex-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ex-card{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.ex-name{font-size:.95em;font-weight:700;margin-bottom:6px}
.ex-bal{font-size:1.5em;font-weight:700;color:var(--green)}
.ex-st{font-size:.72em;color:var(--muted);margin-top:4px}
.ok-dot::before{content:"● ";color:var(--green)}.err-dot::before{content:"● ";color:var(--red)}
.opp{background:var(--bg);border:1px solid var(--border);border-left:3px solid var(--green);
     border-radius:10px;padding:14px;margin-bottom:10px}
.opp.hot{border-left-color:var(--yellow)}
.opp-h{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.exch-tag{background:#1f2d45;padding:3px 10px;border-radius:12px;font-size:.75em;font-weight:600}
.pct{color:var(--green);font-size:1.2em;font-weight:700}.pct.hot{color:var(--yellow)}
.usdt-val{color:var(--yellow);font-size:.85em}
.route{font-family:monospace;font-size:.78em;color:var(--muted);
       background:#090d1a;padding:6px 10px;border-radius:6px;margin-bottom:6px}
.steps{font-family:monospace;font-size:.72em;color:#9ca3af}
.steps div{padding:2px 0}
.tbl{width:100%;border-collapse:collapse;font-size:.82em}
.tbl th,.tbl td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:600}
.tbl tr:last-child td{border-bottom:none}
.pos{color:var(--green)}.neg{color:var(--red)}
.pill{padding:2px 8px;border-radius:10px;font-size:.78em;font-weight:600}
.pill-ok{background:#0a2a1a;color:var(--green)}.pill-fail{background:#2a0a0a;color:var(--red)}
.empty{text-align:center;padding:32px;color:var(--muted);
       background:var(--bg);border-radius:8px;border:1px dashed var(--border)}
.cfg-tbl{width:100%;border-collapse:collapse;font-size:.85em}
.cfg-tbl td{padding:8px 6px;border-bottom:1px solid var(--border)}
.cfg-tbl tr:last-child td{border-bottom:none}
.cfg-tbl td:first-child{color:var(--muted);width:50%}
footer{text-align:center;padding:14px;color:var(--muted);font-size:.75em}
.prog{height:2px;background:var(--border);border-radius:2px;overflow:hidden;margin-top:5px}
.prog-fill{height:100%;background:var(--accent);animation:prog 10s linear infinite}
@keyframes prog{from{width:0}to{width:100%}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.live{animation:pulse 2s infinite}
</style>
</head>
<body>
<header>
  <h1>🤖 Arbitrage Bot <span>Bybit &amp; Binance · Triangular · 24h</span></h1>
  <div class="badges">
    <span class="badge bg live" id="hd-st">⬤ —</span>
    <span class="badge bb" id="hd-sc">🔄 —</span>
    <span class="badge bp" id="hd-md">👁️ Monitor</span>
    <span class="badge bp" id="hd-vl">💵 —</span>
  </div>
</header>

<main>
  <!-- KPIs -->
  <div class="kpi">
    <div class="kpi-card c-b">
      <div class="kpi-label">🔄 Scans</div>
      <div class="kpi-val" id="k-sc">—</div>
      <div class="kpi-sub">Total realizados</div>
    </div>
    <div class="kpi-card c-p">
      <div class="kpi-label">💡 Oportunidades</div>
      <div class="kpi-val" id="k-op">—</div>
      <div class="kpi-sub">Detectadas</div>
    </div>
    <div class="kpi-card c-g">
      <div class="kpi-label">✅ Trades OK</div>
      <div class="kpi-val" id="k-tok">—</div>
      <div class="kpi-sub" id="k-tf">0 falhados</div>
    </div>
    <div class="kpi-card c-y">
      <div class="kpi-label">💵 Lucro Total</div>
      <div class="kpi-val" id="k-pr">—</div>
      <div class="kpi-sub">USDT</div>
    </div>
    <div class="kpi-card c-b">
      <div class="kpi-label">⚠️ Erros</div>
      <div class="kpi-val" id="k-er">—</div>
      <div class="kpi-sub">Total</div>
    </div>
    <div class="kpi-card c-g">
      <div class="kpi-label">⏱️ Uptime</div>
      <div class="kpi-val" id="k-up">—</div>
      <div class="kpi-sub">Desde início</div>
    </div>
  </div>

  <!-- Exchanges + Config -->
  <div class="two">
    <div class="sec">
      <div class="sec-t">🏦 Exchanges</div>
      <div class="ex-grid" id="ex-grid">
        <div class="ex-card"><div class="ex-name">A carregar…</div></div>
      </div>
    </div>
    <div class="sec">
      <div class="sec-t">⚙️ Configuração</div>
      <table class="cfg-tbl">
        <tr><td>Volume</td><td id="c-vol">—</td></tr>
        <tr><td>Lucro Mínimo</td><td id="c-pft">—</td></tr>
        <tr><td>Auto-Trade</td><td id="c-aut">—</td></tr>
        <tr><td>Interval</td><td id="c-int">—</td></tr>
        <tr><td>Triângulos</td><td id="c-tri">—</td></tr>
        <tr><td>Último Scan</td><td id="c-ls">—</td></tr>
      </table>
    </div>
  </div>

  <!-- Oportunidades -->
  <div class="sec">
    <div class="sec-t">🎯 Oportunidades Actuais <span id="opp-ct" style="color:var(--muted);font-weight:400"></span></div>
    <div id="opps-list"><div class="empty">🔍 A aguardar scan…</div></div>
  </div>

  <!-- Histórico -->
  <div class="sec">
    <div class="sec-t">📜 Histórico de Trades</div>
    <div id="hist"><div class="empty">Sem trades executados</div></div>
  </div>
</main>

<footer>
  Actualiza a cada 10s
  <div class="prog"><div class="prog-fill"></div></div>
</footer>

<script>
const f2  = n => Number(n).toFixed(2);
const f3  = n => Number(n).toFixed(3);
const f4  = n => Number(n).toFixed(4);
const ago = ts => {
  const s = Math.round(Date.now()/1e3 - ts);
  if(s < 60)   return s+'s atrás';
  if(s < 3600) return Math.round(s/60)+'m atrás';
  return Math.round(s/3600)+'h atrás';
};
const uptime = s => {
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60);
  return h+'h '+m+'m';
};

async function load(){
  try{
    const r = await fetch('/api');
    const d = await r.json();
    render(d);
  }catch(e){ console.warn(e) }
}

function render(d){
  const s  = d.stats        || {};
  const ex = d.exchanges    || {};
  const op = d.opportunities|| [];
  const tr = d.trades       || [];
  const cf = d.config       || {};

  /* header */
  const run = s.running;
  document.getElementById('hd-st').textContent = run ? '⬤ ACTIVO' : '⬤ PARADO';
  document.getElementById('hd-st').className   = 'badge '+(run?'bg live':'br');
  document.getElementById('hd-sc').textContent = s.last_scan ? '🔄 '+ago(s.last_scan) : '🔄 —';
  document.getElementById('hd-md').textContent = cf.auto_trade ? '🤖 Auto-Trade' : '👁️ Monitor';
  document.getElementById('hd-vl').textContent = '💵 $'+(cf.amount||0);

  /* KPIs */
  document.getElementById('k-sc').textContent  = s.scans       || 0;
  document.getElementById('k-op').textContent  = s.opps_found  || 0;
  document.getElementById('k-tok').textContent = s.trades_ok   || 0;
  document.getElementById('k-tf').textContent  = (s.trades_fail||0)+' falhados';
  document.getElementById('k-pr').textContent  = '$'+f4(s.total_profit||0);
  document.getElementById('k-er').textContent  = s.errors      || 0;
  if(s.start_time)
    document.getElementById('k-up').textContent = uptime(Math.round(Date.now()/1e3-s.start_time));

  /* exchanges */
  const eg = document.getElementById('ex-grid');
  if(Object.keys(ex).length){
    eg.innerHTML = Object.entries(ex).map(([name,info])=>`
      <div class="ex-card">
        <div class="ex-name ${info.connected?'ok-dot':'err-dot'}">${name.toUpperCase()}</div>
        <div class="ex-bal">$${f2(info.balance||0)}</div>
        <div class="ex-st">${info.connected?'✅ Conectado':'❌ Desconectado'}</div>
        ${info.error?`<div style="font-size:.68em;color:var(--red);margin-top:3px">${info.error.slice(0,60)}</div>`:''}
      </div>`).join('');
  }

  /* config */
  document.getElementById('c-vol').textContent = '$'+f2(cf.amount||0)+' USDT';
  document.getElementById('c-pft').textContent = f2(cf.min_profit||0)+'%';
  document.getElementById('c-aut').innerHTML   = cf.auto_trade
    ? '<span class="pill pill-ok">ON ✅</span>'
    : '<span class="pill pill-fail">OFF ❌</span>';
  document.getElementById('c-int').textContent = (cf.scan_interval||15)+'s';
  document.getElementById('c-tri').textContent = (cf.triangles||0)+' pares';
  document.getElementById('c-ls').textContent  = s.last_scan ? ago(s.last_scan) : '—';

  /* oportunidades */
  document.getElementById('opp-ct').textContent = op.length ? '('+op.length+')' : '';
  const od = document.getElementById('opps-list');
  if(op.length){
    od.innerHTML = op.slice(0,10).map(o=>{
      const hot  = o.profit_pct >= 1;
      const stps = (o.steps||[]).map((s,i)=>
        `<div>${['1️⃣','2️⃣','3️⃣'][i]||'•'} ${(s.side||'').toUpperCase()} ${s.pair||''} @ ${Number(s.price||0).toFixed(8)}</div>`
      ).join('');
      return `
        <div class="opp ${hot?'hot':''}">
          <div class="opp-h">
            <span class="exch-tag">${o.exchange.toUpperCase()}</span>
            <span class="pct ${hot?'hot':''}">+${f3(o.profit_pct)}%</span>
            <span class="usdt-val">+$${f4(o.profit_usdt)}</span>
            <span style="font-size:.72em;color:var(--muted)">${ago(o.timestamp)}</span>
          </div>
          <div class="route">🔄 ${o.route}</div>
          <div class="steps">${stps}</div>
          <div style="font-size:.7em;color:var(--muted);margin-top:5px">
            Vol: $${f2(o.amount)} USDT · ID: ${o.id}
          </div>
        </div>`;
    }).join('');
  } else {
    od.innerHTML = '<div class="empty">🔍 Sem oportunidades lucrativas neste momento</div>';
  }

  /* histórico */
  const hw = document.getElementById('hist');
  if(tr.length){
    const rows = [...tr].reverse().slice(0,20).map(t=>`
      <tr>
        <td>${new Date((t.ts||0)*1e3).toLocaleTimeString('pt-PT')}</td>
        <td>${(t.exchange||'').toUpperCase()}</td>
        <td style="font-family:monospace;font-size:.8em">${t.route||'—'}</td>
        <td class="${(t.profit_usdt||0)>=0?'pos':'neg'}">${(t.profit_usdt||0)>=0?'+':''}$${f4(t.profit_usdt||0)}</td>
        <td class="${(t.profit_pct||0)>=0?'pos':'neg'}">${f3(t.profit_pct||0)}%</td>
        <td><span class="pill ${t.success?'pill-ok':'pill-fail'}">${t.success?'OK':'FAIL'}</span></td>
      </tr>`).join('');
    hw.innerHTML = `
      <table class="tbl">
        <thead><tr>
          <th>Hora</th><th>Exchange</th><th>Rota</th>
          <th>Lucro $</th><th>%</th><th>Estado</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } else {
    hw.innerHTML = '<div class="empty">Sem trades executados ainda</div>';
  }
}

load();
setInterval(load, 10000);
</script>
</body>
</html>
"""


@flask_app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@flask_app.route("/api")
def api():
    return jsonify(
        {
            "stats": {**G.stats, "running": G.running},
            "exchanges": G.exchange_status,
            "opportunities": G.opportunities[:10],
            "trades": G.trade_history[-50:],
            "config": {
                "amount":        G.amount,
                "min_profit":    G.min_profit,
                "auto_trade":    G.auto_trade,
                "scan_interval": SCAN_INTERVAL,
                "triangles":     len(TRIANGLES),
            },
        }
    )


@flask_app.route("/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})


# ══════════════════════════════════════════════════
# KEEP-ALIVE
# ══════════════════════════════════════════════════
def keep_alive():
    time.sleep(90)
    while True:
        try:
            requests.get(f"{RENDER_URL}/health", timeout=8)
            log.info("🏓 keep-alive OK")
        except Exception as exc:
            log.warning("keep-alive falhou: %s", exc)
        time.sleep(270)


# ══════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def start_scan_thread(tg_app: Application):
    def _run():
        time.sleep(20)
        G.running = True
        scan_loop(tg_app)

    threading.Thread(target=_run, daemon=True).start()


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido!")

    log.info("🚀 A iniciar Arbitrage Bot…")

    # Flask
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("🌐 Dashboard em http://0.0.0.0:%d", PORT)

    # Keep-alive
    threading.Thread(target=keep_alive, daemon=True).start()

    # Telegram
    tg = Application.builder().token(TELEGRAM_TOKEN).build()

    tg.add_handler(CommandHandler("start",     cmd_start))
    tg.add_handler(CommandHandler("help",      cmd_help))
    tg.add_handler(CommandHandler("scan",      cmd_scan))
    tg.add_handler(CommandHandler("opps",      cmd_opps))
    tg.add_handler(CommandHandler("stats",     cmd_stats))
    tg.add_handler(CommandHandler("balances",  cmd_balances))
    tg.add_handler(CommandHandler("status",    cmd_status))
    tg.add_handler(CommandHandler("setamount", cmd_set_amount))
    tg.add_handler(CommandHandler("setprofit", cmd_set_profit))
    tg.add_handler(CommandHandler("autotrade", cmd_autotrade))
    tg.add_handler(CallbackQueryHandler(handle_callback))

    # Scan thread
    start_scan_thread(tg)

    log.info("✅ Bot pronto!")
    tg.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
