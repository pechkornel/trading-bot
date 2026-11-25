"""KeeperConfig: defaults, the shipped YAML block and strict validation."""

from __future__ import annotations

import datetime as dt

import pytest

import core.config as config
from core.config import KeeperConfig


def _point_at(monkeypatch, tmp_path, text: str) -> None:
    (tmp_path / "symbols.yaml").write_text(text)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)


def _check_defaults(cfg: KeeperConfig) -> None:
    assert cfg.tickle_interval_sec == 60
    assert cfg.timezone == "America/New_York"
    assert cfg.nightly_gap_start == "00:45"
    assert cfg.nightly_gap_end == "01:30"
    assert cfg.maintenance_weekday == 5
    assert cfg.maintenance_start == "00:00"
    assert cfg.maintenance_end == "23:59"
    assert cfg.max_session_age_sec == 86400
    assert cfg.login_backoff_base_sec == 60
    assert cfg.login_backoff_max_sec == 1800
    assert cfg.max_failed_logins == 3
    assert cfg.failed_login_window_sec == 86400
    assert cfg.session_lost_alert_cooldown_sec == 3600
    assert cfg.paper_account_prefix == "DU"


def test_dataclass_defaults():
    _check_defaults(KeeperConfig())


def test_shipped_yaml_loads_the_defaults():
    _check_defaults(config.load_keeper_config())


def test_missing_block_gives_defaults(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "portfolio:\n  starting_equity: 1000\n")
    _check_defaults(config.load_keeper_config())


def test_empty_block_gives_defaults(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "keeper:\n")
    _check_defaults(config.load_keeper_config())


def test_override_from_yaml(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "keeper:\n  tickle_interval_sec: 45\n  timezone: UTC\n")
    cfg = config.load_keeper_config()
    assert cfg.tickle_interval_sec == 45
    assert cfg.timezone == "UTC"


def test_time_helpers_parse_to_time_objects():
    cfg = KeeperConfig()
    assert cfg.nightly_gap_start_time == dt.time(0, 45)
    assert cfg.nightly_gap_end_time == dt.time(1, 30)
    assert cfg.maintenance_start_time == dt.time(0, 0)
    assert cfg.maintenance_end_time == dt.time(23, 59)


@pytest.mark.parametrize(
    "key, value",
    [
        ("nightly_gap_start", "25:00"),
        ("nightly_gap_start", "0:45"),
        ("nightly_gap_start", "0045"),
        ("nightly_gap_end", "01:60"),
        ("maintenance_start", "noon"),
        ("maintenance_end", 130),
        ("nightly_gap_end", ""),
        ("timezone", "Mars/Olympus"),
        ("timezone", ""),
        ("timezone", 5),
        ("max_failed_logins", 0),
        ("max_failed_logins", -1),
        ("max_failed_logins", True),
        ("max_failed_logins", 2.5),
        ("tickle_interval_sec", True),
        ("tickle_interval_sec", "60"),
        ("tickle_interval_sec", 0),
        ("login_backoff_base_sec", 0),
        ("login_backoff_base_sec", False),
        ("login_backoff_max_sec", "1800"),
        ("max_session_age_sec", 0),
        ("failed_login_window_sec", -5),
        ("session_lost_alert_cooldown_sec", True),
        ("session_lost_alert_cooldown_sec", -1),
        ("maintenance_weekday", 7),
        ("maintenance_weekday", -1),
        ("maintenance_weekday", True),
        ("maintenance_weekday", "5"),
        ("paper_account_prefix", ""),
        ("paper_account_prefix", "  "),
        ("paper_account_prefix", None),
        ("paper_account_prefix", 7),
    ],
)
def test_bad_value_names_the_key(key, value):
    with pytest.raises(ValueError, match=f"keeper.{key}"):
        KeeperConfig(**{key: value})


def test_backoff_max_below_base_rejected():
    with pytest.raises(ValueError, match="login_backoff_max_sec"):
        KeeperConfig(login_backoff_base_sec=120, login_backoff_max_sec=60)


def test_backoff_max_equal_to_base_allowed():
    KeeperConfig(login_backoff_base_sec=60, login_backoff_max_sec=60)


def test_gap_end_must_be_after_start():
    with pytest.raises(ValueError, match="nightly_gap_end"):
        KeeperConfig(nightly_gap_start="02:00", nightly_gap_end="01:00")


def test_maintenance_end_must_be_after_start():
    with pytest.raises(ValueError, match="maintenance_end"):
        KeeperConfig(maintenance_start="10:00", maintenance_end="10:00")


def test_unknown_yaml_key_rejected(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "keeper:\n  tickle_every: 5\n")
    with pytest.raises(TypeError):
        config.load_keeper_config()


def test_yaml_boolean_for_number_rejected(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "keeper:\n  max_failed_logins: true\n")
    with pytest.raises(ValueError, match="keeper.max_failed_logins"):
        config.load_keeper_config()


def test_yaml_unquoted_time_rejected(monkeypatch, tmp_path):
    # YAML 1.1 reads an unquoted 10:30 as a sexagesimal integer (630), not a string
    _point_at(monkeypatch, tmp_path, "keeper:\n  nightly_gap_end: 10:30\n")
    with pytest.raises(ValueError, match="keeper.nightly_gap_end"):
        config.load_keeper_config()
