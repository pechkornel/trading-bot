"""SENTIMENT_PROVIDER in .env overrides the YAML sentiment provider."""

from __future__ import annotations

import pytest

import core.config as config


@pytest.fixture
def yaml_provider(monkeypatch, tmp_path):
    """Point the loader at a temp symbols.yaml with a chosen provider."""

    def _set(provider: str | None, block: str = "sentiment_provider") -> None:
        lines = [f"{block}:", "  poll_interval_sec: 1800"]
        if provider is not None:
            lines.append(f"  provider: {provider}")
        (tmp_path / "symbols.yaml").write_text("\n".join(lines) + "\n")
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    return _set


def test_unset_uses_yaml(yaml_provider):
    yaml_provider("alpha_vantage")
    assert config.load_sentiment_config().provider == "alpha_vantage"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_uses_yaml(yaml_provider, monkeypatch, value):
    yaml_provider("alpha_vantage")
    monkeypatch.setenv("SENTIMENT_PROVIDER", value)
    assert config.load_sentiment_config().provider == "alpha_vantage"


def test_env_overrides_yaml(yaml_provider, monkeypatch):
    yaml_provider("alpha_vantage")
    monkeypatch.setenv("SENTIMENT_PROVIDER", "stub")
    cfg = config.load_sentiment_config()
    assert cfg.provider == "stub"
    assert cfg.poll_interval_sec == 1800


def test_env_value_is_stripped_and_lowercased(yaml_provider, monkeypatch):
    yaml_provider("stub")
    monkeypatch.setenv("SENTIMENT_PROVIDER", "  Alpha_Vantage ")
    assert config.load_sentiment_config().provider == "alpha_vantage"


def test_env_applies_when_yaml_has_no_provider(yaml_provider, monkeypatch):
    yaml_provider(None)
    monkeypatch.setenv("SENTIMENT_PROVIDER", "alpha_vantage")
    assert config.load_sentiment_config().provider == "alpha_vantage"


def test_env_applies_to_legacy_sentiment_block(yaml_provider, monkeypatch):
    yaml_provider("alpha_vantage", block="sentiment")
    monkeypatch.setenv("SENTIMENT_PROVIDER", "stub")
    assert config.load_sentiment_config().provider == "stub"


@pytest.mark.parametrize("value", ["alphavantage", "openai", "none"])
def test_invalid_value_raises(yaml_provider, monkeypatch, value):
    yaml_provider("stub")
    monkeypatch.setenv("SENTIMENT_PROVIDER", value)
    with pytest.raises(ValueError, match="SENTIMENT_PROVIDER"):
        config.load_sentiment_config()


def test_loads_dotenv(yaml_provider, monkeypatch):
    yaml_provider("alpha_vantage")
    calls = []
    monkeypatch.setattr("core.config.load_dotenv", lambda *a, **k: calls.append(a))
    config.load_sentiment_config()
    assert calls == [(config.ROOT / ".env",)]
