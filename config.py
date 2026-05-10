import os

class Config:
    # ─── Segurança ─────────────────────────────────────────────────────────────
    FLASK_SECRET      = os.environ.get("FLASK_SECRET", "TROQUE-ESTA-CHAVE-NO-RENDER")
    DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

    # ─── Servidor ──────────────────────────────────────────────────────────────
    PORT = int(os.environ.get("PORT", 5000))
    # Render monta o disco persistente em /data — use DB_DIR=/ data no painel
    _DB_DIR      = os.environ.get("DB_DIR", ".")
    DATABASE_URL = f"sqlite:///{_DB_DIR}/bot.db"

    # ─── Indicadores (estáticos — não mudar aqui, usar Dashboard) ──────────────
    EMA_FAST       = 9
    EMA_SLOW       = 21
    RSI_PERIOD     = 14
    ADX_MIN        = 25
    RSI_OVERSOLD   = 35   # Proibir SHORT abaixo deste valor
    RSI_OVERBOUGHT = 65   # Proibir LONG acima deste valor

    # ─── Padrões de trading ────────────────────────────────────────────────────
    DEFAULT_SYMBOL         = "BTC/USDT"
    DEFAULT_TIMEFRAME      = "15m"
    DEFAULT_TRAILING_PCT   = 1.5
    DEFAULT_SIZE_PCT       = 10.0
    DEFAULT_EXCHANGE       = "bybit"

    # ─── Capital Shield ────────────────────────────────────────────────────────
    CAPITAL_SHIELD_PCT = 0.99  # nunca fechar se saldo < 99% do inicial da sessão
