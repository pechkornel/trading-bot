"""
Multi-sleeve portfolio runner — Phase 8 rewrite.

Active sleeves (Phase 8A):
  - core (70%) — weekly-evaluated macro + TA portfolio over 10 ETFs.
                 See core/sleeves/core.py + core/scoring.py + core/macro.py.

Disabled sleeves (preserved in code, flip `enabled: true` to re-add):
  - sentiment (Phase 8B) — daily news-sentiment satellite
  - tactical              — 5-min XGBoost (legacy)
  - long_term             — All-Weather (replaced by core)

Cadence:
  - Outer loop ticks every `portfolio.cycle_interval_sec` (default 1h).
  - Each enabled sleeve decides whether to act this cycle. Most cycles are
    no-ops (core only acts on Fri close or on drift; sentiment only acts
    when a news threshold trips). That's by design — medium-term ≠ active.

Usage:
    python -m live.portfolio_runner              # dry-run
    python -m live.portfolio_runner --once       # one cycle, exit
    python -m live.portfolio_runner --live       # send paper orders
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from datetime import datetime, timezone

from core.config import (
    assert_paper_mode,
    load_app_config,
    load_macro_config,
    load_phase8_sleeves,
    load_portfolio,
    load_sentiment_config,
    load_symbol,
    CoreSleeveConfig,
    MacroConfig,
    PortfolioConfig,
    SentimentConfig,
    SentimentSatelliteConfig,
    SymbolConfig,
    TacticalSleeveConfig,
)
from core.cpgw_client import CPGWClient, NotAuthenticatedError
from core.indicators import capitalize_columns
from core.macro import MacroFetcher
from core.market_hours import (
    MarketRegime,
    is_core_rebalance_window,
    market_state,
    should_run_entries,
    should_run_exits,
)
from core.sleeves import (
    CoreCycleResult,
    CoreDecision,
    CoreSleeve,
    Position,
    SatelliteCycleResult,
    SatelliteEntry,
    SatelliteExit,
    SentimentInput,
    SentimentSatelliteSleeve,
)
from live.cpgw_keeper import read_session
from live.notifier import format_trade_alert, send_email
from live.storage import Storage


class PortfolioRunner:
    def __init__(
        self,
        *,
        dry_run: bool,
        once: bool,
        interval_override: int | None,
    ) -> None:
        self.dry_run = dry_run
        self.once = once
        self.stopped = False

        self.app_cfg = load_app_config()
        assert_paper_mode(self.app_cfg)
        self.portfolio_cfg: PortfolioConfig = load_portfolio()
        self.core_cfg, self.sent_cfg, self.tact_cfg = load_phase8_sleeves()
        self.macro_cfg: MacroConfig = load_macro_config()
        self.interval = interval_override or self.portfolio_cfg.cycle_interval_sec

        self.storage = Storage()
        self.core = CoreSleeve(self.core_cfg) if self.core_cfg.enabled else None
        self.satellite = SentimentSatelliteSleeve(self.sent_cfg) if self.sent_cfg.enabled else None
        self.macro_fetcher = MacroFetcher(self.macro_cfg)
        self.sentiment_cfg: SentimentConfig = load_sentiment_config()

        # Load per-symbol configs for every symbol that any enabled sleeve might use.
        self.symbols: dict[str, SymbolConfig] = {}
        for sym in self._all_required_symbols():
            try:
                self.symbols[sym] = load_symbol(sym)
            except KeyError:
                self.storage.log_event(
                    "warn", "portfolio", f"{sym}: not in config/symbols.yaml universe, skipping"
                )

        self.client: CPGWClient | None = None

    def _all_required_symbols(self) -> set[str]:
        symbols: set[str] = set()
        if self.core_cfg.enabled:
            symbols.update(self.core_cfg.universe)
            symbols.update(self.core_cfg.risk_off_universe)
            symbols.add("SPY")  # always need SPY for the risk-off filter
        if self.sent_cfg.enabled:
            symbols.update(self.sent_cfg.universe)
        if self.tact_cfg.enabled:
            symbols.update(self.tact_cfg.universe)
        return symbols

    # ---- lifecycle ----

    def start(self) -> None:
        total_eq = self.portfolio_cfg.starting_equity
        active = [
            (name, cfg.allocation_pct)
            for name, cfg in [
                ("core", self.core_cfg),
                ("sentiment", self.sent_cfg),
                ("tactical", self.tact_cfg),
            ]
            if cfg.enabled
        ]
        active_str = (
            " · ".join(f"{name} ${total_eq * pct:,.0f} ({pct:.0%})" for name, pct in active)
            or "(none enabled)"
        )
        banner = (
            f"PHASE 8 PORTFOLIO · PAPER · port {self.app_cfg.ib.port} · "
            f"{active_str} · cycle={self.interval}s · dry_run={self.dry_run}"
        )
        self.storage.log_event("info", "portfolio", f"starting | {banner}")
        print("=" * min(len(banner), 130))
        print(banner)
        print("=" * min(len(banner), 130))

        session = read_session()
        if session is None or not session.get("cookie"):
            raise RuntimeError(
                "No CPGW session. Start `live.cpgw_keeper` first so the SRP login "
                "runs and writes logs/cpgw_session.json."
            )
        self.client = CPGWClient(cookie=session["cookie"], port=self.app_cfg.ib.port)
        if session.get("account_id"):
            self.client.account_id = session["account_id"]

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_: Any) -> None:
        print("\n[portfolio] stop signal received…")
        self.stopped = True

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.storage.log_event("info", "portfolio", "stopped")
        print("[portfolio] disconnected.")

    # ---- main loop ----

    def loop(self) -> None:
        while not self.stopped:
            cycle_start = time.time()
            try:
                self._cycle()
            except NotAuthenticatedError as e:
                self.storage.log_event(
                    "warn", "portfolio", f"session expired ({e}); cpgw_keeper should re-login"
                )
            except Exception as e:  # noqa: BLE001
                self.storage.log_event("error", "portfolio", f"cycle error: {e}")
                print(f"[portfolio] cycle error: {e}")
            if self.once:
                return
            elapsed = time.time() - cycle_start
            sleep = max(0.0, self.interval - elapsed)
            slept = 0.0
            while slept < sleep and not self.stopped:
                time.sleep(min(0.5, sleep - slept))
                slept += 0.5

    def _cycle(self) -> None:
        assert self.client is not None
        # Refresh cookie from the keeper's session file every cycle
        session = read_session()
        if session and session.get("cookie"):
            self.client.update_cookie(session["cookie"])

        cycle_id = uuid.uuid4().hex[:12]
        total_equity = self.portfolio_cfg.starting_equity
        ms = market_state()

        # Log market state once per cycle so the dashboard timeline shows it
        if ms.regime == MarketRegime.OPEN:
            mkt_note = f"OPEN ({ms.minutes_to_close}m to close, ET {ms.eastern_now:%H:%M %a})"
        elif ms.regime == MarketRegime.WEEKEND:
            mkt_note = (
                f"WEEKEND (next open in {ms.minutes_to_open // 60}h, ET {ms.eastern_now:%a %H:%M})"
            )
        elif ms.regime == MarketRegime.PRE_OPEN:
            mkt_note = f"PRE-OPEN ({ms.minutes_to_open}m to open, ET {ms.eastern_now:%H:%M %a})"
        else:
            mkt_note = f"AFTER-CLOSE (next open in {ms.minutes_to_open // 60}h, ET {ms.eastern_now:%H:%M %a})"

        # ----- Sentiment satellite -----
        # Runs even when market is closed: time-based exits keep firing, and the
        # plan can be logged even if execution waits for the next open.
        if self.satellite is not None:
            self._cycle_satellite(cycle_id, total_equity, ms)

        # ----- Core sleeve -----
        # Only on the rebalance day after close (Fri 16:00–23:59 ET by default)
        # OR if drift kicks in during market hours.
        if self.core is not None:
            self._cycle_core(cycle_id, total_equity, ms)

        if not (self.core or self.satellite):
            self.storage.log_event(
                "info", "portfolio", f"cycle={cycle_id} no sleeves enabled — {mkt_note}"
            )

    # ---- core sleeve ----

    def _cycle_core(self, cycle_id: str, total_equity: float, ms) -> None:
        assert self.client is not None and self.core is not None
        sleeve_equity = total_equity * self.core_cfg.allocation_pct

        # Market-hours gate: only act in the rebalance window (Fri after close)
        # OR if existing positions have drifted (which can happen any time
        # we have current prices, i.e. only during/after market hours).
        in_rebalance_window = is_core_rebalance_window(ms, self.core_cfg.rebalance_day_of_week)
        if not in_rebalance_window and ms.regime in (MarketRegime.WEEKEND, MarketRegime.PRE_OPEN):
            self.storage.log_event(
                "info",
                "core",
                f"cycle={cycle_id} skip ({ms.regime.value}, next open in "
                f"{(ms.minutes_to_open or 0) // 60}h)",
            )
            return

        last = self.storage.last_rebalance("core")
        last_ts = datetime.fromisoformat(last["ts"]) if last else None

        # Snapshot current holdings + latest prices first — needed for the should-evaluate check
        holdings = {r["symbol"]: int(r["shares"]) for r in self.storage.get_holdings("core")}
        latest_prices: dict[str, float] = {}
        for sym in set(self.symbols) | set(holdings):
            cfg = self.symbols.get(sym)
            if cfg is None:
                continue
            close = self._get_latest_close(sym, cfg)
            if close is not None:
                latest_prices[sym] = close

        should, why = self.core.should_evaluate(
            last_rebalance_ts=last_ts,
            current_holdings=holdings,
            prices=latest_prices,
            sleeve_equity=sleeve_equity,
        )
        if not should:
            self.storage.log_event("info", "core", f"cycle={cycle_id} skip ({why})")
            return

        # Pull full close histories for scoring (needs ~2y to compute 12m momentum + 200d MA)
        closes: dict[str, pd.Series] = {}
        for sym, cfg in self.symbols.items():
            series = self._get_close_series(sym, cfg)
            if series is not None and len(series) > 0:
                closes[sym] = series

        if "SPY" not in closes:
            self.storage.log_event(
                "error",
                "core",
                f"cycle={cycle_id}: no SPY history — can't run risk-off filter, bailing",
            )
            return

        macro = self.macro_fetcher.snapshot()
        self.storage.log_event(
            "info",
            "core",
            f"cycle={cycle_id} macro: vix={macro.vix} yc={macro.yield_curve} "
            f"dxy={macro.dxy} complete={macro.complete}",
        )

        result: CoreCycleResult = self.core.evaluate(
            closes=closes,
            macro=macro,
            current_holdings=holdings,
            sleeve_equity=sleeve_equity,
            reason=why,
        )

        # Log every decision so the dashboard shows the full ranking
        for dec in result.decisions:
            self.storage.log_target(
                cycle_id=cycle_id,
                sleeve="core",
                symbol=dec.symbol,
                prediction=None,
                confidence=dec.score.composite if dec.score else None,
                sentiment_score=None,
                close_price=dec.close_price,
                realized_vol=None,
                signal_weight=None,
                vol_scale=None,
                target_shares=dec.target_shares,
                position_value=dec.target_value,
                reason=(
                    (dec.score.reason + "; " if dec.score else "")
                    + f"target_w={dec.target_weight:.1%} delta={dec.delta_shares:+d} "
                    + dec.reason
                ),
            )

        self.storage.log_rebalance(
            sleeve="core",
            reason=why,
            equity_before=sleeve_equity,
            notes=(
                f"risk_off={result.risk_off} | "
                + ", ".join(
                    f"{d.symbol} {d.delta_shares:+d}" for d in result.decisions if d.delta_shares
                )
            ),
        )

        # Pretty-print
        flag = " ⚠ RISK-OFF" if result.risk_off else ""
        print(f"[cycle {cycle_id} core] EVAL — {why}{flag}")
        print("  top scores:")
        for s in result.scores[:6]:
            print(f"    {s.symbol:<5} composite={s.composite:+.3f}  {s.reason}")
        print("  plan:")
        for d in result.decisions:
            if d.delta_shares == 0:
                if d.current_shares > 0:
                    print(f"    {d.symbol}: hold {d.current_shares} ({d.target_weight:.1%})")
                continue
            action = "BUY " if d.delta_shares > 0 else "SELL"
            qty = abs(d.delta_shares)
            print(
                f"    {d.symbol}: {action} {qty} → {d.target_shares} sh "
                f"({d.target_weight:.1%}, ${d.target_value:,.0f})"
            )
            self._execute_core_leg(d, cycle_id)

    def _execute_core_leg(self, d: CoreDecision, cycle_id: str) -> None:
        if d.delta_shares == 0:
            return
        action = "BUY" if d.delta_shares > 0 else "SELL"
        qty = abs(d.delta_shares)
        entry = round(d.close_price * (1.0005 if action == "BUY" else 0.9995), 2)

        if self.dry_run:
            self.storage.log_order(
                sleeve="core",
                symbol=d.symbol,
                action=action,
                quantity=qty,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=None,
                tp_id=None,
                stop_id=None,
                dry_run=True,
                status="logged",
                note=f"rebalance cycle={cycle_id}",
            )
            self.storage.upsert_holding(
                sleeve="core",
                symbol=d.symbol,
                shares=float(d.target_shares),
                avg_cost=d.close_price,
            )
            return

        assert self.client is not None
        conid = self.client.conid_for_stock(d.symbol)
        order = self.client.build_limit_order(
            conid=conid,
            side=action,
            quantity=qty,
            price=entry,
            tif="DAY",
        )
        try:
            tickets = self.client.place_orders([order])
            ticket = tickets[0] if tickets else None
            order_id_str = ticket.order_id if ticket else None
            parent_int = int(order_id_str) if order_id_str and order_id_str.isdigit() else None
            self.storage.log_order(
                sleeve="core",
                symbol=d.symbol,
                action=action,
                quantity=qty,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=parent_int,
                tp_id=None,
                stop_id=None,
                dry_run=False,
                status="placed",
                note=f"rebalance cycle={cycle_id} cpgw_id={order_id_str}",
            )
            self.storage.upsert_holding(
                sleeve="core",
                symbol=d.symbol,
                shares=float(d.target_shares),
                avg_cost=d.close_price,
            )
        except Exception as e:  # noqa: BLE001
            self.storage.log_event("error", "core", f"order failed for {d.symbol}: {e}")
            self.storage.log_order(
                sleeve="core",
                symbol=d.symbol,
                action=action,
                quantity=qty,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=None,
                tp_id=None,
                stop_id=None,
                dry_run=False,
                status="failed",
                note=str(e),
            )

    # ---- sentiment satellite ----

    def _cycle_satellite(self, cycle_id: str, total_equity: float, ms) -> None:
        assert self.client is not None and self.satellite is not None
        sleeve_equity = total_equity * self.sent_cfg.allocation_pct
        allow_entries = should_run_entries(ms)
        allow_exits = should_run_exits(ms)

        # Pull current positions from holdings table
        positions_raw = self.storage.get_holdings("sentiment")
        positions: dict[str, Position] = {}
        for r in positions_raw:
            opened = datetime.fromisoformat(r["opened_at"])
            positions[r["symbol"]] = Position(
                symbol=r["symbol"],
                shares=int(r["shares"]),
                entry_price=float(r["avg_cost"]),
                opened_at=opened,
            )

        # Pull latest sentiment for each universe symbol (from SQLite, NOT API)
        sent_rows = self.storage.latest_sentiment_batch(
            self.sent_cfg.universe,
            max_age_sec=self.sentiment_cfg.staleness_sec,
        )

        # Pull current prices for held positions + every candidate symbol
        symbols_needed = set(self.sent_cfg.universe) | set(positions)
        inputs: dict[str, SentimentInput] = {}
        for sym in symbols_needed:
            cfg = self.symbols.get(sym)
            if cfg is None:
                continue
            price = self._get_latest_close(sym, cfg)
            if price is None:
                continue
            row = sent_rows.get(sym)
            inputs[sym] = SentimentInput(
                symbol=sym,
                price=price,
                sentiment_score=float(row["score"]) if row else None,
                article_count=int(row.get("article_count") or 0) if row else 0,
                sentiment_ts=datetime.fromisoformat(row["ts"]) if row else None,
            )

        result = self.satellite.evaluate(
            inputs=inputs,
            positions=positions,
            sleeve_equity=sleeve_equity,
            allow_entries=allow_entries,
            allow_exits=allow_exits,
        )

        # Log every decision (entries + exits + skips) so the dashboard tells the full story
        for e in result.entries:
            self.storage.log_target(
                cycle_id=cycle_id,
                sleeve="sentiment",
                symbol=e.symbol,
                prediction=None,
                confidence=None,
                sentiment_score=(inputs[e.symbol].sentiment_score if e.symbol in inputs else None),
                close_price=e.price,
                realized_vol=None,
                signal_weight=None,
                vol_scale=None,
                target_shares=e.shares,
                position_value=e.target_value,
                reason=e.reason,
            )
        for x in result.exits:
            self.storage.log_target(
                cycle_id=cycle_id,
                sleeve="sentiment",
                symbol=x.symbol,
                prediction=None,
                confidence=None,
                sentiment_score=(inputs[x.symbol].sentiment_score if x.symbol in inputs else None),
                close_price=x.price,
                realized_vol=None,
                signal_weight=None,
                vol_scale=None,
                target_shares=0,
                position_value=0,
                reason=f"EXIT: {x.reason}",
            )
        for sym, why in result.skipped:
            self.storage.log_event("info", "sentiment", f"cycle={cycle_id} {sym} skip: {why}")

        # Pretty-print
        if result.entries or result.exits:
            print(
                f"[cycle {cycle_id} sentiment] entries={len(result.entries)} "
                f"exits={len(result.exits)} skipped={len(result.skipped)} "
                f"(entries_allowed={allow_entries})"
            )
            for e in result.entries:
                print(f"  → ENTER {e.symbol} ×{e.shares} @ {e.price:.2f}  {e.reason}")
            for x in result.exits:
                print(f"  ← EXIT  {x.symbol} ×{x.shares} @ {x.price:.2f}  {x.reason}")
            self._execute_satellite_plan(result, cycle_id)

    def _execute_satellite_plan(self, result: SatelliteCycleResult, cycle_id: str) -> None:
        assert self.client is not None
        # Process exits first to free up cash + slots
        for x in result.exits:
            self._exec_satellite_leg(
                symbol=x.symbol,
                action="SELL",
                quantity=x.shares,
                price=x.price,
                note=f"satellite exit: {x.reason} cycle={cycle_id}",
                drop_holding=True,
            )
        for e in result.entries:
            self._exec_satellite_leg(
                symbol=e.symbol,
                action="BUY",
                quantity=e.shares,
                price=e.price,
                note=f"satellite entry: {e.reason} cycle={cycle_id}",
                drop_holding=False,
            )

    def _exec_satellite_leg(
        self,
        *,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        note: str,
        drop_holding: bool,
    ) -> None:
        assert self.client is not None
        entry = round(price * (1.0005 if action == "BUY" else 0.9995), 2)
        if self.dry_run:
            self.storage.log_order(
                sleeve="sentiment",
                symbol=symbol,
                action=action,
                quantity=quantity,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=None,
                tp_id=None,
                stop_id=None,
                dry_run=True,
                status="logged",
                note=note,
            )
            if drop_holding:
                self.storage.delete_holding(sleeve="sentiment", symbol=symbol)
            else:
                self.storage.upsert_holding(
                    sleeve="sentiment",
                    symbol=symbol,
                    shares=float(quantity),
                    avg_cost=price,
                )
            return
        try:
            conid = self.client.conid_for_stock(symbol)
            order = self.client.build_limit_order(
                conid=conid,
                side=action,
                quantity=quantity,
                price=entry,
                tif="DAY",
            )
            tickets = self.client.place_orders([order])
            ticket = tickets[0] if tickets else None
            order_id_str = ticket.order_id if ticket else None
            parent_int = int(order_id_str) if order_id_str and order_id_str.isdigit() else None
            self.storage.log_order(
                sleeve="sentiment",
                symbol=symbol,
                action=action,
                quantity=quantity,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=parent_int,
                tp_id=None,
                stop_id=None,
                dry_run=False,
                status="placed",
                note=note,
            )
            if drop_holding:
                self.storage.delete_holding(sleeve="sentiment", symbol=symbol)
            else:
                self.storage.upsert_holding(
                    sleeve="sentiment",
                    symbol=symbol,
                    shares=float(quantity),
                    avg_cost=price,
                )
        except Exception as e:  # noqa: BLE001
            self.storage.log_event("error", "sentiment", f"{symbol} {action} failed: {e}")
            self.storage.log_order(
                sleeve="sentiment",
                symbol=symbol,
                action=action,
                quantity=quantity,
                entry_price=entry,
                tp_price=None,
                stop_price=None,
                parent_id=None,
                tp_id=None,
                stop_id=None,
                dry_run=False,
                status="failed",
                note=str(e),
            )

    # ---- price fetching helpers ----

    def _get_latest_close(self, symbol: str, cfg: SymbolConfig) -> float | None:
        """One-bar lookup for drift checks. Returns None on any failure."""
        assert self.client is not None
        try:
            raw = self.client.get_historical_data(
                symbol,
                bar=cfg.bar_size,
                period=cfg.history_duration,
            )
        except Exception as e:  # noqa: BLE001
            self.storage.log_event(
                "warn",
                "core",
                f"{symbol}: latest-close fetch failed ({cfg.bar_size}/{cfg.history_duration}): {e}",
            )
            return None
        if raw is None or raw.empty:
            return None
        df = capitalize_columns(raw)
        return float(df["Close"].iloc[-1])

    def _get_close_series(self, symbol: str, cfg: SymbolConfig) -> pd.Series | None:
        """Full close history for scoring (momentum + trend)."""
        assert self.client is not None
        try:
            raw = self.client.get_historical_data(
                symbol,
                bar=cfg.bar_size,
                period=cfg.history_duration,
            )
        except Exception as e:  # noqa: BLE001
            self.storage.log_event(
                "warn",
                "core",
                f"{symbol}: close-series fetch failed: {e}",
            )
            return None
        if raw is None or raw.empty:
            return None
        df = capitalize_columns(raw)
        return df["Close"].astype(float)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-sleeve portfolio runner (Phase 8).")
    p.add_argument("--interval", type=int, default=None, help="Override cycle interval seconds")
    p.add_argument("--once", action="store_true", help="Run one cycle then exit (testing)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument(
        "--live", dest="dry_run", action="store_false", help="Send orders to the IB paper account"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = PortfolioRunner(
        dry_run=args.dry_run,
        once=args.once,
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
