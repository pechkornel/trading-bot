from datetime import datetime, timedelta, timezone

import pytest

from core.config import SentimentSatelliteConfig
from core.sleeves.sentiment_satellite import (
    Position,
    SentimentInput,
    SentimentSatelliteSleeve,
)

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
# Entry times that are exactly 10 and 9 NYSE sessions before NOW.
TEN_SESSIONS = datetime(2026, 10, 30, 14, tzinfo=timezone.utc)
NINE_SESSIONS = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)


def make_sleeve(reentry_cooldown_sec=0, **overrides) -> SentimentSatelliteSleeve:
    return SentimentSatelliteSleeve(
        SentimentSatelliteConfig(enabled=True, **overrides),
        reentry_cooldown_sec=reentry_cooldown_sec,
    )


def inp(symbol="AAPL", score=0.6, count=6, price=100.0) -> SentimentInput:
    return SentimentInput(
        symbol=symbol,
        price=price,
        sentiment_score=score,
        article_count=count,
        sentiment_ts=NOW,
    )


def pos(symbol="AAPL", shares=10, entry=100.0, opened_at=NOW - timedelta(days=1)) -> Position:
    # The default is 1 session before NOW.
    return Position(symbol=symbol, shares=shares, entry_price=entry, opened_at=opened_at)


def run(inputs, positions=None, equity=20_000.0, sleeve=None, now=NOW, **kwargs):
    return (sleeve or make_sleeve()).evaluate(
        inputs={i.symbol: i for i in inputs},
        positions=positions or {},
        sleeve_equity=equity,
        now=now,
        **kwargs,
    )


# ---- entries ----


def test_entry_sizing_and_reason():
    result = run([inp()])
    assert len(result.entries) == 1
    entry = result.entries[0]
    # 5% of 20,000 is 1,000, so 10 shares at 100.
    assert entry.shares == 10
    assert entry.reason.startswith("enter:")


def test_threshold_boundary_enters_at_exactly_point_four():
    assert len(run([inp(score=0.4)]).entries) == 1


def test_below_threshold_is_skipped():
    result = run([inp(score=0.39)])
    assert result.entries == []
    assert result.skipped[0][0] == "AAPL"


def test_too_few_articles_is_skipped():
    result = run([inp(count=4)])
    assert result.entries == []
    assert "articles=4" in result.skipped[0][1]


def test_article_minimum_boundary_enters_at_exactly_five():
    assert len(run([inp(count=5)]).entries) == 1


def test_bearish_is_skipped_with_longs_only():
    result = run([inp(score=-0.8)])
    assert result.entries == []
    assert "bearish" in result.skipped[0][1]


def test_missing_sentiment_is_skipped():
    result = run([inp(score=None)])
    assert result.entries == []
    assert result.skipped == [("AAPL", "no fresh sentiment")]


@pytest.mark.parametrize(
    "candidate",
    [inp(score=None), inp(score=0.39), inp(count=4), inp(score=-0.8)],
)
def test_skip_reasons_have_no_surrounding_whitespace(candidate):
    result = run([candidate])
    assert result.skipped
    for _, why in result.skipped:
        assert why == why.strip()


def test_symbol_outside_universe_is_ignored():
    result = run([inp(symbol="ZZZZ")])
    assert result.entries == []
    assert result.skipped == []


def test_ranking_and_slot_cap():
    # Two of four slots are taken, so only two of the six candidates enter.
    held = {"SPY": pos("SPY"), "QQQ": pos("QQQ")}
    candidates = [
        inp("AAPL", score=0.50),
        inp("MSFT", score=0.90),
        inp("NVDA", score=0.60),
        inp("TSLA", score=0.80),
        inp("AMZN", score=0.45),
        inp("META", score=0.70),
    ]
    inputs = candidates + [inp("SPY", score=0.1), inp("QQQ", score=0.1)]
    result = run(inputs, positions=held)
    assert [e.symbol for e in result.entries] == ["MSFT", "TSLA"]


def test_already_holding_is_skipped():
    result = run([inp("AAPL")], positions={"AAPL": pos("AAPL")})
    assert result.entries == []
    assert ("AAPL", "already holding") in result.skipped


def test_rounds_to_zero_shares():
    # Target is 1,000 but one share costs 2,000.
    result = run([inp(price=2_000.0)])
    assert result.entries == []
    assert result.skipped == [("AAPL", "rounds to 0 shares")]


def test_allow_entries_false_blocks_entries():
    assert run([inp()], allow_entries=False).entries == []


# ---- exits ----


def test_exit_by_timer_at_ten_sessions():
    result = run([inp(score=0.0)], positions={"AAPL": pos(opened_at=TEN_SESSIONS)})
    assert [e.symbol for e in result.exits] == ["AAPL"]
    assert result.exits[0].reason == "hold timer: 10 trading days >= 10 max"


def test_nine_session_old_position_stays():
    result = run([inp(score=0.0)], positions={"AAPL": pos(opened_at=NINE_SESSIONS)})
    assert result.exits == []


def test_naive_opened_at_is_treated_as_utc():
    naive = Position("AAPL", 10, 100.0, TEN_SESSIONS.replace(tzinfo=None))
    result = run([inp(score=0.0)], positions={"AAPL": naive})
    assert len(result.exits) == 1


