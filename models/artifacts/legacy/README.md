# Legacy artifacts

These are the trained models that lived loose in `combined_backtester/` and
`model_selector_XGBOOSt/`. Copied here in Phase 1 so the new registry has a
home for them; **originals were not deleted**.

The new registry format (one folder per model + `metadata.json`) hasn't been
applied to these - we don't have ground truth for which feature set / SHIFT /
threshold each was trained with, just guesses from filenames.

| File | Likely meaning | Notes |
|---|---|---|
| `SPY_.pkl` | SPY, XGBoost. Bare filename, no accuracy in name. | Identical in `combined_backtester/` and `model_selector_XGBOOSt/`. Probably the most recent default save. |
| `SPY_5MIN_Config1_71.42.pkl` | SPY 5-min bars, `config1` features (15-feat indicators-only), 71.42% test accuracy | |
| `SPY_5_73.17%_config1.pkl` | SPY 5-min bars, `config1` features, 73.17% test accuracy | Best of the lot by reported accuracy. |
| `SPY_71.88%_.pkl` | SPY, 71.88% accuracy. Only existed in `model_selector_XGBOOSt/`. | Unclear bar size. |
| `model.pkl` | Generic save from `model_selector_XGBOOSt/main.py` (line 52, hardcoded path) | Almost certainly the last training run. |
| `model_5min.pkl` | 5-min variant. Larger file (715 KB) - possibly more trees. | |
| `model_LSTM.pkl` | LSTM model, but saved as `.pkl` not `.keras`. Probably a `joblib.dump` of the Keras model - may or may not load cleanly. | Pair with `my_scaler.gz`. |
| `my_scaler.gz` | MinMaxScaler fit during LSTM training | Required to score the LSTM. |

## What to do with them

Phase 2 plan:
1. Pick **`SPY_5_73.17%_config1.pkl`** (highest reported accuracy) and try to
   load it with the new `features.CONFIG_INDICATORS_ONLY` feature list.
2. If it loads and predicts on a fresh frame, promote it into a proper
   registry entry: `models/artifacts/SPY_5min_xgb_legacy/{model.pkl, metadata.json}`.
3. Retrain everything in Phase 2 anyway so we have known provenance - these
   become reference baselines, not production models.

## Reproducing

We don't have the exact training data these models saw (IB historical data
varies by request time). Closest reproduction:
- ticker = SPY
- bar_size = "5 mins"
- duration = "5 D"
- features = `core.features.CONFIG_INDICATORS_ONLY`
- classifier = binary (filenames suggest 2-class)
- SHIFT/future_n = unknown, try 3 and 5
