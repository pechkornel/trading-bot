# Trading logic

How the bot decides what to do. Top-down: portfolio split → per-sleeve strategy → per-symbol signal → order shape. No code in this file, just the rules.

If you want to change *what* the bot does, this is the doc to read. If you want to change *how* it does it, see the relevant module in `core/sleeves/` or `core/sizing.py`.

---

## 1. The portfolio is split into two sleeves

```
Total equity (default $100,000, configurable in symbols.yaml → portfolio.starting_equity)
│
├── 80% - long-term sleeve  ("long_term")
│   └── slow, mandate-driven, doesn't react to ticks
│       hold real diversification across asset classes
│
└── 20% - tactical sleeve   ("tactical")
    └── fast, signal-driven (TA × sentiment)
        modest size, tries to add alpha around the core
```

The two sleeves have **separate equity budgets** that never compete:

- A tactical loss does not shrink the long-term budget
- A long-term rebalance does not touch tactical positions
- Each sleeve enforces its own per-symbol and gross caps against its own equity

The same symbol can appear in both sleeves (SPY and GLD do, in the default config). Positions are tracked per-sleeve in the `holdings` table - they don't net out.

The split is set in `config/symbols.yaml` → `sleeves.long_term.allocation_pct` and `sleeves.tactical.allocation_pct`. Anything that sums to 1.0 is valid.

---

## 2. Long-term sleeve - All-Weather

The long-term sleeve runs Ray Dalio's "All-Weather" allocation: five assets weighted to perform reasonably in any macro regime (growth/recession × inflation/deflation).

| Symbol | Weight | What it is |
|---|---|---|
| **SPY** | 30% | S&P 500 - large-cap US equity |
| **TLT** | 40% | 20+ year Treasury bonds - duration |
| **IEF** | 15% | 7-10 year Treasury bonds - intermediate duration |
| **DBC** | 7.5% | Diversified commodities (DB Commodity Tracking Index) |
| **GLD** | 7.5% | Gold |

The weights come from the original All-Weather literature and are stable. They're meant to be set once and rebalanced periodically, not optimized.

### When the sleeve rebalances

Two independent triggers - whichever fires first:

1. **Calendar**: at least `rebalance_freq_days` (default **30**) since the last rebalance.
2. **Drift**: any single holding has moved more than `drift_threshold` (default **5%**) above or below its target weight. So if SPY's target is 30% and it now sits at 36% of the sleeve's equity, that's 6% drift → rebalance.

Between triggers, the sleeve **does nothing**. No churn. The bot looks at it every cycle (every 60s) but only acts when one of the triggers fires.

### What a rebalance does

For each symbol:

```
target_value  = sleeve_equity × target_weight
target_shares = floor(target_value / current_price)
delta_shares  = target_shares − current_shares
```

If `delta_shares > 0`, buy that many shares. If `< 0`, sell. If `= 0`, hold.

Orders are submitted as **single LMT orders, no bracket** - the long-term sleeve doesn't use stop-losses. The position is held until the next rebalance moves it. Stops would just churn around volatility events.

### What's recorded

Every rebalance writes a row to the `rebalances` table with the trigger reason ("calendar 30d" or "drift SPY 6.4%") and notes the delta orders. Every individual leg also goes into the `orders` table tagged with `sleeve='long_term'`.

---

## 3. Tactical sleeve - TA × sentiment, vol-target sized

The tactical sleeve is signal-driven. It runs on a smaller subset of the universe (`SPY, AAPL, QQQ, GLD` by default) and looks for short/mid-term opportunities every minute.

### Per-symbol decision flow

```
            ┌──────────────────────────────┐
            │  Fetch latest 5-min bars     │
            │  from CPGW (last 5 days)     │
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Compute indicators          │
            │  (RSI, MACD, ADX, ATR,       │
            │   BB, OBV, MA1/2/3, LRC,     │
            │   last-N-lows)               │
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Score XGBoost model for     │
            │  this symbol (from registry) │
            │  → prediction + confidence   │
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Read latest sentiment from  │
            │  SQLite (worker poll → cache)│
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Fuse: combined_confidence = │
            │  0.7 × ta_signal +           │
            │  0.3 × sentiment             │
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Size: vol-target ×          │
            │  signal-weight ×             │
            │  per-symbol cap ×            │
            │  gross cap                   │
            └────────────────┬─────────────┘
                             ▼
            ┌──────────────────────────────┐
            │  Place bracket order         │
            │  (entry / TP / stop)         │
            │  if target_shares > 0 AND    │
            │  symbol not on cooldown      │
            └──────────────────────────────┘
```

### Step 1: technical model

Each symbol has its own XGBoost classifier (or LSTM, but the live path is XGBoost). The default trains on:

