"""
Configuration loader.

Loads:
  - default indicator parameters (matching the legacy settings.py values)
  - per-symbol overrides from config/symbols.yaml
  - runtime/env settings from .env (IB host, port, paper/live, etc.)

Nothing here is symbol-specific by default — pass a symbol to load_symbol().
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"
ARTIFACTS_DIR = ROOT / "models" / "artifacts"

# Hard lock — paper-only.
# Phase 7+ uses Client Portal Gateway (CPGW), which only exposes port 5000.
# Paper vs live is decided by:
#   1. PAPER=true in .env (refuses to start if false)
#   2. LOGIN_TYPE=2 on the SRP-6a login (set in core.ibkr_auth)
#   3. IBKR server confirms paper=true in the SRP INIT response (validated)
# All three must agree before any order can be sent.
PAPER_PORTS: set[int] = {5000}


@dataclass
class IndicatorParams:
    """Indicator hyperparameters. Defaults match the legacy settings.py."""

    rsi_length: int = 14
    overbought: int = 75
    oversold: int = 25
    ema_span: int = 15
    bb_window: int = 20
    bb_num_std_dev: float = 2.0
    atr_period: int = 30
    ma_1: int = 50
    ma_2: int = 200
    ma_3: int = 14
    adx_period: int = 14
    lr_period: int = 20
    lr_std_mult: float = 2.0
    lastlow_periods: tuple[int, int, int] = (10, 50, 100)


@dataclass
class TradingParams:
    """Risk / sizing / prediction params."""

    rr_ratio: float = 4.0  # Risk-reward ratio for bracket take-profit
    risk_pct: float = 0.02  # Risk per trade
    atr_multiplier: float = 2.0  # ATR multiplier for stop placement
    shift: int = 5  # Periods ahead to predict (target horizon)
    confidence_threshold: float = 0.5
    prediction_interval_sec: int = 20


@dataclass
class SymbolConfig:
    """Per-symbol config loaded from symbols.yaml."""

    symbol: str
    feature_set: str = "full"
    bar_size: str = "5 mins"
    history_duration: str = "5 D"
    timeframe_label: str = "5min"
    model: str | None = None  # Registry name; portfolio runner uses this
    indicator: IndicatorParams = field(default_factory=IndicatorParams)
    trading: TradingParams = field(default_factory=TradingParams)
    enabled: bool = True


@dataclass
class PortfolioConfig:
    """Portfolio-level sizing config, loaded from the `portfolio:` block in symbols.yaml."""

    starting_equity: float = 100_000.0
    cycle_interval_sec: int = 60
    reentry_cooldown_sec: int = 600


@dataclass
class TacticalSleeveConfig:
    """Short-term sleeve. DISABLED in Phase 8 (enabled=false in config)."""

    enabled: bool = False
    allocation_pct: float = 0.0
    universe: list[str] = field(default_factory=lambda: ["SPY", "AAPL", "QQQ", "GLD"])
    ta_weight: float = 0.70
    sentiment_weight: float = 0.30
    target_vol_per_bar: float = 0.005
    vol_lookback: int = 20
    max_per_symbol_pct: float = 0.40
    max_gross_pct: float = 1.0
    confidence_floor: float = 0.55


@dataclass
class CoreSleeveConfig:
    """
    Medium-term sleeve (Phase 8).

    Weekly evaluation, 1–3 month horizon. Score each asset in the universe by
    momentum + macro alignment + trend, hold the top N equal-weighted.
    Hard risk-off filter to bonds when SPY is below its 200-day MA AND VIX
    is above the threshold.
    """

    enabled: bool = True
    allocation_pct: float = 0.70
    universe: list[str] = field(
        default_factory=lambda: [
            "SPY",
            "IWM",
            "EFA",
            "VWO",
            "TLT",
            "IEF",
            "AGG",
            "GLD",
            "DBC",
            "BIL",
        ]
    )
    hold_top_n: int = 4
    momentum_weight: float = 0.40
    macro_weight: float = 0.30
    trend_weight: float = 0.30
    momentum_lookback_days: int = 252
    trend_ma_days: int = 200
    risk_off_spy_ma_days: int = 200
    risk_off_vix_threshold: float = 25.0
    risk_off_universe: list[str] = field(default_factory=lambda: ["TLT", "IEF"])
    rebalance_day_of_week: int = 4  # 0=Mon, 4=Fri
    drift_threshold: float = 0.08


@dataclass
class SentimentSatelliteConfig:
    """News-sentiment satellite sleeve (Phase 8B)."""

    enabled: bool = False
    allocation_pct: float = 0.20
    universe: list[str] = field(
        default_factory=lambda: [
            "SPY",
            "QQQ",
            "AAPL",
            "MSFT",
            "NVDA",
            "TSLA",
            "AMZN",
            "META",
            "GOOGL",
        ]
    )
    sentiment_threshold: float = 0.4
    min_article_count: int = 5
    max_concurrent_positions: int = 4
    position_pct_of_sleeve: float = 0.05
    hold_max_days: int = 10
    stop_loss_pct: float = 0.05
    longs_only: bool = True


@dataclass
class MacroConfig:
    """FRED + yfinance feeds powering the core sleeve's macro score."""

    fred_series: dict[str, str] = field(
        default_factory=lambda: {
            "yield_curve": "T10Y2Y",
            "fed_funds": "DFF",
            "ten_year": "DGS10",
            "two_year": "DGS2",
            "cpi": "CPIAUCSL",
            "unemployment": "UNRATE",
        }
    )
    yf_tickers: dict[str, str] = field(
        default_factory=lambda: {
            "vix": "^VIX",
            "dxy": "DX-Y.NYB",
            "hyg": "HYG",
            "lqd": "LQD",
        }
    )
    cache_ttl_sec: int = 21600


