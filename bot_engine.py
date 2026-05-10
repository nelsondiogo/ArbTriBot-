"""
Motor do bot — loop de trading em thread de background.
Gerencia conexão CCXT, posições abertas, trailing stop e Capital Shield.
"""
import time
import logging
import threading
from datetime import datetime

import ccxt
import pandas as pd

from config import Config
from crypto_utils import decrypt
from strategy import (
    compute_indicators,
    get_signal,
    check_trailing_stop,
    check_reversal,
    indicator_summary,
)

logger = logging.getLogger(__name__)

# Intervalo de ciclo por timeframe (segundos)
CYCLE_SLEEP = {"1m": 30, "5m": 90, "15m": 60, "1h": 120, "4h": 300}


class BotEngine:
    """Singleton — instanciado uma única vez em app.py."""

    def __init__(self, app):
        self.app     = app
        self._thread = None
        self.running = False

        # Estado em memória (lido pelo Dashboard via /api/status)
        self.status_msg     = "Aguardando inicialização..."
        self.current_price  = 0.0
        self.last_signal    = "none"
        self.indicators     = {}
        self.position       = None   # dict | None
        self.session_start_balance = None

        self._exchange = None
        self._lock = threading.Lock()

    # ─── Controle externo ──────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False  # já rodando
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="BotEngine")
        self._thread.start()
        logger.info("BotEngine iniciado.")
        return True

    def stop(self):
        self.running = False
        self.status_msg = "⛔ Bot parado manualmente."
        logger.info("BotEngine parado.")

    def update_trailing(self, new_pct: float):
        """Atualiza o trailing stop sem reiniciar o bot."""
        with self._lock:
            if self.position:
                self.position["trailing_pct"] = new_pct
        logger.info(f"Trailing stop atualizado para {new_pct}%")

    # ─── Exchange ──────────────────────────────────────────────────────────────

    def _init_exchange(self, settings):
        api_key    = decrypt(settings.api_key_encrypted, Config.FLASK_SECRET)
        api_secret = decrypt(settings.api_secret_encrypted, Config.FLASK_SECRET)

        cls = getattr(ccxt, settings.exchange)
        self._exchange = cls({
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,          # ← corrige retCode 10006
            "options": {"defaultType": "future"},
        })
        logger.info(f"Exchange {settings.exchange} conectada.")

    def _ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        raw = self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def _balance(self) -> float:
        bal = self._exchange.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))

    # ─── Ordens ───────────────────────────────────────────────────────────────

    def _open(self, symbol: str, side: str, size_pct: float, balance: float):
        from models import db, Trade
        ticker = self._exchange.fetch_ticker(symbol)
        price  = float(ticker["last"])
        usd    = balance * (size_pct / 100)
        qty    = self._exchange.amount_to_precision(symbol, usd / price)

        order_side = "buy" if side == "long" else "sell"
        self._exchange.create_market_order(symbol, order_side, qty)

        with self.app.app_context():
            trade = Trade(
                symbol=symbol, side=side,
                entry_price=price, quantity=float(qty), status="open"
            )
            db.session.add(trade)
            db.session.commit()
            trade_id = trade.id

        with self._lock:
            self.position = {
                "side":         side,
                "entry":        price,
                "qty":          float(qty),
                "peak":         price,
                "trailing_pct": None,  # será lido das settings a cada ciclo
                "trade_id":     trade_id,
            }

        self.status_msg = f"✅ {side.upper()} aberto @ {price:,.4f} USDT"
        logger.info(f"Abriu {side} {symbol} @ {price} qty={qty}")

    def _close(self, symbol: str, reason: str, current_price: float):
        from models import db, Trade
        with self._lock:
            if not self.position:
                return
            pos = dict(self.position)
            self.position = None

        close_side = "sell" if pos["side"] == "long" else "buy"
        self._exchange.create_market_order(symbol, close_side, pos["qty"])

        if pos["side"] == "long":
            pnl = (current_price - pos["entry"]) * pos["qty"]
        else:
            pnl = (pos["entry"] - current_price) * pos["qty"]

        with self.app.app_context():
            trade = Trade.query.get(pos["trade_id"])
            if trade:
                trade.exit_price  = current_price
                trade.pnl         = round(pnl, 4)
                trade.status      = "closed"
                trade.close_reason = reason
                trade.closed_at   = datetime.utcnow()
                db.session.commit()

        sign = "+" if pnl >= 0 else ""
        self.status_msg = f"🔒 Fechado ({reason}) | PnL: {sign}{pnl:.2f} USDT"
        logger.info(f"Fechou {pos['side']} @ {current_price} | PnL={pnl:.2f} | {reason}")

    # ─── Loop principal ────────────────────────────────────────────────────────

    def _run(self):
        from models import db, Settings, SessionStats

        # ── Inicialização ──
        with self.app.app_context():
            settings = Settings.query.first()
            if not settings or not settings.has_api:
                self.status_msg = "❌ Configure as chaves de API no Dashboard → Configurações"
                self.running = False
                return

            try:
                self._init_exchange(settings)
                balance = self._balance()
            except Exception as e:
                self.status_msg = f"❌ Falha ao conectar exchange: {e}"
                self.running = False
                return

            self.session_start_balance = balance

            stats = SessionStats.query.first()
            if not stats:
                stats = SessionStats()
                db.session.add(stats)
            stats.session_start   = datetime.utcnow()
            stats.initial_balance = balance
            stats.current_balance = balance
            stats.total_trades    = 0
            stats.winning_trades  = 0
            stats.total_pnl       = 0.0
            db.session.commit()

        self.status_msg = "🟢 Ativo — analisando mercado..."

        # ── Ciclo ──
        while self.running:
            try:
                with self.app.app_context():
                    settings = Settings.query.first()
                    symbol       = settings.symbol
                    timeframe    = settings.timeframe
                    trailing_pct = settings.trailing_pct
                    size_pct     = settings.position_size_pct

                df = compute_indicators(self._ohlcv(symbol, timeframe, 102))
                self.indicators = indicator_summary(df)

                ticker = self._exchange.fetch_ticker(symbol)
                self.current_price = float(ticker["last"])
                price = self.current_price

                # ── Gestão de posição aberta ──────────────────────────────────
                with self._lock:
                    pos = dict(self.position) if self.position else None

                if pos:
                    side = pos["side"]
                    # Atualiza trailing_pct com o valor mais recente das settings
                    tp = pos.get("trailing_pct") or trailing_pct

                    # Atualiza pico
                    with self._lock:
                        if self.position:
                            if side == "long" and price > self.position["peak"]:
                                self.position["peak"] = price
                            elif side == "short" and price < self.position["peak"]:
                                self.position["peak"] = price
                            peak = self.position["peak"]
                        else:
                            peak = pos["peak"]

                    in_profit = (
                        (side == "long"  and price > pos["entry"]) or
                        (side == "short" and price < pos["entry"])
                    )

                    # Capital Shield: protege saldo da sessão
                    balance = self._balance()
                    if balance < self.session_start_balance * Config.CAPITAL_SHIELD_PCT:
                        self._close(symbol, "Escudo de Capital ativado", price)
                        time.sleep(30)
                        continue

                    # Trailing Stop
                    if check_trailing_stop(peak, price, side, tp):
                        if in_profit:
                            self._close(symbol, f"Trailing Stop {tp}%", price)
                        # Se não está em lucro, aguarda reversão
                        else:
                            self.status_msg = f"⚠️ Trailing ativado mas sem lucro — aguardando..."

                    # Reversão de tendência
                    elif check_reversal(df, side):
                        if in_profit:
                            self._close(symbol, "Reversão detectada", price)
                        else:
                            self.status_msg = f"⚠️ Reversão detectada sem lucro — mantendo"

                # ── Busca nova entrada ────────────────────────────────────────
                else:
                    signal = get_signal(df)
                    self.last_signal = signal

                    if signal != "none":
                        with self.app.app_context():
                            balance  = self._balance()
                            settings = Settings.query.first()
                            self._open(symbol, signal, settings.position_size_pct, balance)

                            # Atualiza stats
                            stats = SessionStats.query.first()
                            if stats:
                                stats.total_trades += 1
                                stats.current_balance = balance
                                db.session.commit()
                    else:
                        rsi_val = self.indicators.get("rsi", 0)
                        adx_val = self.indicators.get("adx", 0)
                        self.status_msg = (
                            f"🔍 Analisando {symbol} | RSI {rsi_val:.1f} | ADX {adx_val:.1f}"
                        )

                sleep_s = CYCLE_SLEEP.get(timeframe, 60)
                time.sleep(sleep_s)

            except ccxt.RateLimitExceeded:
                logger.warning("Rate limit atingido — aguardando 60s")
                time.sleep(60)
            except ccxt.NetworkError as e:
                logger.warning(f"Erro de rede: {e} — aguardando 30s")
                time.sleep(30)
            except Exception as e:
                logger.exception(f"Erro inesperado no loop: {e}")
                self.status_msg = f"⚠️ Erro: {e}"
                time.sleep(30)

        self.status_msg = "⛔ Bot parado."
