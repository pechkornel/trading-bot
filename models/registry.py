"""
Model artifact registry.

Each artifact lives in models/artifacts/<name>/ as:
  - model.pkl       (or model.keras for LSTM)
  - metadata.json   (model_type, features, accuracy, etc.)
  - scaler.gz       (LSTM only — MinMaxScaler from training)

This replaces the loose .pkl files sitting next to the old scripts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

from core.config import ARTIFACTS_DIR


def _model_dir(name: str) -> Path:
    return ARTIFACTS_DIR / name


def list_models() -> list[dict[str, Any]]:
    """Return metadata for every artifact in the registry."""
    if not ARTIFACTS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(ARTIFACTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "metadata.json"
        if not meta_path.exists():
            continue
        with meta_path.open() as f:
            meta = json.load(f)
        meta["name"] = d.name
        out.append(meta)
    return out


def save_model(name: str, model: Any, metadata: dict[str, Any]) -> Path:
    """
    Persist a trained model + metadata under models/artifacts/<name>/.

    For LSTM models: pops `_scaler` from metadata and saves it as scaler.gz.
    """
    path = _model_dir(name)
    path.mkdir(parents=True, exist_ok=True)

    meta = dict(metadata)
    meta["name"] = name
    meta["saved_at"] = datetime.now(timezone.utc).isoformat()

    scaler = meta.pop("_scaler", None)
    if scaler is not None:
        joblib.dump(scaler, path / "scaler.gz")
        meta["scaler_file"] = "scaler.gz"

    model_type = meta.get("model_type", "xgboost")
    if model_type == "lstm":
        model.save(path / "model.keras")
        meta["model_file"] = "model.keras"
    else:
        joblib.dump(model, path / "model.pkl")
        meta["model_file"] = "model.pkl"

    with (path / "metadata.json").open("w") as f:
        json.dump(meta, f, indent=2, default=str)

    return path


def load_model(name: str) -> tuple[Any, dict[str, Any], Any | None]:
    """
    Load a model by registry name.
    Returns (model, metadata, scaler_or_None).
    """
    path = _model_dir(name)
    meta_path = path / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No model named {name!r} in registry ({path})")

    with meta_path.open() as f:
        meta = json.load(f)

    model_file = meta.get("model_file", "model.pkl")
    model_path = path / model_file

    if model_file.endswith(".keras"):
        from tensorflow.keras.models import load_model as keras_load

        model = keras_load(model_path)
    else:
        model = joblib.load(model_path)

    scaler = None
    if "scaler_file" in meta:
        scaler = joblib.load(path / meta["scaler_file"])

    return model, meta, scaler
