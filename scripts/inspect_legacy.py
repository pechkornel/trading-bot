"""
Probe legacy .pkl files in models/artifacts/legacy/ — try to load them
and report what features they expect, how many classes, what the saved model
type is. Useful before promoting any of them into the new registry.

Usage:
    python -m scripts.inspect_legacy
    python -m scripts.inspect_legacy --file SPY_5_73.17%_config1.pkl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import joblib

from core.config import ARTIFACTS_DIR


LEGACY_DIR = ARTIFACTS_DIR / "legacy"


def probe(path: Path) -> dict:
    info: dict = {"file": path.name, "size_bytes": path.stat().st_size}
    try:
        obj = joblib.load(path)
    except Exception as e:
        info["error"] = f"joblib.load failed: {e}"
        return info

    info["class"] = type(obj).__name__
    info["module"] = type(obj).__module__

    for attr in ("n_features_in_", "feature_names_in_", "classes_", "n_classes_"):
        if hasattr(obj, attr):
            value = getattr(obj, attr)
            try:
                info[attr] = (
                    list(value)
                    if hasattr(value, "__iter__") and not isinstance(value, str)
                    else value
                )
            except Exception:
                info[attr] = str(value)

    # XGBoost-specific
    if hasattr(obj, "get_booster"):
        try:
            booster = obj.get_booster()
            info["xgb_feature_names"] = booster.feature_names
            info["xgb_n_estimators"] = getattr(obj, "n_estimators", None)
            info["xgb_objective"] = getattr(obj, "objective", None)
        except Exception as e:
            info["xgb_error"] = str(e)

    return info


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=None, help="One file under models/artifacts/legacy/")
    args = p.parse_args(argv)

    if not LEGACY_DIR.exists():
        print(f"No legacy dir at {LEGACY_DIR}")
        return 1

    files = (
        [LEGACY_DIR / args.file]
        if args.file
        else sorted(p for p in LEGACY_DIR.iterdir() if p.suffix in {".pkl", ".gz"})
    )

    for f in files:
        print(f"\n=== {f.name} ===")
        info = probe(f)
        for k, v in info.items():
            if k == "xgb_feature_names" and v:
                print(f"  {k}: ({len(v)} features) {v}")
            else:
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
