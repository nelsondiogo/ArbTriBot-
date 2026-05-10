"""
Aplicação Flask — Dashboard + API de controle do bot.
"""
import logging
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash,
)
from flask_sqlalchemy import SQLAlchemy

from config import Config
from models import db, Settings, Trade, SessionStats
from crypto_utils import encrypt, decrypt, mask
from bot_engine import BotEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ─── Factory ──────────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = Config.FLASK_SECRET
    app.config["SQLALCHEMY_DATABASE_URI"]        = Config.DATABASE_URL
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()
        if not Settings.query.first():
            db.session.add(Settings())
            db.session.commit()
        if not SessionStats.query.first():
            db.session.add(SessionStats(initial_balance=0, current_balance=0))
            db.session.commit()

    bot = BotEngine(app)

    # ─── Auth ──────────────────────────────────────────────────────────────────

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    # ─── Rotas públicas ────────────────────────────────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "bot_running": bot.running})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if request.form.get("password") == Config.DASHBOARD_PASSWORD:
                session["logged_in"] = True
                return redirect(url_for("dashboard"))
            return render_template("login.html", error="Senha incorreta.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ─── Dashboard principal ───────────────────────────────────────────────────

    @app.route("/")
    @login_required
    def dashboard():
        settings = Settings.query.first()
        trades   = Trade.query.order_by(Trade.opened_at.desc()).limit(25).all()
        stats    = SessionStats.query.first()

        closed  = Trade.query.filter_by(status="closed").all()
        total_pnl = sum(t.pnl for t in closed if t.pnl is not None)
        wins      = sum(1 for t in closed if t.pnl and t.pnl > 0)
        win_rate  = (wins / len(closed) * 100) if closed else 0.0

        return render_template(
            "dashboard.html",
            settings=settings,
            trades=trades,
            stats=stats,
            total_pnl=total_pnl,
            win_rate=win_rate,
            bot=bot,
            config=Config,
        )

    # ─── Configurações ────────────────────────────────────────────────────────

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        settings = Settings.query.first()

        if request.method == "POST":
            action = request.form.get("action")

            if action == "save_api":
                key    = request.form.get("api_key", "").strip()
                secret = request.form.get("api_secret", "").strip()
                exc    = request.form.get("exchange", "bybit")

                if key and secret:
                    settings.api_key_encrypted    = encrypt(key,    Config.FLASK_SECRET)
                    settings.api_secret_encrypted = encrypt(secret, Config.FLASK_SECRET)
                    settings.exchange  = exc
                    settings.updated_at = datetime.utcnow()
                    db.session.commit()
                    flash("✅ Chaves de API salvas com criptografia.", "success")
                else:
                    flash("❌ Preencha API Key e API Secret.", "error")

            elif action == "save_trading":
                settings.symbol           = request.form.get("symbol", "BTC/USDT").strip().upper()
                settings.timeframe        = request.form.get("timeframe", "15m")
                settings.trailing_pct     = float(request.form.get("trailing_pct", 1.5))
                settings.position_size_pct = float(request.form.get("position_size_pct", 10))
                settings.updated_at       = datetime.utcnow()
                db.session.commit()

                # Atualiza trailing stop ao vivo sem parar o bot
                bot.update_trailing(settings.trailing_pct)
                flash("✅ Configurações de trading atualizadas.", "success")

            return redirect(url_for("settings_page"))

        # Máscara das chaves para exibição
        key_mask    = mask(settings.api_key_encrypted)    if settings.api_key_encrypted    else ""
        secret_mask = mask(settings.api_secret_encrypted) if settings.api_secret_encrypted else ""

        return render_template(
            "settings.html",
            settings=settings,
            key_mask=key_mask,
            secret_mask=secret_mask,
        )

    # ─── Controle do bot ──────────────────────────────────────────────────────

    @app.route("/bot/start", methods=["POST"])
    @login_required
    def bot_start():
        settings = Settings.query.first()
        if not settings.has_api:
            return jsonify({"ok": False, "msg": "Configure as chaves de API primeiro."})
        if bot.running:
            return jsonify({"ok": False, "msg": "Bot já está em execução."})
        bot.start()
        settings.bot_active = True
        db.session.commit()
        return jsonify({"ok": True, "msg": "Bot iniciado com sucesso."})

    @app.route("/bot/stop", methods=["POST"])
    @login_required
    def bot_stop():
        bot.stop()
        settings = Settings.query.first()
        settings.bot_active = False
        db.session.commit()
        return jsonify({"ok": True, "msg": "Bot parado."})

    # ─── API de status (polling do Dashboard) ────────────────────────────────

    @app.route("/api/status")
    @login_required
    def api_status():
        stats   = SessionStats.query.first()
        closed  = Trade.query.filter_by(status="closed").all()
        total_pnl = sum(t.pnl for t in closed if t.pnl is not None)

        pos     = bot.position
        pos_pnl = 0.0
        if pos:
            cp = bot.current_price
            pos_pnl = (
                (cp - pos["entry"]) * pos["qty"] if pos["side"] == "long"
                else (pos["entry"] - cp) * pos["qty"]
            )

        return jsonify({
            "running":          bot.running,
            "status_msg":       bot.status_msg,
            "price":            bot.current_price,
            "last_signal":      bot.last_signal,
            "indicators":       bot.indicators,
            "total_pnl":        round(total_pnl, 2),
            "pos_pnl":          round(pos_pnl, 2),
            "has_position":     bool(pos),
            "position":         pos,
            "balance":          stats.current_balance if stats else 0,
            "initial_balance":  stats.initial_balance if stats else 0,
            "total_trades":     stats.total_trades    if stats else 0,
        })

    @app.route("/api/trades")
    @login_required
    def api_trades():
        trades = Trade.query.order_by(Trade.opened_at.desc()).limit(50).all()
        return jsonify([{
            "id":          t.id,
            "symbol":      t.symbol,
            "side":        t.side,
            "entry":       t.entry_price,
            "exit":        t.exit_price,
            "pnl":         t.pnl,
            "pnl_pct":     round(t.pnl_pct, 2),
            "status":      t.status,
            "reason":      t.close_reason,
            "opened_at":   t.opened_at.isoformat() if t.opened_at else None,
        } for t in trades])

    @app.route("/trades/clear", methods=["POST"])
    @login_required
    def clear_trades():
        Trade.query.delete()
        db.session.commit()
        flash("Histórico de trades limpo.", "success")
        return redirect(url_for("dashboard"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=False)
