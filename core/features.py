"""
Named feature sets the models can be trained / scored on.

Originally three different settings.py files each defined their own
config1/config2 with subtle differences. Consolidated here.
"""

# Indicators only (no raw OHLC). Matches the old `config1` in trading_bot_1_for_tests.
CONFIG_INDICATORS_ONLY = [
    "RSI", "MACD", "EMA",
    "BB_Upper", "BB_Lower",
    "OBV", "ATR", "ADX",
    "MA_1", "MA_2", "MA_3",
    "Volume",
    "LRC_Middle", "LRC_Slope", "LRC_Breakout",
]

# OHLC + indicators + recent lows. Matches the old `config2` in model_selector folders.
CONFIG_FULL = [
    "Open", "High", "Low", "Close",
    "RSI", "MACD", "EMA",
    "BB_Upper", "BB_Lower",
    "OBV", "ATR", "ADX",
    "MA_1", "MA_2", "MA_3",
    "Volume",
    "LRC_Middle", "LRC_Slope",
    "Last10low", "Last50low", "Last100low",
]

# Raw OHLCV only. Matches the old `config3`.
CONFIG_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


FEATURE_SETS = {
    "indicators_only": CONFIG_INDICATORS_ONLY,
    "full": CONFIG_FULL,
    "ohlcv": CONFIG_OHLCV,
}


def get_features(name: str) -> list[str]:
    if name not in FEATURE_SETS:
        raise ValueError(f"Unknown feature set {name!r}. Available: {list(FEATURE_SETS)}")
    return list(FEATURE_SETS[name])
