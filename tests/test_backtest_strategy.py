from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backtest.ablations import VARIANTS, build_cfg
from backtest.engine import simulate
from backtest.macro_replay import HistoricalMacroFetcher, replay_macro_config
from backtest.strategy import CoreReplay, decision_time, risk_off_days, trigger_counts
from core.config import MacroConfig
from core.market_hours import to_eastern
from core.scoring import score_universe, select_holdings
from tests.backtest_synth import make_frames, small_cfg

N = 40
START = 12  # calendar starts at index 12 (Thursday 2024-01-18) so the windows are full
SYMS = ["SPY", "TLT", "GLD", "IEF"]
INDEX = pd.bdate_range("2024-01-02", periods=N)


def base_returns(**overrides):
    rets = {
        "SPY": [0.0] + [0.010] * (N - 1),
        "TLT": [0.0] + [0.005] * (N - 1),
        "GLD": [0.0] + [-0.005] * (N - 1),
        "IEF": [0.0] + [0.001] * (N - 1),
        "^VIX": [0.0] * N,
    }
    rets.update(overrides)
    return rets


def build(returns=None, vix=15.0, calendar_drop=()):
    raw, adj = make_frames(returns or base_returns())
    raw["^VIX"] = vix
    cal = raw.index[START:].drop([pd.Timestamp(d) for d in calendar_drop])
    return raw, adj[SYMS], cal


def make_replay(raw, cfg, fred):
    fetcher = HistoricalMacroFetcher(raw, fred, replay_macro_config(MacroConfig()))
    return CoreReplay(cfg, raw, fetcher, reentry_cooldown_sec=0)


def run(raw, adj, cal, cfg, fred=None, cost_bps=5.0):
    replay = make_replay(raw, cfg, fred or {})
    sim = simulate(adj, cal, replay.decide, cost_bps)
    return replay, sim


def dates(records):
    return [r.date.date().isoformat() for r in records]


# ---- timing ----


def test_first_decision_is_first_friday_and_fills_next_trading_day():
    raw, adj, cal = build()
    replay, sim = run(raw, adj, cal, small_cfg())
    assert dates(replay.decisions)[0] == "2024-01-19"
    assert replay.decisions[0].reason.startswith("calendar")
    first_fill, targets = sim.fills[0]
    assert first_fill == pd.Timestamp("2024-01-22")
    assert set(targets) == {"SPY", "TLT"}
    assert sim.weights.loc["2024-01-19"].sum() == 0.0  # still cash on the decision day
    assert sim.weights.loc["2024-01-22", "SPY"] == pytest.approx(0.5)


def test_missing_friday_gives_no_decision_that_week():
    raw, adj, cal = build(calendar_drop=["2024-01-19"])
    replay, _ = run(raw, adj, cal, small_cfg())
    assert dates(replay.decisions)[0] == "2024-01-26"


def test_drift_triggers_on_a_non_friday_once_per_day():
    rets = base_returns()
    jump = list(rets["SPY"])
    jump[INDEX.get_loc(pd.Timestamp("2024-01-24"))] = 0.30
    rets["SPY"] = jump
    raw, adj, cal = build(rets)
    replay, _ = run(raw, adj, cal, small_cfg(drift_threshold=0.05))
    drift = [r for r in replay.decisions if r.reason.startswith("drift")]
    assert dates(drift) == ["2024-01-24"]
    assert all(r.date.weekday() == 4 for r in replay.decisions if r not in drift)
    assert trigger_counts(replay.decisions) == {
        "calendar": len(replay.decisions) - 1,
        "drift": 1,
    }
    # a drift evaluation is also once per day
    fresh = make_replay(raw, small_cfg(drift_threshold=0.05), {})
    day = pd.Timestamp("2024-01-24")
    assert fresh.decide(day, {"SPY": 0.9, "TLT": 0.1}) is not None
    assert fresh.decide(day, {"SPY": 0.9, "TLT": 0.1}) is None


def test_once_per_day_rule_holds_for_the_calendar_trigger():
    raw, adj, cal = build()
    replay = make_replay(raw, small_cfg(), {})
    friday = pd.Timestamp("2024-01-19")
    assert replay.decide(friday, {}) is not None
    assert replay.decide(friday, {}) is None


@pytest.mark.parametrize("day", ["2024-01-15", "2024-07-15"])
def test_decision_time_maps_to_the_same_eastern_date(day):
    t = pd.Timestamp(day)
    now = decision_time(t)
    assert now == datetime(t.year, t.month, t.day, 21, tzinfo=timezone.utc)
    assert to_eastern(now).date() == t.date()


