# Multi-Divergence Version 5

Version 5 is an experimental revision of `multi-divergence-reversal-v1`. Version
4 remains frozen for comparison; Version 5 must not be mixed with its state or
validation report.

## Signal model

- Regular and hidden divergences are evaluated separately. Evidence from one
  type cannot satisfy the threshold for the other type.
- Indicators are counted uniquely, and each indicator group contributes at most
  its configured weight once. The default weights are oscillator `0.35`,
  trend/momentum `0.35`, and volume `0.30`.
- Entries require at least three indicators, two groups, a score of `0.65`, and
  at least one non-oscillator group.
- Hidden divergence requires the higher-timeframe trend direction and ADX at or
  above `18`.
- Regular divergence is allowed in a neutral or weakening trend and is rejected
  when ADX exceeds `30` against the target direction.
- Relative volume must be at least `0.8` of its 20-bar average.
- Expected movement (`2 * ATR`) must be at least `1.5` times the configured
  round-trip cost estimate (`2 * 15 bps`).
- All filters use completed candles only.

## Position state machine

An opposite divergence meeting the exit threshold (`2` unique indicators and a
`0.65` group score) produces an `EXIT` decision. It closes the current position
on the next candle open. It does not open the opposite position in the same
operation.

After an opposite-divergence exit, the target direction is watched for up to six
bars. A new position is opened only after the complete Version 5 entry rules and
structure confirmation pass. This is the confirmed-reversal policy.

The strategy can also emit protective exits:

- break-even exit after a configurable favorable `R` move and a close back
  through entry;
- ATR trailing exit after the configured favorable `R` threshold;
- time exit when the maximum holding bars elapse without minimum favorable
  progress.

These are software decisions evaluated on closed candles. Exchange-side stop
protection remains the primary emergency protection; a live `EXIT` decision is
submitted through the risk-reducing `STRATEGY_EXIT` daemon command.

## Diagnostics

`BacktestTrade` now records holding bars, maximum favorable excursion (MFE),
maximum adverse excursion (MAE), and MFE/MAE in initial-risk (`R`) units.
`BacktestResult` also reports average holding time and average MFE/MAE.

## Initial verification

On the clean Binance mainnet dataset
`.autotrade/research-mainnet-12m.db` (`BTCUSDT/5m`, 2025-07-01 through
2026-07-01), with 5 bps fee, 10 bps slippage, and zero cooldown:

- 31 strategy decisions;
- 17 completed trades;
- net PnL `-6.43 USDT`;
- win rate `17.65%`;
- maximum drawdown `0.68%`.

The chronological split remains negative:

| Split | Trades | Net PnL | Win rate |
| --- | ---: | ---: | ---: |
| Development | 6 | -1.10 | 33.33% |
| Validation | 8 | -4.41 | 0.00% |
| Quasi-OOS | 3 | -0.92 | 33.33% |

At zero fee and zero slippage, Version 5 is still negative at `-2.63 USDT`.
The sample is too small for a profitability claim. The revision reduces
turnover and drawdown, but it has not yet demonstrated a positive edge.
