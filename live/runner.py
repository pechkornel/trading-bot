"""
Single-symbol live runner. Phase 7 — talks to CPGW (REST) instead of TWS API.

Flow per cycle:
  1. Fetch latest bars from CPGW for the configured bar_size / period
  2. Compute indicators
  3. Score the registry model on the last fully-formed bar
  4. Log the prediction to SQLite
  5. If confidence > threshold and prediction == bullish class, optionally
     place a bracket order via CPGW:
        - --dry-run  (default): log it, don't send it
        - --live:              send to the paper account

Usage:
    python -m live.runner --symbol SPY --model SPY_5min_xgboost
    python -m live.runner --symbol SPY --model SPY_5min_xgboost --live --quantity 5
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any

import numpy as np

from core.config import assert_paper_mode, load_app_config, load_symbol, SymbolConfig
from core.cpgw_client import CPGWClient, NotAuthenticatedError
from core.features import get_features
from core.indicators import calculate_indicators, capitalize_columns
from live.cpgw_keeper import read_session
from live.notifier import format_trade_alert, send_email
from live.storage import Storage
from models.registry import load_model


# Bullish classes for binary (1) and multiclass (2) models.
BULLISH_CLASSES = {1, 2}


def _client_from_session(port: int) -> CPGWClient:
    """Load the cookie that cpgw_keeper writes; refuse to start if it's missing."""
    session = read_session()
    if session is None or not session.get("cookie"):
        raise RuntimeError(
            "No CPGW session found. Start `python -m live.cpgw_keeper` first "
            "(or the cpgw_keeper service) — it logs in and writes the session "
            "cookie that the runners read."
        )
    client = CPGWClient(cookie=session["cookie"], port=port)
    if session.get("account_id"):
        client.account_id = session["account_id"]
    return client


