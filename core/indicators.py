"""
Technical indicators.

Consolidated from three near-identical copies in:
  - model_selector_XGBOOSt/indicators.py
  - model_selector_lstm/indicators.py
  - trading_bot_1_for_tests/indicators.py

All functions take a DataFrame with capitalized OHLCV columns
(Open, High, Low, Close, Volume) and return the same DataFrame
with new columns added.
"""

import numpy as np
import pandas as pd
from scipy.stats import linregress


def calculate_ADX(df: pd.DataFrame, period: int) -> pd.DataFrame:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    plus_dm = pd.Series(0.0, index=df.index, dtype="float64")
    minus_dm = pd.Series(0.0, index=df.index, dtype="float64")
    up_move = high.diff()
    down_move = -(low.diff())
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    atr = tr.rolling(window=period).mean()
    smoothed_plus_dm = plus_dm.rolling(window=period).mean()
    smoothed_minus_dm = minus_dm.rolling(window=period).mean()

    plus_di = 100 * (smoothed_plus_dm / atr)
    minus_di = 100 * (smoothed_minus_dm / atr)
    plus_di = plus_di.replace([np.inf, -np.inf], 0)
    minus_di = minus_di.replace([np.inf, -np.inf], 0)

    df["Plus_DI"] = plus_di
    df["Minus_DI"] = minus_di
    df["DI_diff"] = plus_di - minus_di

    sum_di = plus_di + minus_di
    dx = 100 * ((plus_di - minus_di).abs() / sum_di)
    dx = dx.replace([np.inf, -np.inf], 0).fillna(0)
    df["ADX"] = dx.rolling(window=period).mean()
    return df


def calculate_ATR(df: pd.DataFrame, period: int) -> pd.DataFrame:
    hl = df["High"] - df["Low"]
    hpc = (df["High"] - df["Close"].shift(1)).abs()
    lpc = (df["Low"] - df["Close"].shift(1)).abs()
    df["TR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["ATR"] = df["TR"].rolling(window=period).mean()
    return df


def calculate_bollinger_bands(df: pd.DataFrame, window: int, num_std_dev: float) -> pd.DataFrame:
    middle = df["Close"].rolling(window=window).mean()
    std = df["Close"].rolling(window=window).std()
    df["BB_Middle"] = middle
    df["BB_Upper"] = middle + std * num_std_dev
    df["BB_Lower"] = middle - std * num_std_dev
    return df


def calculate_EMA(df: pd.DataFrame, span: int) -> pd.DataFrame:
    df["EMA"] = df["Close"].ewm(span=span, adjust=False).mean()
    return df


def calculate_moving_averages(df: pd.DataFrame, ma1: int, ma2: int, ma3: int) -> pd.DataFrame:
    df["MA_1"] = df["Close"].rolling(window=ma1).mean()
    df["MA_2"] = df["Close"].rolling(window=ma2).mean()
    df["MA_3"] = df["Close"].rolling(window=ma3).mean()
    return df


def calculate_MACD(df: pd.DataFrame) -> pd.DataFrame:
    ema_12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["EMA_12"] = ema_12
    df["EMA_26"] = ema_26
    df["MACD"] = ema_12 - ema_26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    return df


def calculate_OBV(df: pd.DataFrame) -> pd.DataFrame:
    # Vectorized: sign of close change times volume, cumulative sum.
    direction = np.sign(df["Close"].diff()).fillna(0)
    df["OBV"] = (direction * df["Volume"]).cumsum()
    return df


def calculate_RSI(df: pd.DataFrame, length: int) -> pd.DataFrame:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def calculate_linear_regression(df: pd.DataFrame, period: int, std_mult: float) -> pd.DataFrame:
    def _line_end(x):
        slope, intercept, *_ = linregress(np.arange(len(x)), x)
        return slope * len(x) + intercept

    def _slope(x):
        slope, *_ = linregress(np.arange(len(x)), x)
        return slope

    df["LRC_Middle"] = df["Close"].rolling(window=period).apply(_line_end, raw=False)
    std = df["Close"].rolling(window=period).std()
    df["LRC_Upper"] = df["LRC_Middle"] + std * std_mult
    df["LRC_Lower"] = df["LRC_Middle"] - std * std_mult
    df["Dist_to_Upper"] = (df["Close"] - df["LRC_Upper"]) / df["Close"]
    df["LRC_Slope"] = df["Close"].rolling(20).apply(_slope, raw=False)
    df["LRC_Breakout"] = ((df["Close"] > df["LRC_Upper"]) | (df["Close"] < df["LRC_Lower"])).astype(int)
    return df


def calculate_last_lows(df: pd.DataFrame, p1: int, p2: int, p3: int) -> pd.DataFrame:
    df[f"Last{p1}low"] = df["Close"].rolling(window=p1, min_periods=1).min()
    df[f"Last{p2}low"] = df["Close"].rolling(window=p2, min_periods=1).min()
    df[f"Last{p3}low"] = df["Close"].rolling(window=p3, min_periods=1).min()
    return df


def calculate_indicators(
    df: pd.DataFrame,
    *,
    adx_period: int = 14,
    atr_period: int = 30,
    bb_window: int = 20,
    bb_num_std_dev: float = 2.0,
    ema_span: int = 15,
    ma_1: int = 50,
    ma_2: int = 200,
    ma_3: int = 14,
    rsi_length: int = 14,
    lr_period: int = 20,
    lr_std_mult: float = 2.0,
    lastlow_periods: tuple[int, int, int] = (10, 50, 100),
) -> pd.DataFrame:
    """Compute the full indicator stack on a fresh copy of the input frame."""
    out = df.copy()
    out = calculate_ADX(out, adx_period)
    out = calculate_ATR(out, atr_period)
    out = calculate_bollinger_bands(out, bb_window, bb_num_std_dev)
    out = calculate_EMA(out, ema_span)
    out = calculate_moving_averages(out, ma_1, ma_2, ma_3)
    out = calculate_MACD(out)
    out = calculate_OBV(out)
    out = calculate_RSI(out, rsi_length)
    out = calculate_linear_regression(out, lr_period, lr_std_mult)
    out = calculate_last_lows(out, *lastlow_periods)
    return out


def calculate_dynamic_threshold(df: pd.DataFrame, window: int = 5) -> float:
    """Median absolute pct change over `window` bars, scaled by 1.5."""
    pct = df["Close"].pct_change().abs()
    median_vol = pct.rolling(window).median().iloc[-1]
    return round(float(median_vol) * 1.5, 6)


def capitalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename ohlcv → Ohlcv. Matches the convention from the IB raw frames."""
    df = df.copy()
    df.columns = [c[0].upper() + c[1:].lower() for c in df.columns]
    return df
