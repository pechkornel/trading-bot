# 0002. Backtest by replaying the live core sleeve with our own loop

## Status

Accepted.

## Context

We wanted to know how the core sleeve would have behaved in the past. A backtest that
re-implements the rules is only as good as the copy: it can drift from the live code and
then says nothing about the bot. The core sleeve is small (10 ETFs, at most one decision a
day, weekly rebalance), and its inputs are daily bars and a handful of macro series.

## Decision

Add a `backtest/` package with its own day by day loop. Each day it calls the bot's own
`CoreSleeve.should_evaluate`, `CoreSleeve.evaluate` and `MacroFetcher.snapshot`. Only the
two data methods of the macro fetcher are overridden, to return cached series sliced as of
the decision date. The engine itself (fills, costs, metrics, benchmarks, ablations) is new
code, tested offline on tiny synthetic data with known answers.

Decisions are made at the close of day t and filled at the next close, with fractional
weights and a flat cost in bps per unit of turnover. Parameters are the live config, not
tuned. Backtest-only dependencies (yfinance, matplotlib) live in
`requirements-backtest.txt` and are not installed in Docker or CI.

## Consequences

- A change to the core sleeve or scoring code changes the backtest automatically.
- The backtest tests run offline in CI with `requirements-dev.txt` only.
- The results cover the core sleeve only. The sentiment satellite and the tactical sleeve
  are not tested.
- Execution is simplified (fractional weights, next close fills, no slippage, cash earns 0),
  so absolute numbers are indicative only. The deviations from live are listed in
  `docs/backtest/results.md`.
- We maintain a small engine ourselves instead of a framework.

## Alternatives

- **vectorbt, backtrader, zipline:** large abstractions. We would still have to express the
  sleeve rules in their API, which is a copy of the logic, and add heavy dependencies.
- **Vectorised re-implementation in pandas:** fast, but a second copy of the scoring and
  risk-off rules that can silently diverge from live.
- **Wait for live paper history:** too short to say anything about drawdowns or regimes.
