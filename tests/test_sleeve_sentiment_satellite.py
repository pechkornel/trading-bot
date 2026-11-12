from datetime import datetime, timedelta, timezone

from core.config import SentimentSatelliteConfig
from core.sleeves.sentiment_satellite import (
    Position,
    SentimentInput,
    SentimentSatelliteSleeve,
)

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def make_sleeve(**overrides) -> SentimentSatelliteSleeve:
    return SentimentSatelliteSleeve(SentimentSatelliteConfig(enabled=True, **overrides))


def inp(symbol="AAPL", score=0.6, count=6, price=100.0) -> SentimentInput:
    return SentimentInput(
        symbol=symbol,
        price=price,
        sentiment_score=score,
        article_count=count,
        sentiment_ts=NOW,
    )


def pos(symbol="AAPL", shares=10, entry=100.0, age_days=1) -> Position:
    return Position(
        symbol=symbol,
        shares=shares,
        entry_price=entry,
        opened_at=NOW - timedelta(days=age_days),
    )


def run(inputs, positions=None, equity=20_000.0, **kwargs):
    return make_sleeve().evaluate(
        inputs={i.symbol: i for i in inputs},
        positions=positions or {},
        sleeve_equity=equity,
        now=NOW,
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
    # Current behaviour: the "skip:" prefix is stripped with [5:], which leaves
    # a leading space in the reason.
    assert result.skipped == [("AAPL", " no fresh sentiment")]


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


def test_exit_by_timer_at_ten_days():
    result = run([inp(score=0.0)], positions={"AAPL": pos(age_days=10)})
    assert [e.symbol for e in result.exits] == ["AAPL"]
    assert result.exits[0].reason.startswith("hold timer")


def test_nine_day_old_position_stays():
    result = run([inp(score=0.0)], positions={"AAPL": pos(age_days=9)})
    assert result.exits == []


def test_naive_opened_at_is_treated_as_utc():
    naive = Position("AAPL", 10, 100.0, (NOW - timedelta(days=10)).replace(tzinfo=None))
    result = run([inp(score=0.0)], positions={"AAPL": naive})
    assert len(result.exits) == 1


def test_exit_by_stop_at_five_percent_loss():
    result = run([inp(score=0.0, price=95.0)], positions={"AAPL": pos()})
    assert len(result.exits) == 1
    assert result.exits[0].reason.startswith("stop")
    assert result.exits[0].price == 95.0


def test_small_loss_does_not_stop_out():
    result = run([inp(score=0.0, price=95.01)], positions={"AAPL": pos()})
    assert result.exits == []


def test_exit_without_input_uses_entry_price():
    result = run([], positions={"AAPL": pos(entry=123.0, age_days=10)})
    assert result.exits[0].price == 123.0


def test_exit_frees_a_slot_for_one_new_entry():
    held = {s: pos(s) for s in ("SPY", "QQQ", "AAPL", "MSFT")}
    held["SPY"] = pos("SPY", age_days=10)
    inputs = [inp("NVDA", score=0.9), inp("TSLA", score=0.8)]
    result = run(inputs, positions=held)
    assert [e.symbol for e in result.exits] == ["SPY"]
    assert [e.symbol for e in result.entries] == ["NVDA"]


def test_allow_exits_false_blocks_exits():
    result = run([inp(score=0.0)], positions={"AAPL": pos(age_days=10)}, allow_exits=False)
    assert result.exits == []
