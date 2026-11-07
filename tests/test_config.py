import pytest

from core.config import AppConfig, IBConfig, assert_paper_mode, load_app_config


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
