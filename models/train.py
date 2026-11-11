"""
Model training.

Consolidates:
  - model_selector_XGBOOSt/models.py     (3-class XGBoost + SMOTE)
  - trading_bot_1_for_tests/models.py    (2-class XGBoost + class weights)
  - model_selector_lstm/main.py:122-213  (LSTM 2-layer 128, sparse softmax)

Public entry points:
  - train_xgboost(df, features, ...)
  - train_lstm(df, features, ...)

Both return a (model, metadata_dict) tuple. `metadata_dict` gets saved
alongside the artifact by the registry.
"""

from __future__ import annotations

import itertools
import timeit
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import train_test_split
from sklearn.utils.class_weight import compute_class_weight


def classify_trend_binary(change: float, threshold: float) -> int:
    """1 = uptrend (change > threshold), 0 = not-uptrend. Original trading_bot_1 behavior."""
    return 1 if change > threshold else 0


def classify_trend_multiclass(change: float, threshold: float) -> int:
    """2 = uptrend, 0 = downtrend, 1 = sideways. Original model_selector_XGBOOSt behavior."""
    if change > threshold:
        return 2
    if change < -threshold:
        return 0
    return 1


def _prepare(
    df: pd.DataFrame,
    features: list[str],
    threshold: float,
    future_n: int,
    classifier: str,
) -> tuple[pd.DataFrame, pd.Series]:
    df_model = df.dropna().copy()
    if len(df_model) < 100:
        raise ValueError(f"Need at least 100 samples after dropna, got {len(df_model)}")

    missing = [f for f in features if f not in df_model.columns]
    if missing:
        raise ValueError(f"Missing features in dataframe: {missing}")

    if df_model["Volume"].eq(-1).all():
        raise ValueError("Volume column is all -1; check IB whatToShow parameter.")

    pct = (df_model["Close"].shift(-future_n) - df_model["Close"]) / df_model["Close"]
    classify_fn = classify_trend_multiclass if classifier == "multiclass" else classify_trend_binary
    df_model["Target_Trend"] = pct.apply(lambda x: classify_fn(x, threshold))
    df_model = df_model.dropna(subset=["Target_Trend"])

    return df_model[features], df_model["Target_Trend"].astype(int)


def train_xgboost(
    df: pd.DataFrame,
    features: list[str],
    threshold: float,
    future_n: int,
    classifier: str = "multiclass",
    use_smote: bool = True,
    param_grid: dict[str, list[Any]] | None = None,
    random_state: int = 1,
) -> tuple[Any, dict[str, Any]]:
    """Train an XGBoost classifier with a small grid search. Returns (model, metadata)."""
    from xgboost import XGBClassifier

    X, y = _prepare(df, features, threshold, future_n, classifier)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=random_state
    )

    if use_smote:
        from imblearn.over_sampling import SMOTE

        X_train, y_train = SMOTE(random_state=random_state).fit_resample(X_train, y_train)

    if classifier == "multiclass":
        objective = "multi:softmax"
        eval_metric = "mlogloss"
        num_class = int(y.nunique())
        weight_kwarg = {}
    else:
        objective = "binary:logistic"
        eval_metric = "aucpr"
        num_class = None
        weights = compute_class_weight("balanced", classes=np.unique(y_train), y=y_train)
        weight_kwarg = {"scale_pos_weight": float(weights[1] / weights[0])}

    if param_grid is None:
        param_grid = {
            "learning_rate": [0.01, 0.08],
            "n_estimators": [50, 100],
            "max_depth": [3, 5],
            "gamma": [0.1],
            "subsample": [0.6, 1.0],
            "colsample_bytree": [0.6, 1.0],
            "reg_alpha": [1],
            "reg_lambda": [0.5, 2],
            "min_child_weight": [1, 5],
        }

    best_score = float("-inf")
    best_model = None
    best_params: dict[str, Any] = {}

    keys = list(param_grid.keys())
    for combo in itertools.product(*param_grid.values()):
        params = dict(zip(keys, combo))
        start = timeit.default_timer()
        clf = XGBClassifier(
            **params,
            **weight_kwarg,
            objective=objective,
            eval_metric=eval_metric,
            num_class=num_class,
            random_state=42,
            use_label_encoder=False,
        )
        clf.fit(X_train, y_train)
        score = clf.score(X_test, y_test)
        elapsed = timeit.default_timer() - start
        print(f"score={score:.4f}  params={params}  time={elapsed:.1f}s")
        if score > best_score:
            best_score = score
            best_model = clf
            best_params = params

    pred_train = best_model.predict(X_train)
    pred_test = best_model.predict(X_test)
    acc_train = accuracy_score(y_train, pred_train)
    acc_test = accuracy_score(y_test, pred_test)

    print(f"\nBest: train_acc={acc_train:.2%}  test_acc={acc_test:.2%}  params={best_params}")
    print(classification_report(y_test, pred_test))
    print("Class distribution:", y.value_counts().to_dict())

    metadata = {
        "model_type": "xgboost",
        "classifier": classifier,
        "features": list(features),
        "future_n": future_n,
        "threshold": float(threshold),
        "best_params": best_params,
        "train_accuracy": float(acc_train),
        "test_accuracy": float(acc_test),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
    }
    return best_model, metadata


