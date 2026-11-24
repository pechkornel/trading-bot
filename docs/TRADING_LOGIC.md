# Trading logic

How the bot decides what to do, as it is in `config/symbols.yaml` and the code today. Top down: portfolio split, cycle and market hours, then one section per sleeve, then risk controls, known gaps and where to change things.

This file describes what the code does, including behaviour that looks like a bug. Those cases are listed in section 8.

The bot is paper-trading only. Orders are sent only when a runner is started with `--live`.

---

## 1. Portfolio split

Total equity is the paper account's net liquidation value, read from the Client Portal Gateway once per cycle (`live/account_equity.py`). When that value is missing or invalid, total equity is the last accepted gateway reading if it is recent, and otherwise `portfolio.starting_equity` (100,000). Each sleeve gets `total equity * allocation_pct`. The figures below use 100,000.

The gateway value is rejected when:

- `portfolio.use_account_equity` is false. Then `starting_equity` is always used.
- The gateway does not answer, is not logged in, or the summary has no usable `netliquidation` amount.
- The value is not finite or not positive.
- The currency is present and differs from `portfolio.equity_currency` (USD). A missing currency is accepted.
- The value is outside `equity_min_ratio` to `equity_max_ratio` times `starting_equity` (0.25x to 2x, bounds inclusive, so 25,000 to 200,000). A fresh paper account of 1,000,000 is rejected for this reason.
- The value differs by more than `equity_max_step` (0.25, that is 25%) from the last accepted gateway reading, when that reading is younger than `equity_last_max_age_sec`. The first reading ever, or one after the last reading has aged out, is only range checked.

When the gateway value is rejected (except for `use_account_equity: false`), the cycle uses the last accepted gateway reading from the `equity` table if it is younger than `equity_last_max_age_sec` (86400, one day). Only when there is no such reading does it use `starting_equity`. This stops one failed read from resizing the sleeves and triggering a drift rebalance. The reused value is not written to the `equity` table again.

Dry runs read the account value too. Each cycle writes one `portfolio` event that says which value was used, for example `equity: net liquidation 123,456.78 USD (gateway)`, `equity: last net liquidation 60,000.00 (fallback: <reason>, from <timestamp>)` or `equity: starting_equity 100,000.00 (fallback: <reason>)`. Anything other than a fresh gateway reading is logged at `warn` level, except the deliberate `use_account_equity: false`, which is `info`. Gateway readings are also written to the `equity` table (shown at `/api/equity`). Fallbacks are not.

The `portfolio` equity keys are checked when the config loads: `use_account_equity` must be a YAML boolean (a quoted `"false"` is an error), the numeric keys must be numbers, and `equity_currency` must be a 3 letter code (stored in upper case).

| Sleeve | Key | Enabled | Allocation | Budget |
|---|---|---|---|---|
| Core | `sleeves.core` | yes | 0.70 | 70,000 |
| Sentiment satellite (research) | `sleeves.sentiment` | no | 0.0 | 0 |
| Tactical | `sleeves.tactical` | no | 0.0 | 0 |

The remaining 30% (30,000) is never traded.

```
Total equity  100,000  (net liquidation, else last reading, else starting_equity)
|
+-- core       70%  70,000  macro + trend + momentum, weekly or on drift
+-- sentiment   0%       0  research status, off (premium data)
+-- tactical    0%       0  disabled
+-- unallocated 30% 30,000  never traded
```

The sleeves keep separate budgets and separate rows in the `holdings` table. The same symbol (SPY is in both universes) can be held by core and by the satellite at the same time. Positions do not net out.

Holdings are written in dry-run mode too, so a dry run simulates positions in SQLite. In `--live` mode a holding is updated when the order is placed, not when it fills.

An older sleeve (`core/sleeves/long_term.py`, `LongTermSleeveConfig`) still exists in the code. The portfolio runner does not use it and the YAML has no block for it.

---

## 2. Cycle and market hours

`live/portfolio_runner.py` runs one cycle every `portfolio.cycle_interval_sec` (3600 seconds). Each cycle runs the satellite first, then the core sleeve. Most cycles do nothing. The runner reads the clock once per cycle and passes that same time to the market state, the satellite and the core sleeve.

