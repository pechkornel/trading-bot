"""Offline tests for live.account_equity (fake client, no sockets)."""

import pytest

from core.config import PortfolioConfig
from core.cpgw_client import CPGWError, NotAuthenticatedError
from live.account_equity import (
    EquityReading,
    _mask_account,
    format_equity_event,
    parse_net_liquidation,
    resolve_total_equity,
)

ACCOUNT = "DU0000000"
RANGE_TEXT = "outside 25,000.00 to 400,000.00 (0.25x to 4x starting_equity)"


class FakeAccountClient:
    def __init__(self, summary=None, exc=None, account_id=ACCOUNT):
        self._summary = summary
        self._exc = exc
        self.account_id = account_id

    def account_summary(self):
        if self._exc is not None:
            raise self._exc
        return self._summary


class NoSummaryClient:
    account_id = ACCOUNT


def nl(amount=123456.78, currency="USD", **extra):
    entry = {"amount": amount, "currency": currency, "isNull": False}
    entry.update(extra)
    return {"netliquidation": entry}


@pytest.fixture
def cfg():
    return PortfolioConfig(starting_equity=100_000.0)


def assert_fallback(reading, reason):
    assert reading == EquityReading(100_000.0, "starting_equity", reason)


def test_valid_value_is_used(cfg):
    r = resolve_total_equity(FakeAccountClient(nl()), cfg)
    assert r == EquityReading(123456.78, "gateway", "", "USD")


def test_int_amount_and_lowercase_currency_accepted(cfg):
    r = resolve_total_equity(FakeAccountClient(nl(150_000, "usd")), cfg)
    assert r.source == "gateway"
    assert r.equity == 150_000.0
    assert isinstance(r.equity, float)


def test_missing_currency_accepted(cfg):
    summary = {"netliquidation": {"amount": 120_000.0}}
    r = resolve_total_equity(FakeAccountClient(summary), cfg)
    assert r == EquityReading(120_000.0, "gateway", "", None)


def test_disabled_by_config():
    cfg = PortfolioConfig(use_account_equity=False)
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl()), cfg),
        "disabled (portfolio.use_account_equity: false)",
    )


def test_client_none(cfg):
    assert_fallback(resolve_total_equity(None, cfg), "no gateway client")


@pytest.mark.parametrize(
    "exc,reason",
    [
        (NotAuthenticatedError("401 on /x"), "gateway error: NotAuthenticatedError: 401 on /x"),
        (CPGWError("boom"), "gateway error: CPGWError: boom"),
        (RuntimeError("odd"), "gateway error: RuntimeError: odd"),
    ],
)
def test_gateway_exceptions_fall_back(cfg, exc, reason):
    assert_fallback(resolve_total_equity(FakeAccountClient(exc=exc), cfg), reason)


def test_client_without_account_summary(cfg):
    r = resolve_total_equity(NoSummaryClient(), cfg)
    assert r.source == "starting_equity"
    assert r.reason.startswith("gateway error: AttributeError:")


@pytest.mark.parametrize(
    "summary,reason",
    [
        ([], "bad summary: summary is not a dict"),
        ({}, "bad summary: netliquidation missing"),
        ({"netliquidation": 5}, "bad summary: netliquidation missing"),
        (nl(isNull=True), "bad summary: netliquidation isNull"),
        ({"netliquidation": {"currency": "USD"}}, "bad summary: amount missing"),
        (nl("123456"), "bad summary: amount is not a number"),
        (nl(True), "bad summary: amount is not a number"),
    ],
)
def test_bad_summaries(cfg, summary, reason):
    assert_fallback(resolve_total_equity(FakeAccountClient(summary), cfg), reason)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_not_finite(cfg, value):
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl(value)), cfg),
        "net liquidation not finite",
    )


@pytest.mark.parametrize("value,text", [(0, "0.00"), (-5.5, "-5.50")])
def test_not_positive(cfg, value, text):
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl(value)), cfg),
        f"net liquidation not positive ({text})",
    )


def test_currency_mismatch(cfg):
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl(150_000, "EUR")), cfg),
        "currency EUR, expected USD",
    )


def test_below_min_ratio(cfg):
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl(24_999.99)), cfg),
        f"net liquidation 24,999.99 {RANGE_TEXT}",
    )


def test_above_max_ratio_and_million_rejected(cfg):
    assert_fallback(
        resolve_total_equity(FakeAccountClient(nl(1_000_000)), cfg),
        f"net liquidation 1,000,000.00 {RANGE_TEXT}",
    )
    assert resolve_total_equity(FakeAccountClient(nl(400_000.01)), cfg).source == "starting_equity"


@pytest.mark.parametrize("value", [25_000.0, 400_000.0])
def test_bounds_inclusive(cfg, value):
    r = resolve_total_equity(FakeAccountClient(nl(value)), cfg)
    assert r.source == "gateway"
    assert r.equity == value


def test_starting_equity_moves_the_bounds():
    cfg = PortfolioConfig(starting_equity=1_000_000.0)
    assert resolve_total_equity(FakeAccountClient(nl(1_000_000)), cfg).source == "gateway"


def test_error_reason_masks_account_and_is_capped(cfg):
    exc = CPGWError(f"GET /v1/api/portfolio/{ACCOUNT}/summary failed " + "x" * 500)
    r = resolve_total_equity(FakeAccountClient(exc=exc), cfg)
    assert ACCOUNT not in r.reason
    assert "***00" in r.reason
    assert len(r.reason) <= len("gateway error: CPGWError: ") + 160


def test_mask_account():
    assert _mask_account(f"a {ACCOUNT} b {ACCOUNT}", ACCOUNT) == "a ***00 b ***00"
    assert _mask_account("nothing", ACCOUNT) == "nothing"
    assert _mask_account("text", None) == "text"
    assert _mask_account("text", "") == "text"


def test_never_raises_on_odd_account_id(cfg):
    r = resolve_total_equity(FakeAccountClient(exc=CPGWError("x"), account_id=object()), cfg)
    assert r.source == "starting_equity"


def test_parse_net_liquidation():
    assert parse_net_liquidation(nl(10, "USD")) == (10.0, "USD")
    assert parse_net_liquidation({"netliquidation": {"amount": 1.5}}) == (1.5, None)
    with pytest.raises(ValueError):
        parse_net_liquidation("nope")


def test_format_gateway():
    r = EquityReading(123456.78, "gateway", "", "USD")
    assert format_equity_event(r) == "equity: net liquidation 123,456.78 USD (gateway)"


def test_format_gateway_without_currency():
    r = EquityReading(1000.0, "gateway", "", None)
    assert format_equity_event(r) == "equity: net liquidation 1,000.00 (gateway)"


def test_format_fallback():
    r = EquityReading(100_000.0, "starting_equity", "no gateway client")
    assert format_equity_event(r) == (
        "equity: starting_equity 100,000.00 (fallback: no gateway client)"
    )