def train_lstm(
    df: pd.DataFrame,
    features: list[str],
    threshold: float,
    future_n: int,
    n_steps: int = 63,
    epochs: int = 100,
    batch_size: int = 32,
    lstm_units: int = 128,
    dropout: float = 0.2,
    random_state: int = 1,
) -> tuple[Any, dict[str, Any]]:
    """
    Train a 2-layer LSTM classifier (multiclass: down/flat/up).
    Returns (keras_model, metadata). Metadata includes the fitted scaler — caller saves it separately.
    """
    import numpy as np
    from sklearn.metrics import classification_report
    from sklearn.preprocessing import MinMaxScaler
    from sklearn.utils import class_weight
    from tensorflow.keras.layers import LSTM, Dense, Dropout
    from tensorflow.keras.models import Sequential

    X, y = _prepare(df, features, threshold, future_n, classifier="multiclass")

    split = int(len(X) * 0.8)
    X_train_df, X_test_df = X.iloc[:split], X.iloc[split:]
    y_train_s, y_test_s = y.iloc[:split], y.iloc[split:]

    scaler = MinMaxScaler()
    X_train_scaled = scaler.fit_transform(X_train_df)
    X_test_scaled = scaler.transform(X_test_df)

    def _seq(X_data, y_data):
        xs, ys = [], []
        for i in range(len(X_data) - n_steps):
            xs.append(X_data[i : i + n_steps])
            ys.append(y_data.iloc[i + n_steps])
        return np.array(xs), np.array(ys)

    X_train, y_train = _seq(X_train_scaled, y_train_s)
    X_test, y_test = _seq(X_test_scaled, y_test_s)
    print(f"X_train shape: {X_train.shape}   X_test shape: {X_test.shape}")

    num_classes = int(y.nunique())
    num_features = X.shape[1]

    model = Sequential()
    model.add(LSTM(lstm_units, return_sequences=True, input_shape=(n_steps, num_features)))
    model.add(Dropout(dropout))
    model.add(LSTM(lstm_units))
    model.add(Dropout(dropout))
    model.add(Dense(num_classes, activation="softmax"))
    model.compile(optimizer="adam", loss="sparse_categorical_crossentropy", metrics=["accuracy"])

    cw = class_weight.compute_class_weight("balanced", classes=np.unique(y_train), y=y_train)
    cw_dict = dict(enumerate(cw))
    print("Class weights:", cw_dict)

    history = model.fit(
        X_train,
        y_train,
        epochs=epochs,
        batch_size=batch_size,
        validation_data=(X_test, y_test),
        class_weight=cw_dict,
        verbose=1,
    )

    pred_probs = model.predict(X_test)
    pred = np.argmax(pred_probs, axis=1)
    print(classification_report(y_test, pred))

    metadata: dict[str, Any] = {
        "model_type": "lstm",
        "classifier": "multiclass",
        "features": list(features),
        "future_n": future_n,
        "threshold": float(threshold),
        "n_steps": n_steps,
        "epochs": epochs,
        "batch_size": batch_size,
        "lstm_units": lstm_units,
        "dropout": dropout,
        "num_classes": num_classes,
        "final_val_accuracy": float(history.history["val_accuracy"][-1]),
        "final_val_loss": float(history.history["val_loss"][-1]),
        "_scaler": scaler,  # registry strips this before serializing metadata
    }
    return model, metadata