`core/market_hours.py` sorts the current time into a regime, in US Eastern time. The schedule comes from the NYSE (`XNYS`) calendar of the `exchange_calendars` package. The rules ship in the package, so no network access is needed. US Eastern time is `zoneinfo` `America/New_York`, so DST changes are handled exactly.

| Regime | When |
|---|---|
| `OPEN` | NYSE session day, from the session open to the session close (09:30 to 16:00, or to 13:00 on an early close) |
| `PRE_OPEN` | NYSE session day, 00:00 to the session open |
| `AFTER_CLOSE` | NYSE session day, from the session close to 23:59 |
| `WEEKEND` | Saturday and Sunday |
| `HOLIDAY` | Monday to Friday with no NYSE session, for the whole Eastern day (no `PRE_OPEN` or `AFTER_CLOSE` on it) |

Early closes end `OPEN` at the real close, and `AFTER_CLOSE` starts there. `minutes_to_close` uses the real close. `minutes_to_open` is the real elapsed time to the next session open, so it is correct across DST weekends and holidays.

The calendar is built once per process and calendar year, from 2020-01-01 to 31 December of the current UTC year plus 2. A date outside that window raises an error, which the runner logs as `cycle error` and skips the cycle.

What runs in each regime:

- Satellite exits (`should_run_exits`) run in every regime.
- Satellite entries (`should_run_entries`) run in `OPEN` and `AFTER_CLOSE`.
- The core sleeve is skipped in `WEEKEND`, `HOLIDAY` and `PRE_OPEN`. It is evaluated in `OPEN` and `AFTER_CLOSE`.

The runner reads daily bars (`1 day`, `2 Y`) from the Client Portal Gateway for every symbol it needs. Prices are the last close in those bars.

---

## 3. Core sleeve scoring

The core sleeve ranks a universe of 10 ETFs (`sleeves.core.universe`) and holds the best few. The code is in `core/scoring.py`.

| Symbol | Bucket | What it is |
|---|---|---|
| SPY | `equity_us_large` | US large cap |
| IWM | `equity_us_small` | US small cap |
| EFA | `equity_intl_dev` | Developed international |
| VWO | `equity_intl_em` | Emerging markets |
| TLT | `bond_long` | 20+ year Treasuries |
| IEF | `bond_inter` | 7 to 10 year Treasuries |
| AGG | `bond_broad` | Aggregate bonds |
| GLD | `alt_gold` | Gold |
| DBC | `alt_commodity` | Diversified commodities |
| BIL | `cash` | 1 to 3 month T-bills |

Each asset gets three sub-scores in [-1, 1]. The composite is a weighted sum:

```
composite = 0.40 * momentum + 0.30 * macro + 0.30 * trend
```

The weights are `momentum_weight`, `macro_weight` and `trend_weight`. `CoreSleeve.__init__` raises an error if they do not sum to 1.0 (within 0.01).

### Momentum

The return over `momentum_lookback_days` (252 trading days) is ranked across the universe. The best asset gets +1, the worst gets -1, ties get the average rank. An asset with fewer than 253 closes gets 0. If fewer than two assets have a valid return, every asset gets 0. Momentum is relative, so adding or removing an asset changes every other asset's score.

### Trend

The distance of the last close from its `trend_ma_days` (200) day simple average, as a fraction of the average. A distance of plus or minus 10% maps to plus or minus 1, and the result is clipped. With fewer than 200 closes the score is 0.

### Macro

Macro scores come from simple rules per bucket. Each rule uses one input. The sub-score is the average of the inputs that are available, clipped to [-1, 1].

| Input | Signal | Effect by bucket |
|---|---|---|
| Yield curve (`T10Y2Y`) | `norm(spread, -1, 2)`: -1 at a spread of -1, +1 at a spread of +2 | Equity buckets and `alt_commodity` add it. `bond_long` adds the same signal on the negated spread. |
| VIX | `clip((vix - 18) / 12)` | Equity buckets subtract it. `bond_long`, `bond_inter`, `bond_broad`, `alt_gold` and `cash` add it. |
| Dollar (`DX-Y.NYB`) 60 day change | `clip(change * 10)` | `alt_commodity`, `alt_gold`, `equity_intl_em` and `equity_intl_dev` subtract it. `equity_us_large` adds it. |
| Credit (HYG / LQD ratio) 60 day change | `clip(change * 10)` | Equity buckets add it. Bond buckets subtract half of it. |

