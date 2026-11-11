"""Sleeve implementations.

Phase 8 active sleeves:
  - CoreSleeve            — medium-term macro+TA driven, weekly evaluation
  - SentimentSatellite    — daily news-sentiment driven (Phase 8B, not built yet)

Legacy (preserved, may be disabled):
  - AllWeatherSleeve      — fixed-weight All-Weather, replaced by CoreSleeve
  - TacticalSleeve        — 5-min XGBoost, disabled by config flag
"""

from core.sleeves.core import CoreCycleResult, CoreDecision, CoreSleeve
from core.sleeves.long_term import AllWeatherSleeve, LongTermDecision, RebalanceResult
from core.sleeves.sentiment_satellite import (
    Position,
    SatelliteCycleResult,
    SatelliteEntry,
    SatelliteExit,
    SentimentInput,
    SentimentSatelliteSleeve,
)
from core.sleeves.tactical import TacticalDecision, TacticalInput, TacticalSleeve

__all__ = [
    "CoreSleeve",
    "CoreDecision",
    "CoreCycleResult",
    "SentimentSatelliteSleeve",
    "SentimentInput",
    "Position",
    "SatelliteEntry",
    "SatelliteExit",
    "SatelliteCycleResult",
    "AllWeatherSleeve",
    "LongTermDecision",
    "RebalanceResult",
    "TacticalSleeve",
    "TacticalDecision",
    "TacticalInput",
]
