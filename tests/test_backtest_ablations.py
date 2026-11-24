import dataclasses

import pytest

from backtest.ablations import DEFAULT_COST_BPS, VARIANTS, build_cfg
from core.config import CoreSleeveConfig
from core.scoring import score_universe
from core.sleeves.core import CoreSleeve
from tests.backtest_synth import small_cfg
from tests.conftest import linear_closes, make_macro


def variant(name):
    return next(v for v in VARIANTS if v.name == name)


def test_variant_order():
    assert [v.name for v in VARIANTS] == [
        "base",
        "momentum_only",
        "no_macro",
        "no_risk_off",
        "top_3",
        "top_5",
        "cost_0",
        "cost_20",
    ]


@pytest.mark.parametrize("v", VARIANTS, ids=lambda v: v.name)
def test_every_variant_builds_a_config_the_bot_accepts(v):
    cfg = build_cfg(CoreSleeveConfig(), v)
    CoreSleeve(cfg)  # raises if the weights do not sum to 1
    assert v.description


def test_base_config_is_unchanged():
    base = CoreSleeveConfig()
    assert build_cfg(base, variant("base")) == base
    assert variant("base").cost_bps == DEFAULT_COST_BPS == 5.0


def test_momentum_only_weights():
    cfg = build_cfg(CoreSleeveConfig(), variant("momentum_only"))
    assert (cfg.momentum_weight, cfg.macro_weight, cfg.trend_weight) == (1.0, 0.0, 0.0)


def test_no_macro_renormalises_from_the_base_config():
    cfg = build_cfg(CoreSleeveConfig(), variant("no_macro"))
    assert cfg.macro_weight == 0.0
    assert cfg.momentum_weight == pytest.approx(4 / 7)
    assert cfg.trend_weight == pytest.approx(3 / 7)
    other = CoreSleeveConfig(momentum_weight=0.5, macro_weight=0.2, trend_weight=0.3)
    cfg2 = build_cfg(other, variant("no_macro"))
    assert cfg2.momentum_weight == pytest.approx(0.5 / 0.8)
    assert cfg2.trend_weight == pytest.approx(0.3 / 0.8)


def test_no_risk_off_disables_the_switch_with_the_bot_code():
    closes = {
        "SPY": linear_closes(120, -1.0, 20),  # far below its 5 day MA
        "TLT": linear_closes(100, 0.5, 20),
        "GLD": linear_closes(100, 0.1, 20),
    }
    macro = make_macro(vix=80.0)
    base = small_cfg()
    assert score_universe(closes, macro, build_cfg(base, variant("base"))).risk_off is True
    off = build_cfg(base, variant("no_risk_off"))
    assert score_universe(closes, macro, off).risk_off is False


def test_top_n_variants():
    assert build_cfg(CoreSleeveConfig(), variant("top_3")).hold_top_n == 3
    assert build_cfg(CoreSleeveConfig(), variant("top_5")).hold_top_n == 5


def test_cost_variants_keep_the_base_config():
    base = CoreSleeveConfig()
    assert variant("cost_0").cost_bps == 0.0
    assert variant("cost_20").cost_bps == 20.0
    assert build_cfg(base, variant("cost_0")) == base
    assert build_cfg(base, variant("cost_20")) == base


def test_build_cfg_does_not_mutate_the_base():
    base = CoreSleeveConfig()
    before = dataclasses.replace(base)
    for v in VARIANTS:
        build_cfg(base, v)
    assert base == before