An input that is available but does not apply to a bucket still counts in the average, with a contribution of 0. This dilutes the score. If the macro data is incomplete and both VIX and the yield curve are missing, every macro score is 0.

Data sources are FRED CSV files (no key) and the Yahoo Finance chart endpoint. `core/macro.py` calls Yahoo directly with `urlopen`, not through the `yfinance` package. Results are cached in memory for `macro.cache_ttl_sec` (21600 seconds). Only the yield curve, VIX, dollar and credit inputs feed the score. The other configured series (`DFF`, `DGS10`, `DGS2`, `CPIAUCSL`, `UNRATE`) are not used by the scoring rules.

### Risk-off filter

The filter is on when both conditions hold:

1. SPY's last close is below its `risk_off_spy_ma_days` (200) day average.
2. VIX is above `risk_off_vix_threshold` (25.0).

If either input is missing, the filter is off. When it is on, the target is an equal split across `risk_off_universe` (TLT and IEF, 50% each) and the scores are ignored.

### Selection

Otherwise the target is an equal weight across the top `hold_top_n` (4) assets by composite score, 25% each.

---

## 4. Core sleeve triggers and orders

`CoreSleeve.should_evaluate` in `core/sleeves/core.py` decides if this cycle does a rebalance. It checks once per day first, then the drift trigger, then the calendar trigger. All dates and weekdays are US Eastern (`to_eastern` from `core/market_hours.py`).

### Once per day

The sleeve never evaluates twice on the same Eastern date, whatever the trigger. If the last `rebalances` row for `core` has today's Eastern date, `should_evaluate` returns `already rebalanced today`. This check runs before drift.

### Drift trigger

With N held symbols (shares above 0), each symbol's target weight is taken as 1/N. A symbol's weight is `shares * latest close / sleeve_equity`. If any held symbol is more than `drift_threshold` (0.08, that is 8 percentage points of sleeve equity) away from 1/N, the sleeve evaluates. With no holdings there is no drift, so the first allocation waits for the calendar trigger.

### Calendar trigger

The sleeve evaluates when the Eastern weekday equals `rebalance_day_of_week` (4, Friday). It fires on the first eligible cycle of that day, in `OPEN` or `AFTER_CLOSE` (the runner skips `PRE_OPEN`, `WEEKEND` and `HOLIDAY`). It does not wait for the 16:00 close. When that Friday is an NYSE holiday (for example Good Friday), the runner skips the day and the calendar rebalance waits for the next Friday. Drift can still trigger on other days. The reason string is `calendar: Fri`. On other days the reason is `not rebalance day (today=Thu, want=Fri)`.

### Re-entry cooldown

A symbol that core sold down to 0 shares is not bought back for `portfolio.reentry_cooldown_sec` (86400, 24 hours). The runner passes `last_exit_times("core")` to `CoreSleeve.evaluate`. A blocked buy back becomes a hold with 0 target shares and the reason `reentry cooldown: 23.0h left`. Its weight stays in cash and is not spread over the other targets. Top-ups of symbols already held and all sells are not blocked. A partial sell does not start the cooldown.

### What an evaluation does

1. Pull the full close history for every symbol and the macro snapshot. If SPY has no history, the cycle stops with an error log.
2. Score the universe and pick target weights (section 3).
3. For each symbol (current holdings plus targets), compute:

```
target_value  = sleeve_equity * target_weight
target_shares = floor(target_value / last_close)
delta_shares  = target_shares - current_shares
```

4. Every non-zero delta becomes one order: type `LMT`, time in force `DAY`, price `close * 1.0005` for a BUY and `close * 0.9995` for a SELL, rounded to cents. Legs go out in alphabetical symbol order. Sells are not sent before buys.
5. There is no stop-loss or take-profit on core positions.

Every evaluation writes one row to `rebalances` and one row per symbol to `targets`. Every order goes into the `orders` table with `sleeve='core'`. After an order, the holding is set to `target_shares`. A symbol sold down to 0 stays in `holdings` with 0 shares.

The cooldown is derived from the `orders` table, not from a new column. `Storage.last_exit_times(sleeve)` returns the latest SELL per symbol with status `logged` (dry run) or `placed` (live), so a dry run enforces the cooldown the same way. Failed orders do not count.

