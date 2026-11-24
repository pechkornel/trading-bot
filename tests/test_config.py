import pytest

from core.config import (
    AppConfig,
    IBConfig,
    PortfolioConfig,
    assert_paper_mode,
    load_app_config,
    load_portfolio,
)


def test_env_ib_port_5000_passes_paper_lock(monkeypatch):
    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "5000")
    cfg = load_app_config()
    assert cfg.ib.port == 5000
    assert_paper_mode(cfg)


def test_env_ib_port_4002_is_refused(monkeypatch):
    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "4002")
    with pytest.raises(RuntimeError, match="IB_PORT=4002 is not a paper port"):
        assert_paper_mode(load_app_config())


def test_default_config_passes_paper_lock():
    assert_paper_mode(AppConfig())


def test_live_mode_is_refused():
    with pytest.raises(RuntimeError, match="PAPER=false"):
        assert_paper_mode(AppConfig(paper=False))


@pytest.mark.parametrize("port", [4001, 4002, 7496, 7497])
def test_non_cpgw_port_is_refused_without_mentioning_legacy_ports(port):
    with pytest.raises(RuntimeError) as exc:
        assert_paper_mode(AppConfig(ib=IBConfig(port=port)))
    msg = str(exc.value)
    assert f"IB_PORT={port} is not a paper port" in msg
    assert "IB_PORT=5000" in msg
    for legacy in ("4001", "4002", "7496", "7497"):
        if legacy != str(port):
            assert legacy not in msg


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_paper_truthy_values_pass(monkeypatch, value):
    monkeypatch.setenv("PAPER", value)
    monkeypatch.setenv("IB_PORT", "5000")
    cfg = load_app_config()
    assert cfg.paper is True
    assert_paper_mode(cfg)


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "maybe"])
def test_paper_falsy_or_unknown_values_are_refused(monkeypatch, value):
    # _env_bool treats any unknown string as false.
    monkeypatch.setenv("PAPER", value)
    monkeypatch.setenv("IB_PORT", "5000")
    cfg = load_app_config()
    assert cfg.paper is False
    with pytest.raises(RuntimeError, match="PAPER=false"):
        assert_paper_mode(cfg)


def test_unset_env_defaults_to_paper_on_port_5000():
    cfg = load_app_config()
    assert cfg.paper is True
    assert cfg.ib.port == 5000
    assert_paper_mode(cfg)


def test_paper_error_wins_over_port_error(monkeypatch):
    monkeypatch.setenv("PAPER", "false")
    monkeypatch.setenv("IB_PORT", "4002")
    with pytest.raises(RuntimeError, match="PAPER=false"):
        assert_paper_mode(load_app_config())


def test_port_5001_is_refused(monkeypatch):
    monkeypatch.setenv("PAPER", "true")
    monkeypatch.setenv("IB_PORT", "5001")
    with pytest.raises(RuntimeError, match="IB_PORT=5001 is not a paper port"):
        assert_paper_mode(load_app_config())


def test_portfolio_config_default_cooldown_matches_yaml():
    assert PortfolioConfig().reentry_cooldown_sec == 86400


def test_loaded_portfolio_cooldown_is_one_day():
    assert load_portfolio().reentry_cooldown_sec == 86400


def test_portfolio_config_account_equity_defaults():
    cfg = PortfolioConfig()
    assert cfg.use_account_equity is True
    assert cfg.equity_min_ratio == 0.25
    assert cfg.equity_max_ratio == 2.0
    assert cfg.equity_currency == "USD"


def test_load_portfolio_accepts_account_equity_keys(monkeypatch):
    raw = {
        "portfolio": {
            "starting_equity": 50_000.0,
            "use_account_equity": False,
            "equity_min_ratio": 0.5,
            "equity_max_ratio": 2.0,
            "equity_currency": "EUR",
        }
    }
    monkeypatch.setattr("core.config._load_yaml", lambda path: raw)
    cfg = load_portfolio()
    assert cfg.starting_equity == 50_000.0
    assert cfg.use_account_equity is False
    assert cfg.equity_min_ratio == 0.5
    assert cfg.equity_max_ratio == 2.0
    assert cfg.equity_currency == "EUR"


def test_load_portfolio_without_account_equity_keys_uses_defaults(monkeypatch):
    raw = {"portfolio": {"starting_equity": 100_000.0, "cycle_interval_sec": 60}}
    monkeypatch.setattr("core.config._load_yaml", lambda path: raw)
    cfg = load_portfolio()
    assert cfg.use_account_equity is True
    assert cfg.equity_min_ratio == 0.25
    assert cfg.equity_max_ratio == 2.0
    assert cfg.equity_currency == "USD"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity_min_ratio": 0},
        {"equity_min_ratio": 1.5},
        {"equity_max_ratio": 0.5},
        {"starting_equity": 0},
    ],
)
def test_portfolio_config_rejects_bad_equity_settings(kwargs):
    with pytest.raises(ValueError):
        PortfolioConfig(**kwargs)