class Runner:
    def __init__(
        self,
        *,
        symbol: str,
        model_name: str,
        dry_run: bool,
        quantity: float,
        interval_override: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.model_name = model_name
        self.dry_run = dry_run
        self.quantity = quantity
        self.stopped = False

        self.app_cfg = load_app_config()
        assert_paper_mode(self.app_cfg)
        self.sym_cfg: SymbolConfig = load_symbol(symbol)
        self.interval = interval_override or self.sym_cfg.trading.prediction_interval_sec

        self.storage = Storage()
        self.model, self.meta, self.scaler = load_model(model_name)
        self.features = self.meta.get("features") or get_features(self.sym_cfg.feature_set)

        self.client: CPGWClient | None = None

    def start(self) -> None:
        banner = (
            f"PAPER MODE (CPGW) — port={self.app_cfg.ib.port} "
            f"symbol={self.symbol} model={self.model_name} dry_run={self.dry_run}"
        )
        self.storage.log_event("info", "runner", f"starting | {banner}")
        print("=" * len(banner))
        print(banner)
        print("=" * len(banner))
        self.client = _client_from_session(self.app_cfg.ib.port)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_: Any) -> None:
        print("\n[runner] stop signal received, finishing current cycle…")
        self.stopped = True

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
        self.storage.log_event("info", "runner", f"stopped symbol={self.symbol}")
        print("[runner] disconnected.")

    def loop(self) -> None:
        while not self.stopped:
            cycle_start = time.time()
            try:
                self._cycle()
            except NotAuthenticatedError as e:
                self.storage.log_event(
                    "warn",
                    "runner",
                    f"session expired ({e}); cpgw_keeper should re-login. Sleeping.",
                )
            except Exception as e:  # noqa: BLE001
                self.storage.log_event("error", "runner", f"cycle error: {e}")
                print(f"[runner] cycle error: {e}")
            elapsed = time.time() - cycle_start
            sleep = max(0.0, self.interval - elapsed)
            slept = 0.0
            while slept < sleep and not self.stopped:
                time.sleep(min(0.5, sleep - slept))
                slept += 0.5

    def _cycle(self) -> None:
        assert self.client is not None
        # Re-read the session cookie every cycle so the keeper's refreshes flow through
        session = read_session()
        if session and session.get("cookie"):
            self.client.update_cookie(session["cookie"])

        raw = self.client.get_historical_data(
            self.symbol,
            bar=self.sym_cfg.bar_size,
            period=self.sym_cfg.history_duration,
        )
        if raw is None or raw.empty:
            self.storage.log_event("warn", "runner", "empty data from CPGW; skipping cycle")
            return

        df = capitalize_columns(raw)
        df = calculate_indicators(
            df,
            adx_period=self.sym_cfg.indicator.adx_period,
            atr_period=self.sym_cfg.indicator.atr_period,
            bb_window=self.sym_cfg.indicator.bb_window,
            bb_num_std_dev=self.sym_cfg.indicator.bb_num_std_dev,
            ema_span=self.sym_cfg.indicator.ema_span,
            ma_1=self.sym_cfg.indicator.ma_1,
            ma_2=self.sym_cfg.indicator.ma_2,
            ma_3=self.sym_cfg.indicator.ma_3,
            rsi_length=self.sym_cfg.indicator.rsi_length,
            lr_period=self.sym_cfg.indicator.lr_period,
            lr_std_mult=self.sym_cfg.indicator.lr_std_mult,
            lastlow_periods=self.sym_cfg.indicator.lastlow_periods,
        )

        missing = [f for f in self.features if f not in df.columns]
        if missing:
            self.storage.log_event("error", "runner", f"missing features: {missing}")
            return

        last = df[self.features].tail(1)
        if last.isnull().values.any():
            self.storage.log_event("warn", "runner", "NaNs in last row; skipping prediction")
            return

        proba = self.model.predict_proba(last)
        prediction = int(np.argmax(proba, axis=1)[0])
        confidence = float(np.max(proba))
        close = float(df["Close"].iloc[-1])
        atr = float(df["ATR"].iloc[-1])

        self.storage.log_prediction(
            symbol=self.symbol,
            model=self.model_name,
            prediction=prediction,
            confidence=confidence,
            close_price=close,
            atr=atr,
            features={k: float(v) for k, v in last.iloc[0].items()},
        )
        print(
            f"[{self.symbol}] close={close:.2f}  atr={atr:.4f}  "
            f"pred={prediction}  conf={confidence:.1%}"
        )

        if (
            confidence >= self.sym_cfg.trading.confidence_threshold
            and prediction in BULLISH_CLASSES
        ):
            self._handle_bullish(close=close, atr=atr, confidence=confidence)

    def _handle_bullish(self, *, close: float, atr: float, confidence: float) -> None:
        cfg = self.sym_cfg
        entry = round(close * 1.005, 2)
        tp = round(close + cfg.trading.rr_ratio * cfg.trading.atr_multiplier * atr, 2)
        stop = round(close - cfg.trading.atr_multiplier * atr, 2)

        if self.dry_run:
            order_id = self.storage.log_order(
                symbol=self.symbol,
                action="BUY",
                quantity=self.quantity,
                entry_price=entry,
                tp_price=tp,
                stop_price=stop,
                parent_id=None,
                tp_id=None,
                stop_id=None,
                dry_run=True,
                status="logged",
                note=f"conf={confidence:.3f}",
            )
            print(
                f"[{self.symbol}] DRY-RUN BUY ×{self.quantity} "
                f"entry={entry} tp={tp} stop={stop} (id={order_id})"
            )
        else:
            assert self.client is not None
            try:
                tickets = self.client.place_bracket_order(
                    symbol=self.symbol,
                    quantity=self.quantity,
                    entry_price=entry,
                    take_profit_price=tp,
                    stop_loss_price=stop,
                )
                parent, take_profit, stop_t = tickets + [None, None, None]
                self.storage.log_order(
                    symbol=self.symbol,
                    action="BUY",
                    quantity=self.quantity,
                    entry_price=entry,
                    tp_price=tp,
                    stop_price=stop,
                    parent_id=int(parent.order_id)
                    if parent and parent.order_id.isdigit()
                    else None,
                    tp_id=int(take_profit.order_id)
                    if take_profit and take_profit.order_id.isdigit()
                    else None,
                    stop_id=int(stop_t.order_id) if stop_t and stop_t.order_id.isdigit() else None,
                    dry_run=False,
                    status="placed",
                    note=f"conf={confidence:.3f}",
                )
                print(
                    f"[{self.symbol}] PAPER BUY ×{self.quantity} "
                    f"entry={entry} tp={tp} stop={stop} ids={[t.order_id for t in tickets if t]}"
                )
            except Exception as e:  # noqa: BLE001
                self.storage.log_event("error", "runner", f"order placement failed: {e}")
                self.storage.log_order(
                    symbol=self.symbol,
                    action="BUY",
                    quantity=self.quantity,
                    entry_price=entry,
                    tp_price=tp,
                    stop_price=stop,
                    parent_id=None,
                    tp_id=None,
                    stop_id=None,
                    dry_run=False,
                    status="failed",
                    note=str(e),
                )
                return

        subject, body = format_trade_alert(
            symbol=self.symbol,
            action="BUY",
            quantity=self.quantity,
            entry_price=entry,
            tp_price=tp,
            stop_price=stop,
            confidence=confidence,
            dry_run=self.dry_run,
        )
        send_email(subject, body)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Single-symbol live runner (Phase 7 — CPGW).")
    p.add_argument("--symbol", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--quantity", type=float, default=1.0)
    p.add_argument("--interval", type=int, default=None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--live", dest="dry_run", action="store_false")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = Runner(
        symbol=args.symbol,
        model_name=args.model,
        dry_run=args.dry_run,
        quantity=args.quantity,
        interval_override=args.interval,
    )
    runner.start()
    try:
        runner.loop()
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