---

## 5. Sentiment satellite

Status: research, off, 0% allocation. Alpha Vantage `NEWS_SENTIMENT` is now a premium endpoint, so the sleeve is not part of the portfolio. `sleeves.sentiment.status` is `active` or `research`. A `research` sleeve must have `allocation_pct: 0`, and the config refuses to load otherwise. The sleeve is tradeable only when `enabled` is true, the status is `active` and the allocation is above 0. Otherwise the runner builds no satellite and loads none of its symbols. If simulated sentiment holdings are left in SQLite from earlier runs, the runner writes one `warn` event at start and leaves them alone (no orders, nothing deleted). The rest of this section describes the rules the sleeve follows when it is switched back on.

The satellite buys stocks and ETFs with strong positive news sentiment and holds them for a short time. The code is in `core/sleeves/sentiment_satellite.py`, `core/sentiment.py` and `live/sentiment_worker.py`.

Universe (`sleeves.sentiment.universe`): SPY, QQQ, AAPL, MSFT, NVDA, TSLA, AMZN, META, GOOGL.

### Data

`live/sentiment_worker.py` polls once per `sentiment_provider.poll_interval_sec` (3600 seconds) and writes rows to the `sentiment` table. The symbols come from `sleeves.sentiment.universe`, not from `sentiment_provider.symbols`. With `provider: alpha_vantage` it makes one Alpha Vantage `NEWS_SENTIMENT` call for all symbols, with `limit=100` articles published in the last 24 hours (`time_from`, from `AlphaVantageProvider.lookback_hours`).

Per ticker:

- `score` is the relevance-weighted mean of `ticker_sentiment_score`, multiplied by 2 and clipped to [-1, 1].
- `article_count` is the number of ticker mentions in the returned articles.
- A ticker with no mentions gets a score of 0 and a count of 0.

The `stub` provider returns a score of 0 and a count of 0 for every symbol, so the satellite never enters on it.

The runner reads the latest row per symbol from SQLite and never calls the API. A row older than `sentiment_provider.staleness_sec` (86400 seconds, 24 hours) counts as missing.

### Exits come first

A position exits when either of these holds:

- Its age is at least `hold_max_days` (10). Age is counted in NYSE trading days: the number of sessions whose close is after `opened_at` and at or before now (`trading_days_between`). The entry day counts once its close has passed, also for an intraday entry. An early-close day counts as a full day. Weekends and holidays do not count. The reason text is `hold timer: 10 trading days >= 10 max`.
- The latest close is at least `stop_loss_pct` (0.05, 5%) below the entry price.

The stop is checked by the bot each cycle. There is no stop order at the broker.

### Entries

Candidates are the universe symbols, sorted by sentiment score from highest to lowest. A candidate is skipped when:

- it is already held,
- there is no fresh sentiment,
- `abs(score)` is below `sentiment_threshold` (0.4), so a score of exactly 0.4 enters,
- `article_count` is below `min_article_count` (5),
- the score is negative and `longs_only` is true,
- it exited in this cycle (an exit is never re-entered within its own cycle, even with a cooldown of 0),
- this sleeve sold it less than `portfolio.reentry_cooldown_sec` (86400) ago (the reason reads `reentry cooldown: 23.0h left`),
- the price is 0 or missing, or the position rounds to 0 shares.

Each skip is written to the events log as `<symbol> skip: <reason>`. Reasons have no leading space, for example `skip: no fresh sentiment`.

At most `max_concurrent_positions` (4) positions are open at once, counted after the exits of this cycle.

Size per position:

```
shares = floor(sleeve_equity * position_pct_of_sleeve / price)
```

With `position_pct_of_sleeve` of 0.05 that is 1,000 per position, and at most 4,000 (20% of the sleeve) is invested at the same time.

### Orders

Same shape as core: `LMT`, `DAY`, price `close * 1.0005` for a BUY and `close * 0.9995` for a SELL. No bracket. Exits are sent first. A sell removes the holding row. A buy writes it with the last close as the cost (`avg_cost`), not the limit price.

---

## 6. Tactical sleeve (disabled)

`sleeves.tactical` has `enabled: false` and `allocation_pct: 0.0`. The portfolio runner does not run it, even when enabled. If the sleeve is enabled, the runner only adds the tactical symbols to its price fetch.