@dataclass
class LongTermSleeveConfig:
    """Long-term sleeve: fixed weights (All-Weather), monthly/drift rebalance."""

    allocation_pct: float = 0.80
    strategy: str = "all_weather"
    rebalance_freq_days: int = 30
    drift_threshold: float = 0.05
    universe: dict[str, float] = field(
        default_factory=lambda: {
            "SPY": 0.30,
            "TLT": 0.40,
            "IEF": 0.15,
            "DBC": 0.075,
            "GLD": 0.075,
        }
    )


@dataclass
class SentimentConfig:
    """Sentiment provider + polling cadence."""

    provider: str = "stub"
    poll_interval_sec: int = 3600
    staleness_sec: int = 7200
    symbols: list[str] = field(default_factory=lambda: ["SPY", "AAPL", "QQQ", "GLD"])


@dataclass
class IBConfig:
    """
    CPGW connection params, loaded from env.

    The field is still named `IBConfig` for backwards compatibility with the
    rest of the codebase, but it now points at the Client Portal Gateway
    (default port 5000), not the TWS IB Gateway.
    """

    host: str = "localhost"
    port: int = 5000  # CPGW default; only paper-port allowed
    client_id: int = 0  # kept for legacy callers; CPGW doesn't use it


@dataclass
class AppConfig:
    """Top-level app config."""

    paper: bool = True
    dry_run: bool = True
    ib: IBConfig = field(default_factory=IBConfig)
    universe: list[str] = field(default_factory=list)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_app_config() -> AppConfig:
    load_dotenv(ROOT / ".env")
    cfg = AppConfig(
        paper=_env_bool("PAPER", True),
        dry_run=_env_bool("DRY_RUN", True),
        ib=IBConfig(
            host=os.getenv("IB_HOST", "localhost"),
            port=int(os.getenv("IB_PORT", "5000")),
            client_id=int(os.getenv("IB_CLIENT_ID", "0")),
        ),
    )
    symbols_yaml = _load_yaml(CONFIG_DIR / "symbols.yaml")
    cfg.universe = list((symbols_yaml.get("universe") or {}).keys())
    return cfg


def assert_paper_mode(cfg: AppConfig) -> None:
    """
    Hard guard. Every entry point that opens an IB connection must call this first.
    Refuses to start if .env was set to live mode or IB_PORT points at a live socket.
    """
    if not cfg.paper:
        raise RuntimeError(
            "Refusing to start: PAPER=false in .env. This bot is hard-locked to paper trading "
            "(see core.config.assert_paper_mode). Set PAPER=true to continue."
        )
    if cfg.ib.port not in PAPER_PORTS:
        raise RuntimeError(
            f"Refusing to start: IB_PORT={cfg.ib.port} is not a paper port. "
            f"Allowed: {sorted(PAPER_PORTS)} (IBKR Client Portal Gateway). "
            f"Set IB_PORT={min(PAPER_PORTS)} in .env."
        )


def load_symbol(symbol: str) -> SymbolConfig:
    """Load per-symbol config, merging defaults with overrides from symbols.yaml."""
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    universe = data.get("universe") or {}
    if symbol not in universe:
        raise KeyError(f"Symbol {symbol!r} not in config/symbols.yaml")
    raw = universe[symbol] or {}

    ind_raw = raw.get("indicator") or {}
    trd_raw = raw.get("trading") or {}

    if "lastlow_periods" in ind_raw and isinstance(ind_raw["lastlow_periods"], list):
        ind_raw["lastlow_periods"] = tuple(ind_raw["lastlow_periods"])

    return SymbolConfig(
        symbol=symbol,
        feature_set=raw.get("feature_set", "full"),
        bar_size=raw.get("bar_size", "5 mins"),
        history_duration=raw.get("history_duration", "5 D"),
        timeframe_label=raw.get("timeframe_label", "5min"),
        model=raw.get("model"),
        indicator=IndicatorParams(**ind_raw),
        trading=TradingParams(**trd_raw),
        enabled=raw.get("enabled", True),
    )


def load_portfolio() -> PortfolioConfig:
    """Load the `portfolio:` block from config/symbols.yaml, or defaults."""
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    raw = data.get("portfolio") or {}
    return PortfolioConfig(**raw)


def load_sleeves() -> tuple[LongTermSleeveConfig, TacticalSleeveConfig]:
    """Legacy: returns the old (long_term, tactical) tuple. Kept for back-compat
    with older callers. New code should use load_phase8_sleeves()."""
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    sleeves = data.get("sleeves") or {}
    lt_raw = sleeves.get("long_term") or {}
    tc_raw = sleeves.get("tactical") or {}
    return LongTermSleeveConfig(**lt_raw), TacticalSleeveConfig(**tc_raw)


def load_phase8_sleeves() -> tuple[
    CoreSleeveConfig, SentimentSatelliteConfig, TacticalSleeveConfig
]:
    """Phase 8 sleeves: core (medium-term) + sentiment satellite + (disabled) tactical."""
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    sleeves = data.get("sleeves") or {}
    return (
        CoreSleeveConfig(**(sleeves.get("core") or {})),
        SentimentSatelliteConfig(**(sleeves.get("sentiment") or {})),
        TacticalSleeveConfig(**(sleeves.get("tactical") or {})),
    )


def load_macro_config() -> MacroConfig:
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    raw = data.get("macro") or {}
    return MacroConfig(**raw)


def load_sentiment_config() -> SentimentConfig:
    data = _load_yaml(CONFIG_DIR / "symbols.yaml")
    # New key in Phase 8 is `sentiment_provider`; fall back to legacy `sentiment` block
    raw = data.get("sentiment_provider") or data.get("sentiment") or {}
    return SentimentConfig(**raw)
