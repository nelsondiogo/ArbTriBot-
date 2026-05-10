"""
Estratégia de trading — indicadores técnicos e geração de sinais.

Regras:
  LONG  → RSI entre 35-65 | EMA9 > EMA21 | ADX > 25 | DI+ > DI- | RSI subindo
  SHORT → RSI entre 35-65 | EMA9 < EMA21 | ADX > 25 | DI- > DI+ | RSI caindo
  Trailing Stop: recuo do pico configurável pelo Dashboard
  Reversão: cruzamento de EMA ou mudança brusca de RSI
"""
import logging
import pandas as pd
import pandas_ta as ta
from config import Config

logger = logging.getLogger(__name__)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula RSI, EMA9, EMA21 e ADX sobre um DataFrame OHLCV."""
    df = df.copy()
    df["rsi"]  = ta.rsi(df["close"], length=Config.RSI_PERIOD)
    df["ema9"] = ta.ema(df["close"], length=Config.EMA_FAST)
    df["ema21"]= ta.ema(df["close"], length=Config.EMA_SLOW)

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df["ADX_14"]
    df["dmp"]  = adx_df["DMP_14"]   # +DI (bullish)
    df["dmn"]  = adx_df["DMN_14"]   # -DI (bearish)

    return df.dropna()


def get_signal(df: pd.DataFrame) -> str:
    """
    Avalia os últimos dois candles e retorna 'long', 'short' ou 'none'.
    Aplica todos os filtros de exaustão e confirmação de tendência.
    """
    if len(df) < 3:
        return "none"

    last = df.iloc[-1]
    prev = df.iloc[-2]

    rsi  = last["rsi"]
    adx  = last["adx"]
    ema9 = last["ema9"]
    ema21 = last["ema21"]
    dmp  = last["dmp"]
    dmn  = last["dmn"]

    # ── Filtro 1: ADX confirma tendência ───────────────────────────────────────
    if adx < Config.ADX_MIN:
        logger.debug(f"[SINAL] ADX={adx:.1f} < {Config.ADX_MIN} → sem tendência")
        return "none"

    # ── Filtro 2: RSI em zona neutra (evitar extremos) ─────────────────────────
    if rsi <= Config.RSI_OVERSOLD:
        logger.debug(f"[SINAL] RSI={rsi:.1f} em zona oversold → SHORT bloqueado")
        # Não permitimos SHORT no fundo
        # Verificamos apenas LONG com momentum de retomada
        if (ema9 > ema21 and dmp > dmn and last["rsi"] > prev["rsi"]):
            return "long"
        return "none"

    if rsi >= Config.RSI_OVERBOUGHT:
        logger.debug(f"[SINAL] RSI={rsi:.1f} em zona overbought → LONG bloqueado")
        # Não permitimos LONG no topo
        if (ema9 < ema21 and dmn > dmp and last["rsi"] < prev["rsi"]):
            return "short"
        return "none"

    # ── Zona neutra: sinais completos ─────────────────────────────────────────
    rsi_rising  = last["rsi"] > prev["rsi"]
    rsi_falling = last["rsi"] < prev["rsi"]

    if ema9 > ema21 and dmp > dmn and rsi_rising:
        logger.info(f"[SINAL] LONG — RSI={rsi:.1f} ADX={adx:.1f} EMA9>{ema21:.1f}")
        return "long"

    if ema9 < ema21 and dmn > dmp and rsi_falling:
        logger.info(f"[SINAL] SHORT — RSI={rsi:.1f} ADX={adx:.1f} EMA9<{ema21:.1f}")
        return "short"

    return "none"


def check_trailing_stop(peak: float, current: float, side: str,
                         trailing_pct: float) -> bool:
    """
    Retorna True se o recuo do pico atingiu o trailing stop configurado.
    """
    if side == "long":
        drawdown = (peak - current) / peak * 100
        return drawdown >= trailing_pct
    else:  # short: pico = menor preço atingido
        rise = (current - peak) / peak * 100
        return rise >= trailing_pct


def check_reversal(df: pd.DataFrame, side: str) -> bool:
    """
    Detecta inversão de tendência por cruzamento de EMA ou reversão de RSI.
    """
    if len(df) < 2:
        return False

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if side == "long":
        ema_cross = (last["ema9"] < last["ema21"]) and (prev["ema9"] >= prev["ema21"])
        rsi_drop  = (last["rsi"] < 45) and (prev["rsi"] > 52)
        return ema_cross or rsi_drop

    else:  # short
        ema_cross = (last["ema9"] > last["ema21"]) and (prev["ema9"] <= prev["ema21"])
        rsi_rise  = (last["rsi"] > 55) and (prev["rsi"] < 48)
        return ema_cross or rsi_rise


def indicator_summary(df: pd.DataFrame) -> dict:
    """Retorna resumo dos indicadores do último candle para o Dashboard."""
    if df.empty:
        return {}
    last = df.iloc[-1]
    return {
        "rsi":  round(last.get("rsi", 0), 2),
        "ema9": round(last.get("ema9", 0), 4),
        "ema21":round(last.get("ema21", 0), 4),
        "adx":  round(last.get("adx", 0), 2),
        "dmp":  round(last.get("dmp", 0), 2),
        "dmn":  round(last.get("dmn", 0), 2),
    }