# ---- the bot decides ----


def test_targets_equal_the_bots_own_scoring_on_the_same_sliced_inputs():
    raw, adj, cal = build()
    cfg = small_cfg()
    replay, _ = run(raw, adj, cal, cfg)
    rec = replay.decisions[0]
    closes = {s: raw[s].loc[: rec.date].dropna().iloc[-504:] for s in SYMS}
    fetcher = HistoricalMacroFetcher(raw, {}, replay_macro_config(MacroConfig()))
    fetcher.set_asof(rec.date)
    result = score_universe(closes, fetcher.snapshot(), cfg)
    assert rec.targets == select_holdings(result, cfg)
    assert rec.risk_off is result.risk_off
    assert rec.top_scores[0][0] == result.scores[0].symbol


# ---- risk-off ----


def falling_spy():
    return base_returns(SPY=[0.0] * 9 + [-0.02] * (N - 9))


def test_risk_off_switches_to_the_risk_off_universe_from_the_fill_day():
    raw, adj, cal = build(falling_spy(), vix=40.0)
    replay, sim = run(raw, adj, cal, small_cfg())
    rec = replay.decisions[0]
    assert rec.risk_off is True
    assert set(rec.targets) == {"TLT", "IEF"}
    flags = risk_off_days(cal, sim.fills, replay.decisions)
    assert flags.loc[:"2024-01-19"].sum() == 0
    assert flags.loc["2024-01-22":].all()


def test_no_risk_off_ablation_stays_in_the_scored_universe():
    v = next(v for v in VARIANTS if v.name == "no_risk_off")
    raw, adj, cal = build(falling_spy(), vix=40.0)
    replay, sim = run(raw, adj, cal, build_cfg(small_cfg(), v))
    assert replay.decisions[0].risk_off is False
    assert "IEF" not in replay.decisions[0].targets
    assert not risk_off_days(cal, sim.fills, replay.decisions).any()


def test_low_vix_keeps_the_base_config_out_of_risk_off():
    raw, adj, cal = build(falling_spy(), vix=15.0)
    replay, _ = run(raw, adj, cal, small_cfg())
    assert replay.decisions[0].risk_off is False


# ---- look-ahead ----


def perturb(obj, k, rng):
    out = obj.copy()
    tail = out.iloc[k + 1 :]
    out.iloc[k + 1 :] = tail * rng.uniform(0.5, 1.5, tail.shape)
    return out


def test_perturbing_data_after_day_k_does_not_change_earlier_decisions():
    rng = np.random.default_rng(1)
    raw, adj, cal = build(vix=40.0)
    cfg = small_cfg()
    fred = {"T10Y2Y": pd.Series(np.linspace(-0.5, 0.5, N), index=INDEX)}
    k = INDEX.get_loc(pd.Timestamp("2024-02-02"))
    replay1, sim1 = run(raw, adj, cal, cfg, fred)

    raw2 = perturb(raw, k, rng)
    adj2 = perturb(adj, k, rng)
    fred2 = {"T10Y2Y": perturb(fred["T10Y2Y"], k, rng)}
    replay2, sim2 = run(raw2, adj2, cal, cfg, fred2)

    cutoff = INDEX[k]
    d1 = [r for r in replay1.decisions if r.date <= cutoff]
    d2 = [r for r in replay2.decisions if r.date <= cutoff]
    assert len(d1) >= 2
    assert d1 == d2
    assert [f for f in sim1.fills if f[0] <= cutoff] == [f for f in sim2.fills if f[0] <= cutoff]
    assert sim1.equity.iloc[-1] != sim2.equity.iloc[-1]  # the perturbation did bite later


def test_adjusted_closes_feed_only_pnl_not_decisions():
    raw, adj, cal = build()
    cfg = small_cfg()
    replay1, sim1 = run(raw, adj, cal, cfg)
    rng = np.random.default_rng(7)
    noise = pd.DataFrame(
        100 * np.exp(rng.normal(0, 0.05, adj.shape)), index=adj.index, columns=adj.columns
    )
    replay2, sim2 = run(raw, noise, cal, cfg)
    assert replay1.decisions == replay2.decisions
    assert [f[1] for f in sim1.fills] == [f[1] for f in sim2.fills]
    assert sim1.equity.iloc[-1] != sim2.equity.iloc[-1]
