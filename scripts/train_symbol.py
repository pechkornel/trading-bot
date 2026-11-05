"""
Train one symbol's model and save it to the registry.

Usage:
    python -m scripts.train_symbol --symbol SPY --kind xgboost --classifier multiclass
    python -m scripts.train_symbol --symbol SPY --kind xgboost --classifier binary --name SPY_5min_xgb
    python -m scripts.train_symbol --symbol SPY --kind lstm   --name SPY_5min_lstm

Phase 7+: pulls historical bars from CPGW (Client Portal Gateway) instead of TWS.
Requires that `live/cpgw_keeper.py` has already written a valid session file
to logs/cpgw_session.json — start the keeper first.
"""

from __future__ import annotations

import argparse
import sys

from core.config import assert_paper_mode, load_app_config, load_symbol
from core.cpgw_client import CPGWClient
from core.features import get_features
from core.indicators import calculate_dynamic_threshold, calculate_indicators, capitalize_columns
from live.cpgw_keeper import read_session
from models.registry import save_model
from models.train import train_lstm, train_xgboost


def default_name(symbol: str, kind: str, timeframe_label: str) -> str:
    return f"{symbol}_{timeframe_label}_{kind}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train a model for one symbol and register it.")
    p.add_argument("--symbol", required=True)
    p.add_argument("--kind", choices=["xgboost", "lstm"], default="xgboost")
    p.add_argument("--classifier", choices=["binary", "multiclass"], default="multiclass",
                   help="(xgboost only)")
    p.add_argument("--no-smote", action="store_true", help="(xgboost only) disable SMOTE")
    p.add_argument("--name", default=None, help="Registry name (default <symbol>_<tf>_<kind>)")
    p.add_argument("--feature-set", default=None,
                   help="Override feature set (full / indicators_only / ohlcv)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    app_cfg = load_app_config()
    assert_paper_mode(app_cfg)
    sym_cfg = load_symbol(args.symbol)
    feature_name = args.feature_set or sym_cfg.feature_set
    features = get_features(feature_name)
    name = args.name or default_name(args.symbol, args.kind, sym_cfg.timeframe_label)

    session = read_session()
    if session is None or not session.get("cookie"):
        print("[train] No CPGW session — run `python -m live.cpgw_keeper` first.")
        return 1

    print(f"[train] CPGW {app_cfg.ib.host}:{app_cfg.ib.port}  symbol={args.symbol}  kind={args.kind}")
    client = CPGWClient(cookie=session["cookie"], port=app_cfg.ib.port)
    if session.get("account_id"):
        client.account_id = session["account_id"]

    try:
        print(f"[train] fetching {args.symbol} bar={sym_cfg.bar_size} period={sym_cfg.history_duration}")
        raw = client.get_historical_data(
            args.symbol, bar=sym_cfg.bar_size, period=sym_cfg.history_duration,
        )
        if raw is None or raw.empty:
            print("[train] no data returned; check market hours / contract / permissions")
            return 1

        df = capitalize_columns(raw)
        df = calculate_indicators(
            df,
            adx_period=sym_cfg.indicator.adx_period,
            atr_period=sym_cfg.indicator.atr_period,
            bb_window=sym_cfg.indicator.bb_window,
            bb_num_std_dev=sym_cfg.indicator.bb_num_std_dev,
            ema_span=sym_cfg.indicator.ema_span,
            ma_1=sym_cfg.indicator.ma_1,
            ma_2=sym_cfg.indicator.ma_2,
            ma_3=sym_cfg.indicator.ma_3,
            rsi_length=sym_cfg.indicator.rsi_length,
            lr_period=sym_cfg.indicator.lr_period,
            lr_std_mult=sym_cfg.indicator.lr_std_mult,
            lastlow_periods=sym_cfg.indicator.lastlow_periods,
        )

        threshold = calculate_dynamic_threshold(df, window=sym_cfg.trading.shift)
        print(f"[train] dynamic threshold={threshold:.4%}  bars={len(df)}  features={len(features)}")

        if args.kind == "xgboost":
            model, meta = train_xgboost(
                df, features, threshold, sym_cfg.trading.shift,
                classifier=args.classifier, use_smote=not args.no_smote,
            )
        else:
            model, meta = train_lstm(df, features, threshold, sym_cfg.trading.shift)

        meta["symbol"] = args.symbol
        meta["bar_size"] = sym_cfg.bar_size
        meta["timeframe_label"] = sym_cfg.timeframe_label
        meta["feature_set"] = feature_name
        meta["training_bars"] = int(len(df))

        path = save_model(name, model, meta)
        print(f"[train] saved → {path}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
