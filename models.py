"""
Modelos SQLAlchemy — persistência local via SQLite.
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Settings(db.Model):
    """Configurações persistentes — API keys + parâmetros de trading."""
    __tablename__ = "settings"

    id                  = db.Column(db.Integer, primary_key=True)
    exchange            = db.Column(db.String(20), default="bybit")
    api_key_encrypted   = db.Column(db.Text, nullable=True)
    api_secret_encrypted = db.Column(db.Text, nullable=True)
    symbol              = db.Column(db.String(20), default="BTC/USDT")
    timeframe           = db.Column(db.String(5), default="15m")
    trailing_pct        = db.Column(db.Float, default=1.5)
    position_size_pct   = db.Column(db.Float, default=10.0)
    bot_active          = db.Column(db.Boolean, default=False)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def has_api(self) -> bool:
        return bool(self.api_key_encrypted and self.api_secret_encrypted)


class Trade(db.Model):
    """Histórico de ordens executadas."""
    __tablename__ = "trades"

    id           = db.Column(db.Integer, primary_key=True)
    symbol       = db.Column(db.String(20), nullable=False)
    side         = db.Column(db.String(5), nullable=False)   # long | short
    entry_price  = db.Column(db.Float, nullable=False)
    exit_price   = db.Column(db.Float, nullable=True)
    quantity     = db.Column(db.Float, nullable=False)
    pnl          = db.Column(db.Float, nullable=True)
    status       = db.Column(db.String(10), default="open")  # open | closed
    close_reason = db.Column(db.String(60), nullable=True)
    opened_at    = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at    = db.Column(db.DateTime, nullable=True)

    @property
    def pnl_pct(self) -> float:
        if not self.exit_price:
            return 0.0
        if self.side == "long":
            return (self.exit_price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - self.exit_price) / self.entry_price * 100


class SessionStats(db.Model):
    """Estatísticas da sessão atual."""
    __tablename__ = "session_stats"

    id              = db.Column(db.Integer, primary_key=True)
    session_start   = db.Column(db.DateTime, default=datetime.utcnow)
    initial_balance = db.Column(db.Float, default=0.0)
    current_balance = db.Column(db.Float, default=0.0)
    total_trades    = db.Column(db.Integer, default=0)
    winning_trades  = db.Column(db.Integer, default=0)
    total_pnl       = db.Column(db.Float, default=0.0)

    @property
    def win_rate(self) -> float:
        if not self.total_trades:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    @property
    def pnl_pct(self) -> float:
        if not self.initial_balance:
            return 0.0
        return self.total_pnl / self.initial_balance * 100