- **Features**: 21 columns - Open/High/Low/Close, Volume, RSI, MACD, EMA, BB upper/lower, OBV, ATR, ADX, MA1/MA2/MA3, LRC middle/slope, last-50/10/100 lows
- **Target**: forward-looking percent return `SHIFT` bars ahead (default 5 bars = 25 minutes on 5-min data), classified as:
  - `2` = uptrend (return > +threshold)
  - `0` = downtrend (return < −threshold)
  - `1` = sideways (otherwise)
  - `threshold` is dynamically set to 1.5× the median absolute return over the last 5 bars
- **Loss**: multiclass log-loss with SMOTE rebalancing
- **Selection**: small grid search over learning rate, depth, regularization, subsample, min_child_weight - pick the highest test-set accuracy

The model's `predict_proba` on the latest bar gives `(p_down, p_flat, p_up)`. The bot uses `argmax` for the prediction and `max(p)` for the confidence.

### Step 2: sentiment

Independently of the model, a background worker (`live/sentiment_worker.py`) polls the Alpha Vantage News & Sentiment API every hour, batched across all tactical symbols. Per symbol it computes a weighted-by-relevance average sentiment in `[-1, 1]` and writes it to the `sentiment` table.

The tactical sleeve reads the *latest* sentiment row for each symbol when it makes a decision. If the latest row is older than `sentiment.staleness_sec` (default 7200s = 2 hours), it's treated as missing.

### Step 3: signal fusion

```
ta_signal       = +ta_confidence   if prediction is bullish (1 or 2)
                = −ta_confidence   if prediction is bearish (0)

combined_signal = ta_weight × ta_signal + sentiment_weight × sentiment_score
                  (defaults: 0.7 × ta_signal + 0.3 × sentiment_score)

combined_conf   = clamp(combined_signal, 0, 1)   if bullish
                = clamp(−combined_signal, 0, 1)  if bearish
```

Concrete examples:

| TA prediction | TA conf | Sentiment | Combined conf | Reading |
|---|---|---|---|---|
| 2 (up) | 0.85 | +0.40 | 0.71 | Bullish call, bullish news → boost |
| 2 (up) | 0.85 | −0.80 | 0.32 | Bullish call, bearish news → damped |
| 2 (up) | 0.70 | *(missing)* | 0.70 | TA only - sentiment can't penalize for being absent |
| 0 (down) | 0.80 | +0.90 | 0.29 | Bearish call, sentiment dampens it - but we still won't go long |

**Sentiment can never flip the direction** of the TA call. If TA says down, the bot stays flat. Sentiment is only used to scale conviction.

### Step 4: sizing

Once we have a `combined_conf` per symbol, the sizer (in `core/sizing.py`) decides how many shares to actually buy:

```
1) if prediction not bullish OR combined_conf < confidence_floor (default 0.55):
       target_shares = 0
       reason = "below floor" or "not bullish"

2) signal_weight = (combined_conf − floor) / (1 − floor)         # 0..1 ramp

3) realized_vol  = std of last 20 bars' percent returns
   vol_scale     = target_vol_per_bar / realized_vol             # quiet symbols → larger
   vol_scale     = clamp(vol_scale, 0.1, 5.0)

4) raw_value     = sleeve_equity × vol_scale × signal_weight
   capped_value  = min(raw_value, sleeve_equity × max_per_symbol_pct)   # default 40% cap

5) target_shares = floor(capped_value / current_price)
```

Then, after sizing every symbol, if the sum of intended position values exceeds `sleeve_equity × max_gross_pct` (default 100%), **everything scales down proportionally** so total gross fits the cap.

The output: a `target_shares` integer per symbol, ≥ 0.

### Step 5: order placement

A symbol with `target_shares > 0` triggers a **bracket order**:

```
parent  : LMT BUY  qty=target_shares  price=close × 1.005
take-profit : LMT SELL qty=target_shares  price=close + rr_ratio × atr_mult × ATR
stop-loss   : STP SELL qty=target_shares  price=close − atr_mult × ATR
```

Defaults: `rr_ratio=4`, `atr_mult=2`. So if ATR is $0.50 and the entry is $500:
- Stop = $500 − (2 × $0.50) = **$499.00** (a 0.2% loss)
- Take-profit = $500 + (4 × 2 × $0.50) = **$504.00** (a 0.8% win)

A 4:1 reward-to-risk ratio - the bot needs to be right less than 20% of the time to break even.

