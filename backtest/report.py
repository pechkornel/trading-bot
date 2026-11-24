"""Results dict helpers, JSON, Markdown and PNG output for the backtest.

JSON and Markdown are plain text and need no extra packages. `plot_equity` imports
matplotlib inside the function (Agg backend), so importing this module needs neither
matplotlib nor yfinance.

The JSON holds no wall clock time, so two runs on the same input are byte-identical.
The Markdown gets the generation time as an argument.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

BENCHMARK_LABELS = {
    "spy_buy_hold": "SPY buy and hold",
    "sixty_forty": "60/40 (SPY/AGG)",
    "equal_weight": "Equal weight (core ETFs)",
}

ASSUMPTIONS = [
    "Signals use Yahoo split-adjusted `Close`. P&L uses `Adj Close` (dividends reinvested). "
    "Adjusted closes are never passed to the sleeve, the scoring code or the macro fetcher.",
    "Timing: each trading day is evaluated once at 21:00 UTC (the 16:00 New York close). A "
    "decision made at the close of day t is filled at the close of the next trading day. "
    "The strategy starts in cash and waits for the first decision on the rebalance weekday "
    "(Friday in the live config). A Friday decision is therefore filled at the next "
    "trading day's close, usually Monday. See the weekday sensitivity table.",
    "Costs: `cost_bps / 10000 * turnover` of equity on the fill day, where turnover is the "
    "sum of absolute weight changes over assets (cash excluded), so it is two-way (buys plus "
    "sells). A first buy from cash is 1.0, a full switch is 2.0. The first "
    "entry is charged for every portfolio.",
    "Cash earns 0. Weight not invested earns nothing.",
    "Momentum, trend and the HYG/LQD credit ratio use raw `Close` (price return, no "
    "dividends), exactly as the live bot does. Bond ETFs and BIL are understated by their "
    "yield, and distribution days (for example DBC each December) look like price drops for "
    "12 months. This is a property of the live bot's signals, not of the backtest.",
    "Holdings given to the sleeve are synthetic fractional shares on a constant notional, so "
    "the drift rule sees true current weights. Live uses integer shares and DAY limit orders "
    "at plus or minus 5 bps; the backtest fills fractional weights at the next close.",
    "Sleeve equity is marked to market. Live measures drift against a fixed starting-equity "
    "budget (the known gap 'Static starting equity' in `docs/TRADING_LOGIC.md` section 8).",
    "The re-entry cooldown is called with live semantics, but with one decision per day at a "
    "fixed clock the next decision is exactly 86400 s later, which counts as free. It never "
    "binds in the replay.",
    "Macro: the bot's own `MacroFetcher.snapshot()` runs on cached series sliced as of the "
    "decision date. FRED T10Y2Y uses observations dated strictly before the decision day (a "
    "one day lag). VIX, DXY, HYG and LQD use raw closes up to and including the decision day.",
    "VIX settles at 16:15 ET and DXY at 17:00 ET, slightly after the 16:00 equity close. The "
    "effect on a 60-day trend and a level test against 25 is tiny.",
    "Fed funds (DFF) is not downloaded, so the macro snapshot is never `complete`. This has "
    "no effect on scores because the neutral guard also needs VIX and the curve to be "
    "missing.",
    "HYG and LQD are downloaded because the live macro score uses the credit ratio trend for "
    "equity and bond buckets.",
    "Benchmarks: SPY buy and hold; 60% SPY and 40% AGG; equal weight over the core universe "
    "(BIL included). The two rebalanced benchmarks decide on the last trading day of each "
    "month and fill on the first trading day of the next month, at the same cost rate as the "
    "strategy run they are compared with.",
    "The risk-free rate for Sharpe and Sortino is the BIL daily adjusted close return. It "
    "includes BIL's expense ratio (about 0.135%), which slightly flatters every Sharpe.",
    "CAGR uses 252 trading days per year: `(end / start) ** (252 / n_returns) - 1`.",
    "Risk-off share: share of trading days positioned on a risk-off decision, from the fill "
    "day of that decision to the next fill.",
    "Start date: first trading day on which every required ticker has enough raw closes for "
    "the longest signal window, and T10Y2Y has an observation before it.",
    "Trading days are dates on which all core ETFs have a raw and an adjusted close. Macro "
    "tickers are joined as of the last value on or before the decision day.",
]

LIVE_GAPS = [
    "Live evaluates on the first open cycle on Friday using intraday data and fills the same "
    "day with DAY limit orders. The backtest decides at Friday's close and fills at the next "
    "trading day's close (usually Monday). The weekday sensitivity table shows how much the "
    "choice of weekday matters.",
    "Live fills are DAY limit orders at plus or minus 5 bps on integer shares and may be "
    "partial or missed. The backtest fills fractional weights at the next close.",
    "Live decides when the runner wakes up on its schedule, not at a fixed close.",
    "Live uses a fixed sleeve budget for drift; the backtest marks it to market.",
    "Live cash may earn a money market rate; backtest cash earns 0.",
    "Live Yahoo and CPGW data can be revised or delayed; the backtest uses one final "
    "download. Survivorship is not modelled: the universe is today's ETF list.",
    "Taxes, fees other than the flat cost rate, and market impact are not modelled.",
]


def _clean(value, places: int = 6):
    """Round floats, turn non-finite values into None, recurse into containers."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, places) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v, places) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, places) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item(), places)
    return value