def test_friday_entry_does_not_exit_after_ten_calendar_days():
    friday = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
    held = {"AAPL": pos(opened_at=friday)}
    flat = [inp(score=0.0)]
    # 10 calendar days later, but only 5 sessions (Thanksgiving week).
    assert (
        run(flat, positions=held, now=datetime(2026, 11, 30, 15, tzinfo=timezone.utc)).exits == []
    )
    # 9 sessions, one minute before the close.
    assert (
        run(flat, positions=held, now=datetime(2026, 12, 4, 20, 59, tzinfo=timezone.utc)).exits
        == []
    )
    # 10 sessions at the close.
    result = run(flat, positions=held, now=datetime(2026, 12, 4, 21, tzinfo=timezone.utc))
    assert [e.symbol for e in result.exits] == ["AAPL"]
    assert result.exits[0].reason == "hold timer: 10 trading days >= 10 max"


def test_exit_by_stop_at_five_percent_loss():
    result = run([inp(score=0.0, price=95.0)], positions={"AAPL": pos()})
    assert len(result.exits) == 1
    assert result.exits[0].reason.startswith("stop")
    assert result.exits[0].price == 95.0


def test_small_loss_does_not_stop_out():
    result = run([inp(score=0.0, price=95.01)], positions={"AAPL": pos()})
    assert result.exits == []


def test_exit_without_input_uses_entry_price():
    result = run([], positions={"AAPL": pos(entry=123.0, opened_at=TEN_SESSIONS)})
    assert result.exits[0].price == 123.0


def test_exit_frees_a_slot_for_one_new_entry():
    held = {s: pos(s) for s in ("SPY", "QQQ", "AAPL", "MSFT")}
    held["SPY"] = pos("SPY", opened_at=TEN_SESSIONS)
    inputs = [inp("NVDA", score=0.9), inp("TSLA", score=0.8)]
    result = run(inputs, positions=held)
    assert [e.symbol for e in result.exits] == ["SPY"]
    assert [e.symbol for e in result.entries] == ["NVDA"]


def test_allow_exits_false_blocks_exits():
    result = run(
        [inp(score=0.0)], positions={"AAPL": pos(opened_at=TEN_SESSIONS)}, allow_exits=False
    )
    assert result.exits == []


# ---- no same-cycle re-entry ----


def test_timer_exit_is_not_reentered_in_the_same_cycle():
    result = run([inp("AAPL", score=0.9)], positions={"AAPL": pos(opened_at=TEN_SESSIONS)})
    assert [e.symbol for e in result.exits] == ["AAPL"]
    assert result.entries == []
    assert ("AAPL", "exited this cycle") in result.skipped


def test_stop_exit_is_not_reentered_in_the_same_cycle():
    result = run(
        [inp("AAPL", score=0.9, price=95.0)],
        positions={"AAPL": pos(entry=100.0)},
    )
    assert [e.symbol for e in result.exits] == ["AAPL"]
    assert result.entries == []
    assert ("AAPL", "exited this cycle") in result.skipped


def test_freed_slot_goes_to_another_candidate():
    held = {s: pos(s) for s in ("SPY", "QQQ", "AAPL", "MSFT")}
    held["SPY"] = pos("SPY", opened_at=TEN_SESSIONS)
    result = run([inp("SPY", score=0.95), inp("NVDA", score=0.9)], positions=held)
    assert [e.symbol for e in result.exits] == ["SPY"]
    assert [e.symbol for e in result.entries] == ["NVDA"]


# ---- re-entry cooldown ----

DAY = 86400


def test_recent_exit_blocks_reentry():
    result = run(
        [inp("AAPL", score=0.9)],
        sleeve=make_sleeve(reentry_cooldown_sec=DAY),
        last_exit_ts={"AAPL": NOW - timedelta(hours=1)},
    )
    assert result.entries == []
    assert ("AAPL", "reentry cooldown: 23.0h left") in result.skipped


def test_reentry_allowed_at_exactly_the_cooldown():
    result = run(
        [inp("AAPL", score=0.9)],
        sleeve=make_sleeve(reentry_cooldown_sec=DAY),
        last_exit_ts={"AAPL": NOW - timedelta(seconds=DAY)},
    )
    assert [e.symbol for e in result.entries] == ["AAPL"]


def test_zero_cooldown_allows_reentry_after_an_earlier_exit():
    result = run(
        [inp("AAPL", score=0.9)],
        sleeve=make_sleeve(reentry_cooldown_sec=0),
        last_exit_ts={"AAPL": NOW - timedelta(hours=1)},
    )
    assert [e.symbol for e in result.entries] == ["AAPL"]


def test_naive_exit_time_is_treated_as_utc():
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    result = run(
        [inp("AAPL", score=0.9)],
        sleeve=make_sleeve(reentry_cooldown_sec=DAY),
        last_exit_ts={"AAPL": naive},
    )
    assert result.entries == []
    assert ("AAPL", "reentry cooldown: 23.0h left") in result.skipped


def test_cooldown_block_does_not_use_a_slot():
    result = run(
        [inp("AAPL", score=0.9), inp("MSFT", score=0.8)],
        sleeve=make_sleeve(reentry_cooldown_sec=DAY, max_concurrent_positions=1),
        last_exit_ts={"AAPL": NOW - timedelta(hours=1)},
    )
    assert [e.symbol for e in result.entries] == ["MSFT"]