The take-profit and stop are *child* orders of the parent (via CPGW's `parentId = cOID`), so one cancels the other when triggered. Position management happens at IBKR's matching engine, not in our bot. If the bot crashes after the parent fills, the bracket still works.

### Cooldown

After a real (non-dry-run) entry on a symbol, the sleeve **won't open another position in that symbol** for `portfolio.reentry_cooldown_sec` (default 600s = 10 minutes). This prevents:

- Stacking 5 orders on SPY because the signal stayed strong for 5 minutes
- Re-entering immediately after a stop-out, before we know what happened

The cooldown is in-memory per runner process; restarting the runner resets it. Sized decisions are still logged during cooldown so you can see what would have happened.

---

## 4. Risk controls layered on top

These run regardless of strategy or signal:

| Control | Where | What it does |
|---|---|---|
| **Paper-mode lock** | `core/config.py:assert_paper_mode` | Refuses to start any runner if `PAPER ≠ true` OR `IB_PORT ≠ 5000`. IBKR also confirms `paper=true` in the SRP INIT response - three independent checks. |
| **Dry-run default** | Every runner CLI | `--dry-run` is on by default; you must explicitly pass `--live` to send orders. Even live, it's still the paper account. |
| **Confidence floor** | Tactical sizer | `combined_conf < 0.55` → no position. Filters out coin-flip signals. |
| **Per-symbol cap** | Tactical sizer | No more than `max_per_symbol_pct` (default 40%) of the sleeve's equity in any single name. |
| **Gross cap** | Tactical sizer | Sum of position values ≤ `max_gross_pct` (default 100%) of sleeve equity. Above that, everything scales down. |
| **Vol scale bounds** | Tactical sizer | `vol_scale` is clamped to `[0.1, 5.0]` so an ultra-quiet symbol can't get 50× leverage and an ultra-volatile one can't get rounded to zero. |
| **Re-entry cooldown** | Tactical runner | 10-minute lockout per symbol after every live entry. |
| **Drift threshold** | Long-term sleeve | Only rebalances if drift > 5%; otherwise leaves positions alone. |
| **Order brackets** | Tactical entries | Every BUY ships with a stop-loss - no naked longs. |
| **NaN guards** | Runner | If the latest bar has NaN in any required feature, the cycle is skipped (logged), not pushed to the model. |

None of these are "tweaks" - they're load-bearing. Removing any of them is a deliberate decision, not a refactor.

---

## 5. What the bot does NOT do (yet)

By design, none of the following are in scope right now. Each would be a real engineering project:

- **Shorting**. Tactical only opens longs on bullish signals. Bearish signals → stay flat. Adding shorts means new sizing math, new bracket shape, and margin handling.
- **Re-balance the tactical sleeve to a fixed weight scheme**. It's pure entry-driven; exits happen via the bracket. There's no concept of "trim SPY because it grew too much."
- **Cross-sleeve coordination**. The tactical sleeve doesn't know that long_term already owns 48 SPY. If both want SPY long, both will own it (separately).
- **Real account-equity tracking**. Sleeve budgets are static (the `starting_equity` constant), not live IBKR account values. Future work: pull `portfolio/{accountId}/summary` and update sleeve equity in place.
- **Live LSTM inference**. Training works; live scoring needs an `n_steps` rolling-window adapter that isn't built yet.
- **Backtesting in the new code**. The Jupyter notebook in `original_backup/combined_backtester/` is the old one; we haven't ported a clean backtester yet.
- **Slippage / commission modeling**. No T-cost analysis. Fine for paper trading; would matter before going live.

---

## 6. How to change behavior

Where to edit, by intent:

| You want to… | Edit |
|---|---|
| Change the long-term allocation | `config/symbols.yaml` → `sleeves.long_term.universe` |
| Move money between sleeves | `config/symbols.yaml` → `sleeves.{long_term,tactical}.allocation_pct` |
| Add a symbol to the tactical universe | `config/symbols.yaml` → `sleeves.tactical.universe` and add a `universe.<symbol>:` block with a `model:` |
| Change rebalance trigger | `config/symbols.yaml` → `sleeves.long_term.rebalance_freq_days` or `drift_threshold` |
| Make tactical more/less aggressive | `target_vol_per_bar`, `max_per_symbol_pct`, `confidence_floor` in the same file |
| Change the TA/sentiment weights | `ta_weight` + `sentiment_weight` (must sum to 1.0) |
| Change bracket reward:risk | `core/config.py` → `TradingParams.rr_ratio` and `atr_multiplier` |
| Change cycle frequency | `portfolio.cycle_interval_sec` |
| Add a new technical indicator | Add a function in `core/indicators.py` and reference it from a feature set in `core/features.py` |
| Add a new sentiment provider | Implement the `SentimentProvider` Protocol in `core/sentiment.py`, add it to `get_provider()` |
| Change order types (e.g. MKT instead of LMT) | `core/cpgw_client.py` → `place_bracket_order` and the `build_*_order` helpers |

Most behavioral changes happen in `config/symbols.yaml` - that's the right design. If you find yourself editing Python to change strategy parameters, that's a smell that the param should be added to the YAML.

---

## 7. End-of-day mental model

Every minute (default), the bot wakes up and asks two questions:

1. **Has the long-term portfolio drifted too far from target, or is it time for a calendar rebalance?**
   - If yes: compute new target shares per asset, buy/sell the deltas, single LMT orders.
   - If no: do nothing.

2. **For each tactical symbol, does the fused TA + sentiment signal cross the confidence floor?**
   - If yes (and the symbol isn't on cooldown): size with vol-target × signal-weight, place a bracket order, start the cooldown.
   - If no: log the decision (`target_shares=0`) and move on.

That's the whole loop. Everything else is plumbing.
