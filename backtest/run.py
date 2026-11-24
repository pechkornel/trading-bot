"""Run the core sleeve backtest and write the report.

    python -m backtest.run [--refresh] [--end YYYY-MM-DD] [--cache-dir DIR] [--out-dir DIR]

`run_all` is pure (no network, no files). `main` loads data, runs everything, and only
then writes `results.json`, `results.md` and `equity_curve.png` into the output folder.
On any data error it prints the reason, writes nothing and returns 1. There is no
synthetic or fallback data path.

Importing this module needs neither yfinance nor matplotlib.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest import report
from backtest.ablations import DEFAULT_COST_BPS, VARIANTS, Variant, build_cfg
from backtest.data import DataError, MarketData, load_market_data, required_tickers
from backtest.engine import (
    buy_and_hold,
    first_start_date,
    min_bars_for,
    monthly_rebalance,
    simulate,
    trading_calendar,
)
from backtest.macro_replay import HistoricalMacroFetcher, replay_macro_config
from backtest.metrics import (
    bootstrap_sharpe,
    daily_returns,
    subperiod_stats,
    summarize,
)
from backtest.strategy import CoreReplay, risk_off_days, trigger_counts
from core.config import (
    CoreSleeveConfig,
    MacroConfig,
    load_macro_config,
    load_phase8_sleeves,
    load_portfolio,
)

DEFAULT_CACHE_DIR = "data/cache/backtest"
DEFAULT_OUT_DIR = "docs/backtest"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _benchmark_deciders(cfg: CoreSleeveConfig, cal: pd.DatetimeIndex) -> dict[str, Callable]:
    n = len(cfg.universe)
    return {
        "spy_buy_hold": buy_and_hold("SPY"),
        "sixty_forty": monthly_rebalance({"SPY": 0.6, "AGG": 0.4}, cal),
        "equal_weight": monthly_rebalance({s: 1.0 / n for s in cfg.universe}, cal),
    }


def _sim_symbols(cfg: CoreSleeveConfig) -> list[str]:
    return list(dict.fromkeys([*cfg.universe, *cfg.risk_off_universe, "SPY", "AGG", "BIL"]))


def _check_fred_before(data: MarketData, start: pd.Timestamp, ids: list[str]) -> None:
    for sid in ids:
        s = data.fred.get(sid)
        if s is None or not (s.dropna().index < start).any():
            raise ValueError(f"{sid}: no FRED observation before the start date {start.date()}")


def _weekday_summary(
    cfg: CoreSleeveConfig,
    variant: Variant,
    data: MarketData,
    macro_cfg: MacroConfig,
    cooldown: int,
    adj: pd.DataFrame,
    cal: pd.DatetimeIndex,
    rf: pd.Series,
) -> dict:
    fetcher = HistoricalMacroFetcher(data.raw_close, data.fred, macro_cfg)
    replay = CoreReplay(cfg, data.raw_close, fetcher, cooldown)
    sim = simulate(adj, cal, replay.decide, variant.cost_bps)
    return summarize(sim.equity, rf, sim.turnover)


def run_pipeline(
    data: MarketData,
    core_cfg: CoreSleeveConfig,
    reentry_cooldown_sec: int,
    end: str | None = None,
    macro_cfg: MacroConfig | None = None,
) -> tuple[dict, dict[str, pd.Series]]:
    """Run base, ablations and benchmarks. Returns (results dict, equity curves)."""
    macro_cfg = replay_macro_config(macro_cfg or MacroConfig())
    sim_syms = _sim_symbols(core_cfg)
    missing = [s for s in sim_syms if s not in data.adj_close.columns]
    if missing:
        raise ValueError(f"missing price data for: {', '.join(missing)}")
    adj = data.adj_close[sim_syms]

    all_days = trading_calendar(data.raw_close, data.adj_close, sim_syms)
    required = required_tickers(core_cfg, macro_cfg)
    min_bars = min_bars_for(core_cfg)
    start = first_start_date(data.raw_close, required, min_bars, all_days)
    _check_fred_before(data, start, list(macro_cfg.fred_series.values()))
    cal = all_days[all_days >= start]
    if end is not None:
        cal = cal[cal <= pd.Timestamp(end)]
    if len(cal) < 3:
        raise ValueError(f"fewer than 3 trading days between {start.date()} and {end}")

    rows = data.raw_close[sim_syms].dropna(how="all").index
    dropped = int(((rows >= cal[0]) & (rows <= cal[-1])).sum() - len(cal))
    rf = adj["BIL"].reindex(cal).pct_change().dropna()

    variants: dict[str, dict] = {}
    curves: dict[str, pd.Series] = {}
    strategy_by_cost: dict[str, dict] = {}
    for v in VARIANTS:
        cfg = build_cfg(core_cfg, v)
        fetcher = HistoricalMacroFetcher(data.raw_close, data.fred, macro_cfg)
        replay = CoreReplay(cfg, data.raw_close, fetcher, reentry_cooldown_sec)
        sim = simulate(adj, cal, replay.decide, v.cost_bps)
        flags = risk_off_days(cal, sim.fills, replay.decisions)
        summary = summarize(sim.equity, rf, sim.turnover, risk_off=float(flags.mean()))
        summary.update(
            description=v.description,
            cost_bps=v.cost_bps,
            triggers=trigger_counts(replay.decisions),
            evaluations=len(replay.decisions),
            fills=len(sim.fills),
        )
        variants[v.name] = summary
        if v.name == "base":
            curves["Strategy"] = sim.equity
        if not v.overrides:
            strategy_by_cost[f"{v.cost_bps:g}"] = summary

    base_variant = next(v for v in VARIANTS if v.name == "base")
    weekday: dict[str, dict] = {}
    for dow, name in enumerate(WEEKDAY_NAMES):
        live = dow == core_cfg.rebalance_day_of_week
        if live:
            m = variants["base"]
        else:
            m = _weekday_summary(
                dataclasses.replace(build_cfg(core_cfg, base_variant), rebalance_day_of_week=dow),
                base_variant,
                data,
                macro_cfg,
                reentry_cooldown_sec,
                adj,
                cal,
                rf,
            )
        weekday[str(dow)] = {
            "name": name,
            "live": live,
            **{k: m[k] for k in ("cagr", "volatility", "sharpe", "max_drawdown")},
        }

    benchmarks_by_cost: dict[str, dict] = {}
    for cost in sorted({v.cost_bps for v in VARIANTS}):
        group = {}
        for name, decide in _benchmark_deciders(core_cfg, cal).items():
            sim = simulate(adj, cal, decide, cost)
            group[name] = summarize(sim.equity, rf, sim.turnover)
            if cost == DEFAULT_COST_BPS:
                curves[name] = sim.equity
        benchmarks_by_cost[f"{cost:g}"] = group

    bench_returns = {k: daily_returns(curves[k]) for k in report.BENCHMARK_LABELS}
    strat_returns = daily_returns(curves["Strategy"])
    end_year = cal[-1].year
    periods = [
        ("2008 to 2012", 2008, 2012),
        ("2013 to 2019", 2013, 2019),
        (f"2020 to {end_year}", 2020, end_year),
    ]
    subperiods = {
        "Strategy": subperiod_stats(strat_returns, rf, periods),
        **{k: subperiod_stats(r, rf, periods) for k, r in bench_returns.items()},
    }
    try:
        bootstrap = bootstrap_sharpe(strat_returns, bench_returns, rf)
    except ValueError:  # fewer than two blocks of aligned returns
        bootstrap = None

    manifest = data.manifest or {}
    results = {
        "start_date": cal[0].date().isoformat(),
        "end_date": cal[-1].date().isoformat(),
        "trading_days": len(cal),
        "dropped_days": dropped,
        "min_bars": min_bars,
        "base_cost_bps": DEFAULT_COST_BPS,
        "data": {
            "downloaded_at": manifest.get("downloaded_at"),
            "yfinance_version": manifest.get("yfinance_version"),
            "tickers": manifest.get("tickers", {}),
            "fred": manifest.get("fred", {}),
        },
        "strategy": variants["base"],
        "benchmarks": benchmarks_by_cost[f"{DEFAULT_COST_BPS:g}"],
        "weekday_sensitivity": weekday,
        "subperiods": subperiods,
        "bootstrap": bootstrap,
        "variants": variants,
        "strategy_by_cost": strategy_by_cost,
        "benchmarks_by_cost": benchmarks_by_cost,
    }
    return results, curves


def run_all(
    data: MarketData,
    core_cfg: CoreSleeveConfig,
    reentry_cooldown_sec: int,
    end: str | None = None,
    macro_cfg: MacroConfig | None = None,
) -> dict:
    """Pure: no network, no files. Returns the results dict."""
    return run_pipeline(data, core_cfg, reentry_cooldown_sec, end, macro_cfg)[0]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.run",
        description="Backtest the core sleeve and write results.md, results.json and a chart.",
    )
    p.add_argument("--refresh", action="store_true", help="re-download all data")
    p.add_argument("--end", metavar="YYYY-MM-DD", help="last day to include")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="data cache folder")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where to write the results")
    return p


def main(
    argv: list[str] | None = None,
    loader: Callable[..., MarketData] = load_market_data,
    core_cfg: CoreSleeveConfig | None = None,
) -> int:
    args = _parser().parse_args(argv)
    core_cfg = core_cfg or load_phase8_sleeves()[0]
    macro_cfg = load_macro_config()
    cooldown = load_portfolio().reentry_cooldown_sec
    fred_ids = list(replay_macro_config(macro_cfg).fred_series.values())
    tickers = required_tickers(core_cfg, macro_cfg)

    try:
        data = loader(tickers, fred_ids, args.cache_dir, refresh=args.refresh)
        results, curves = run_pipeline(data, core_cfg, cooldown, args.end, macro_cfg)
    except (DataError, ValueError) as exc:
        print(f"error: {exc}")
        print("Nothing was written.")
        return 1

    # Render everything in memory first, so a rendering error writes no file at all.
    png: bytes | None = None
    try:
        drawdowns = {k: v / v.cummax() - 1.0 for k, v in curves.items()}
        labels = {**report.BENCHMARK_LABELS, "Strategy": "Strategy (core sleeve)"}
        buf = io.BytesIO()
        report.plot_equity(
            {labels.get(k, k): v for k, v in curves.items()},
            {labels.get(k, k): v for k, v in drawdowns.items()},
            buf,
        )
        png = buf.getvalue()
    except ImportError:
        print(
            "matplotlib is not installed: skipping equity_curve.png "
            "(uv pip install -r requirements-backtest.txt)"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    json_out = report.json_text(results)
    md_out = report.markdown_text(results, stamp, chart=png is not None)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if png is not None:
        (out / "equity_curve.png").write_bytes(png)
    (out / "results.json").write_text(json_out)
    (out / "results.md").write_text(md_out)
    print(f"Wrote results to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