The code for it still exists and is described here for reference.

### Fusion (`core/signal_fusion.fuse`)

```
ta_signal       = +ta_confidence  if the prediction is 1 or 2
                = -ta_confidence  if the prediction is 0
combined_signal = ta_weight * ta_signal + sentiment_weight * sentiment_score
                  (0.70 and 0.30 in the YAML)
combined_conf   = clamp(combined_signal, 0, 1)    if bullish
                = clamp(-combined_signal, 0, 1)   if not bullish
```

If sentiment is missing, `combined_signal` is `ta_signal`. The two weights are renormalised by their sum.

| TA prediction | TA confidence | Sentiment | Combined confidence | Reading |
|---|---|---|---|---|
| 2 (up) | 0.85 | +0.40 | 0.715 | Bullish news adds to the call |
| 2 (up) | 0.85 | -0.80 | 0.355 | Bearish news damps the call |
| 2 (up) | 0.70 | missing | 0.70 | Technical signal only |
| 0 (down) | 0.80 | +0.90 | 0.29 | Magnitude of the bearish signal, not a long |

Sentiment never flips the direction of the technical call. The sizer only looks at long candidates, so a bearish call never gets a position.

### Sizing (`core/sizing.size_portfolio`)

Values from `sleeves.tactical`:

```
1) prediction not in {1, 2}, or combined_conf < confidence_floor (0.55): 0 shares
2) signal_weight = (combined_conf - floor) / (1 - floor)            # 0..1
3) vol_scale     = target_vol_per_bar (0.005) / realized_vol        # std of the last 20 bar returns
   vol_scale     = clamp(vol_scale, 0.1, 5.0)
4) raw_value     = sleeve_equity * vol_scale * signal_weight
   capped_value  = min(raw_value, sleeve_equity * max_per_symbol_pct)    # 0.40
5) shares        = floor(capped_value / close)
```

If the summed position values exceed `sleeve_equity * max_gross_pct` (1.0), all positions are scaled down by the same factor. If realized volatility is not available, the position is 0.

### Order shape and the single-symbol runner

`live/runner.py` is the only tactical path that runs today. It does not call `fuse` or `size_portfolio`. For one symbol it scores a model from the registry on the latest bar, uses `argmax` as the prediction and the top probability as the confidence, and enters when the confidence is at least `TradingParams.confidence_threshold` (0.5) and the prediction is 1 or 2. The size is the fixed `--quantity`. The entry is a bracket order:

```
parent      LMT BUY   price = close * 1.005
take-profit LMT SELL  price = close + rr_ratio * atr_multiplier * ATR
stop-loss   STP SELL  price = close - atr_multiplier * ATR
```

Defaults from `core/config.py` are `rr_ratio = 4.0` and `atr_multiplier = 2.0`. With a close of 500 and an ATR of 0.50 the stop is 499.00 and the take-profit is 504.00.

---

## 7. Risk controls

| Control | Where | What it does |
|---|---|---|
| Paper lock | `core/config.py`, `assert_paper_mode` | Refuses to start a runner unless `PAPER=true` and `IB_PORT=5000`. Raised before any broker connection. |
| Dry run by default | `live/portfolio_runner.py`, `live/runner.py` | Orders are only logged. `--live` is needed to send them, and it still goes to the paper account. |
| Risk-off filter | Core scoring | SPY below its 200 day average and VIX above 25 moves the core target to TLT and IEF. |
| Top N, equal weight | Core selection | At most 4 assets, 25% of the sleeve each (50% each in risk-off). |
| Once per day | Core trigger | At most one core evaluation per US Eastern date, for the calendar and drift triggers alike. |
| Drift threshold | Core trigger | Off-cycle rebalance only when a holding is more than 8 points from 1/N. |
| Re-entry cooldown | Both active sleeves | No buy back of a symbol within 24 hours of selling it to 0, per sleeve. Derived from the `orders` table. |
| Sentiment threshold | Satellite entry | `abs(score)` of at least 0.4. |
| Article minimum | Satellite entry | At least 5 ticker mentions. |
| Longs only | Satellite entry | Negative scores never open a position. |
| Max concurrent positions | Satellite entry | At most 4 open at once. |
| Fixed position size | Satellite entry | 5% of the sleeve per position. |
| Hold timer | Satellite exit | Exit when the position is 10 or more NYSE trading days old. |
| Bot-side stop | Satellite exit | Exit when the latest close is 5% or more below entry. Checked each cycle, not a broker order. |
| Limit orders only | Both active sleeves | `LMT` with `DAY` and a 0.05% offset. No market orders. |
| Missing data | Runner | A symbol with no price is dropped from the cycle. No SPY history stops the core evaluation. |