def json_text(results: dict) -> str:
    return json.dumps(_clean(results), indent=2, sort_keys=True, allow_nan=False) + "\n"


def write_json(results: dict, path: str | Path) -> None:
    Path(path).write_text(json_text(results))


def _pct(x, places: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{places}f}%"


def _num(x, places: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{places}f}"


def _date(x) -> str:
    return "n/a" if x is None else str(x)


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return [*lines, ""]


_METRIC_HEADER = [
    "Portfolio",
    "CAGR",
    "Volatility",
    "Sharpe",
    "Sortino",
    "Max drawdown",
    "Peak",
    "Trough",
    "Recovery",
    "Calmar",
    "Turnover per year (buys plus sells)",
    "Risk-off share",
]


def _metric_row(label: str, m: dict) -> list[str]:
    return [
        label,
        _pct(m.get("cagr")),
        _pct(m.get("volatility")),
        _num(m.get("sharpe")),
        _num(m.get("sortino")),
        _pct(m.get("max_drawdown")),
        _date(m.get("drawdown_peak")),
        _date(m.get("drawdown_trough")),
        _date(m.get("drawdown_recovery")),
        _num(m.get("calmar")),
        _pct(m.get("annual_turnover"), 0),
        _pct(m.get("risk_off_share"), 1),
    ]


def _weekday_section(results: dict) -> list[str]:
    out = ["## Rebalance weekday sensitivity", ""]
    out += [
        f"Base config at {results['base_cost_bps']:g} bps, changing only the weekday of the "
        "weekly rebalance (0 = Monday to 4 = Friday). The weekday set in the live config is "
        "marked. The strategy decides at that day's close and fills at the next trading day's "
        "close.",
        "",
    ]
    rows = []
    for key, w in results["weekday_sensitivity"].items():
        label = f"{w['name']} (live)" if w["live"] else w["name"]
        rows.append(
            [
                f"{key} {label}",
                _pct(w["cagr"]),
                _pct(w["volatility"]),
                _num(w["sharpe"]),
                _pct(w["max_drawdown"]),
            ]
        )
    return [*out, *_table(["Weekday", "CAGR", "Volatility", "Sharpe", "Max drawdown"], rows)]


def _subperiod_section(results: dict) -> list[str]:
    out = ["## Subperiods", ""]
    out += [
        "CAGR and Sharpe over calendar-year ranges, base cost. The first and last ranges can "
        "be partial. CAGR uses 252 trading days per year.",
        "",
    ]
    sp = results["subperiods"]
    labels = list(sp["Strategy"])
    rows = []
    for name, per in sp.items():
        cells = [
            BENCHMARK_LABELS.get(name, "Strategy (core sleeve)" if name == "Strategy" else name)
        ]
        for label in labels:
            cells += [_pct(per[label]["cagr"]), _num(per[label]["sharpe"])]
        rows.append(cells)
    header = ["Portfolio"]
    for label in labels:
        header += [f"{label} CAGR", f"{label} Sharpe"]
    return [*out, *_table(header, rows)]


def _bootstrap_section(results: dict) -> list[str]:
    out = ["## Uncertainty", ""]
    b = results.get("bootstrap")
    if not b:
        return [*out, "Not computed: the sample is shorter than two bootstrap blocks.", ""]
    out += [
        f"Circular block bootstrap of daily excess returns: {b['block_days']} day blocks, "
        f"{b['draws']} draws, seed {b['seed']}, {b['days']} days. Each draw uses the same "
        "blocks for the strategy and every benchmark, so differences are paired. Intervals "
        "are 95% percentile intervals. Share better is the share of draws in which the "
        "strategy Sharpe is higher than the benchmark's. An interval that contains 0 means "
        "the difference is not statistically distinguishable from noise.",
        "",
    ]
    s = b["strategy_sharpe"]
    out += [
        f"Strategy Sharpe: {_num(s['point'])}, 95% interval {_num(s['lo'])} to {_num(s['hi'])}.",
        "",
    ]
    rows = []
    for k, v in b["vs"].items():
        rows.append(
            [
                BENCHMARK_LABELS.get(k, k),
                _num(v["diff_point"]),
                f"{_num(v['lo'])} to {_num(v['hi'])}",
                _pct(v["share_better"], 1),
            ]
        )
    out += _table(
        ["Strategy minus", "Sharpe difference", "95% interval", "Share of draws better"], rows
    )
    return out


