"""Replay the core sleeve with the bot's own decision code.

`CoreReplay.decide` is a `DecideFn` for `backtest.engine.simulate`. It calls
`CoreSleeve.should_evaluate` and `CoreSleeve.evaluate` (which call `score_universe`
and `select_holdings`) and reads target weights from the returned `CoreDecision`
objects. Nothing is re-implemented here.

Look-ahead rules: signals see raw closes sliced to `<= t`; macro comes from a
`HistoricalMacroFetcher` set to `t`; adjusted closes are never seen by this module
(they feed only P&L in the simulator). Holdings are synthetic fractional shares so the
sleeve's drift rule sees the true current weights (decision D6).

Offline: no yfinance, no matplotlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from backtest.macro_replay import HistoricalMacroFetcher
from core.config import CoreSleeveConfig
from core.sleeves.core import CoreSleeve

NOTIONAL = 1e9
HISTORY_BARS = 504
SPY = "SPY"


@dataclass(frozen=True)
class DecisionRecord:
    date: pd.Timestamp
    reason: str
    risk_off: bool
    targets: dict[str, float]
    top_scores: tuple[tuple[str, float], ...]


def decision_time(t: pd.Timestamp) -> datetime:
    """21:00 UTC on day t: 16:00 EST or 17:00 EDT, so the Eastern date is t (D5)."""
    return datetime(t.year, t.month, t.day, 21, 0, tzinfo=timezone.utc)


class CoreReplay:
    def __init__(
        self,
        cfg: CoreSleeveConfig,
        raw_close: pd.DataFrame,
        macro_fetcher: HistoricalMacroFetcher,
        reentry_cooldown_sec: int,
        history_bars: int = HISTORY_BARS,
        notional: float = NOTIONAL,
    ) -> None:
        self.cfg = cfg
        self.raw_close = raw_close
        self.macro_fetcher = macro_fetcher
        self.sleeve = CoreSleeve(cfg, reentry_cooldown_sec=reentry_cooldown_sec)
        self.history_bars = history_bars
        self.notional = notional
        self.last_ts: datetime | None = None
        self.exits: dict[str, datetime] = {}
        self.decisions: list[DecisionRecord] = []

    def _closes(self, t: pd.Timestamp) -> dict[str, pd.Series]:
        symbols = list(dict.fromkeys([*self.cfg.universe, *self.cfg.risk_off_universe, SPY]))
        out: dict[str, pd.Series] = {}
        for s in symbols:
            if s in self.raw_close.columns:
                out[s] = self.raw_close[s].loc[:t].dropna().iloc[-self.history_bars :]
        return out

    def decide(self, t: pd.Timestamp, weights: dict[str, float]) -> dict[str, float] | None:
        now = decision_time(t)
        closes = self._closes(t)  # raw closes up to and including t only
        prices = {s: float(closes[s].iloc[-1]) for s in weights if s in closes}
        holdings = {s: w * self.notional / prices[s] for s, w in weights.items() if s in prices}

        go, reason = self.sleeve.should_evaluate(
            last_rebalance_ts=self.last_ts,
            current_holdings=holdings,
            prices=prices,
            sleeve_equity=self.notional,
            now=now,
        )
        if not go:
            return None

        self.macro_fetcher.set_asof(t)
        macro = self.macro_fetcher.snapshot()
        result = self.sleeve.evaluate(
            closes=closes,
            macro=macro,
            current_holdings=holdings,
            sleeve_equity=self.notional,
            reason=reason,
            last_exit_ts=self.exits,
            now=now,
        )
        targets = {d.symbol: d.target_weight for d in result.decisions if d.target_weight > 0}
        self.last_ts = now
        for d in result.decisions:
            if d.current_shares > 0 and d.target_shares == 0:
                self.exits[d.symbol] = now
        self.decisions.append(
            DecisionRecord(
                date=pd.Timestamp(t),
                reason=reason,
                risk_off=result.risk_off,
                targets=targets,
                top_scores=tuple((s.symbol, s.composite) for s in result.scores[:5]),
            )
        )
        return targets


def risk_off_days(
    calendar: pd.DatetimeIndex,
    fills: list[tuple[pd.Timestamp, dict[str, float]]],
    decisions: list[DecisionRecord],
) -> pd.Series:
    """True on days the portfolio is positioned on a risk-off decision (D18).

    From the fill day of a risk-off decision until the next fill. A fill on calendar
    day i belongs to the decision made on day i-1.
    """
    by_date = {d.date: d.risk_off for d in decisions}
    flags = pd.Series(False, index=calendar)
    pos = {t: i for i, t in enumerate(calendar)}
    state = False
    fill_state = {}
    for fill_day, _ in fills:
        i = pos[fill_day]
        fill_state[fill_day] = bool(by_date.get(calendar[i - 1], False)) if i > 0 else False
    for t in calendar:
        if t in fill_state:
            state = fill_state[t]
        flags[t] = state
    return flags


def trigger_counts(decisions: list[DecisionRecord]) -> dict[str, int]:
    """Number of evaluations by trigger, from the reason prefix."""
    counts = {"calendar": 0, "drift": 0}
    for d in decisions:
        for key in counts:
            if d.reason.startswith(key):
                counts[key] += 1
    return counts