---

## 8. Known gaps

These are places where the code does not match the intent in the YAML, or where a feature is missing. This file documents the current behaviour.

- **The tactical sleeve is not wired into the portfolio runner.** `TacticalSleeve`, `fuse` and `size_portfolio` are not called by any runner. `live/runner.py` uses a fixed quantity and no fusion.

---

## 9. Where to change behaviour

Most settings are in `config/symbols.yaml`.

| You want to | Edit |
|---|---|
| Move money between sleeves | `sleeves.core.allocation_pct`, `sleeves.sentiment.allocation_pct`, `sleeves.tactical.allocation_pct` |
| Change the fallback budget and the anchor of the range check | `portfolio.starting_equity` |
| Size from a 1,000,000 paper account | Set `portfolio.starting_equity: 1000000` (otherwise 1,000,000 is more than 2x the default and is rejected) |
| Turn account equity sizing off | `portfolio.use_account_equity: false` (always use `starting_equity`) |
| Change the accepted account value range | `portfolio.equity_min_ratio` (0.25), `portfolio.equity_max_ratio` (2.0), as multiples of `starting_equity` |
| Change the largest accepted change between two readings | `portfolio.equity_max_step` (0.25) |
| Change how long the last reading can be reused after a failed read | `portfolio.equity_last_max_age_sec` (86400) |
| Change the expected account currency | `portfolio.equity_currency` (USD) |
| Change cycle frequency | `portfolio.cycle_interval_sec` |
| Change the core universe | `sleeves.core.universe`. Each symbol needs a `universe.<symbol>:` block and a bucket in `ASSET_BUCKET` in `core/scoring.py`. |
| Hold more or fewer core assets | `sleeves.core.hold_top_n` |
| Change core score weights | `sleeves.core.momentum_weight`, `macro_weight`, `trend_weight` (must sum to 1.0) |
| Change lookbacks | `sleeves.core.momentum_lookback_days`, `trend_ma_days` |
| Change the risk-off filter | `sleeves.core.risk_off_spy_ma_days`, `risk_off_vix_threshold`, `risk_off_universe` |
| Change core rebalance triggers | `sleeves.core.rebalance_day_of_week`, `drift_threshold` |
| Change the re-entry cooldown | `portfolio.reentry_cooldown_sec` |
| Change macro series or cache time | `macro.fred_series`, `macro.yf_tickers`, `macro.cache_ttl_sec` |
| Change the macro rules | `_macro_score` in `core/scoring.py` |
| Change satellite entry rules | `sleeves.sentiment.sentiment_threshold`, `min_article_count`, `max_concurrent_positions`, `longs_only` |
| Change satellite size | `sleeves.sentiment.position_pct_of_sleeve` |
| Change satellite exits | `sleeves.sentiment.hold_max_days`, `stop_loss_pct` |
| Change the satellite universe | `sleeves.sentiment.universe` (the worker polls the same list) |
| Change the sentiment provider or timing | `sentiment_provider.provider` (`stub` or `alpha_vantage`), `poll_interval_sec`, `staleness_sec` |
| Tune tactical sizing | `sleeves.tactical.ta_weight`, `sentiment_weight`, `target_vol_per_bar`, `vol_lookback`, `max_per_symbol_pct`, `max_gross_pct`, `confidence_floor` |
| Change bracket reward to risk | `TradingParams.rr_ratio` and `atr_multiplier` in `core/config.py`, or per symbol under `universe.<symbol>.trading:` |
| Add a sentiment provider | Implement `SentimentProvider` in `core/sentiment.py` and add it to `get_provider()` |
| Change order type or offsets | `build_limit_order` in `core/cpgw_client.py`, and the 1.0005 and 0.9995 factors in `live/portfolio_runner.py` |