def render_markdown(results: dict, generated_at: str, chart: bool = False) -> str:
    base = results["strategy"]
    bench = results["benchmarks"]
    data = results["data"]
    cost = results["base_cost_bps"]
    out: list[str] = ["# Core sleeve backtest results", "", "## Summary", ""]
    out += [
        f"- Generated: {generated_at}",
        f"- Data downloaded: {data.get('downloaded_at') or 'unknown'}",
        f"- yfinance version: {data.get('yfinance_version') or 'unknown'}",
        f"- Start date: {results['start_date']}",
        f"- End date: {results['end_date']}",
        f"- Trading days: {results['trading_days']}",
        f"- Days dropped from the calendar (a core ETF had no price): {results['dropped_days']}",
        f"- Bars required before the start: {results['min_bars']}",
        f"- Base cost: {cost:g} bps per unit of turnover",
        "",
    ]
    if chart:
        out += ["![Equity and drawdown](equity_curve.png)", ""]

    out += ["## Data sources", ""]
    out += [
        "Daily bars from Yahoo Finance through yfinance (`Close` and `Adj Close`, "
        "`auto_adjust=False`). T10Y2Y from FRED through the bot's own parser.",
        "",
    ]
    rows = [
        [t, str(v.get("first", "")), str(v.get("last", "")), str(v.get("rows", ""))]
        for t, v in sorted(data["tickers"].items())
    ]
    rows += [
        [f"FRED {s}", str(v.get("first", "")), str(v.get("last", "")), str(v.get("rows", ""))]
        for s, v in sorted(data["fred"].items())
    ]
    out += _table(["Series", "First date", "Last date", "Rows"], rows)

    out += ["## Assumptions and deviations from live", ""]
    out += [f"- {a}" for a in ASSUMPTIONS]
    out += ["", "Known gaps between this backtest and live trading:", ""]
    out += [f"- {g}" for g in LIVE_GAPS]
    out += [""]

    out += [
        "## Headline results",
        "",
        f"Strategy and benchmarks, base cost {cost:g} bps. Turnover is two-way (buys plus "
        "sells): a full switch counts as 200%.",
        "",
    ]
    rows = [_metric_row("Strategy (core sleeve)", base)]
    rows += [_metric_row(BENCHMARK_LABELS.get(k, k), v) for k, v in bench.items()]
    out += _table(_METRIC_HEADER, rows)

    out += _weekday_section(results)
    out += _subperiod_section(results)
    out += _bootstrap_section(results)

    out += ["## Rebalance triggers", ""]
    out += ["Evaluations by trigger and fills actually traded, base config.", ""]
    trig = base["triggers"]
    out += _table(
        ["Calendar", "Drift", "Evaluations", "Fills"],
        [[str(trig["calendar"]), str(trig["drift"]), str(base["evaluations"]), str(base["fills"])]],
    )

    out += ["## Calendar-year returns", ""]
    names = ["Strategy"] + [BENCHMARK_LABELS.get(k, k) for k in bench]
    series = [base["calendar_year_returns"]] + [v["calendar_year_returns"] for v in bench.values()]
    years = sorted({int(y) for s in series for y in s})
    rows = []
    for i, y in enumerate(years):
        label = f"{y}*" if i in (0, len(years) - 1) else str(y)
        rows.append([label] + [_pct(s.get(str(y))) for s in series])
    out += _table(["Year", *names], rows)
    out += ["* The first and last years are partial.", ""]

    out += ["## Ablations", "", "Each variant changes only config fields and the cost rate.", ""]
    rows = []
    for name, v in results["variants"].items():
        r = _metric_row(name, v)
        rows.append([r[0], v["description"], *r[1:6], *r[9:12], f"{v['cost_bps']:g}"])
    out += _table(
        [
            "Variant",
            "Description",
            "CAGR",
            "Volatility",
            "Sharpe",
            "Sortino",
            "Max drawdown",
            "Calmar",
            "Turnover per year (buys plus sells)",
            "Risk-off share",
            "Cost (bps)",
        ],
        rows,
    )

    out += ["## Costs", "", "Strategy and benchmarks at the same cost rate.", ""]
    rows = []
    for c, group in results["benchmarks_by_cost"].items():
        strat = results["strategy_by_cost"][c]
        rows.append([f"{float(c):g}", "Strategy", _pct(strat["cagr"]), _num(strat["sharpe"])])
        for k, v in group.items():
            rows.append(["", BENCHMARK_LABELS.get(k, k), _pct(v["cagr"]), _num(v["sharpe"])])
    out += _table(["Cost (bps)", "Portfolio", "CAGR", "Sharpe"], rows)
    return "\n".join(out)


def markdown_text(results: dict, generated_at: str, chart: bool = False) -> str:
    return render_markdown(_clean(results), generated_at, chart) + "\n"


def write_markdown(results: dict, path: str | Path, generated_at: str, chart: bool = False) -> None:
    Path(path).write_text(markdown_text(results, generated_at, chart))


def plot_equity(curves: dict, drawdowns: dict, path) -> None:
    """Top panel: log equity. Bottom panel: drawdowns. Needs matplotlib (lazy import).

    `path` is a file path or a binary file-like object (anything `savefig` accepts).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    for name, series in curves.items():
        ax1.plot(series.index, series.values, label=name, linewidth=1.2)
    ax1.set_yscale("log")
    ax1.set_ylabel("Equity (log, start = 1)")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)
    for name, series in drawdowns.items():
        ax2.plot(series.index, series.values * 100, label=name, linewidth=1.0)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
