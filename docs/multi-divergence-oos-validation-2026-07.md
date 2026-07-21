# Multi-Divergence Version 4: Chronological Validation

Validation date: 2026-07-21

## Frozen configuration

- Strategy instance: `divergence-btc-5m`
- Implementation: `multi-divergence-reversal-v1`
- Strategy version: `4`
- Configuration file SHA-256: `B6CA86A7F77249B706CF8B5D62B93DF230F1E13991057623F1971D2F83711F41`
- Initial balance: 1,000 USDT
- Position risk: 1 USDT per accepted entry
- Cooldown: 0 bars
- No strategy parameter was changed after inspecting these results.

## Dataset

The validated dataset is `.autotrade/research-mainnet-12m.db`. It contains only
public Binance USD-M Futures mainnet klines fetched from `/fapi/v1/klines`.

- Symbol and interval: `BTCUSDT/5m`
- Half-open range: `[2025-07-01T00:00:00Z, 2026-07-01T00:00:00Z)`
- Closed candles: 105,120
- Expected candles: 105,120
- Duplicate open times: 0
- Missing five-minute intervals: 0
- Non-closed candles: 0
- Invalid candle durations: 0
- First open time: `2025-07-01T00:00:00Z`
- Last open time: `2026-06-30T23:55:00Z`

Do not use `.autotrade/research.db` for this validation. Its previously stored
2026-04 through 2026-06 candles differ from mainnet, and a subsequent backfill
retained those rows while adding mainnet history. It is therefore a mixed-source
database.

## Chronological protocol

The strategy was replayed once over the continuous 12-month timeline. Indicator,
setup, consumed-pivot, position, and reversal state flowed forward across split
boundaries. Results were then assigned to the split containing each trade's entry
time. No trade crossed a split boundary.

| Split | UTC range | Role |
| --- | --- | --- |
| Development | 2025-07-01 to 2026-02-01 | 7-month development sample |
| Validation | 2026-02-01 to 2026-04-01 | 2-month forward validation |
| OOS | 2026-04-01 to 2026-07-01 | 3-month forward evaluation |

The final three months are not a pristine holdout: the same calendar range was
examined during earlier strategy development using Testnet candles. Because that
prior dataset came from a different market-data source, this run is useful as a
chronological forward evaluation, but it must be described as quasi-OOS rather
than untouched OOS.

## Baseline result

Costs are 5 bps fee per side and 10 bps slippage per side.

| Split | Signals | Trades | Rejections | Net PnL | Win rate | Realized max drawdown | Fees |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Development | 79 | 79 | 0 | -37.3662 | 17.72% | 3.76% | 11.3092 |
| Validation | 58 | 58 | 0 | -34.1480 | 12.07% | 3.41% | 7.9587 |
| Quasi-OOS | 43 | 43 | 0 | -23.1879 | 16.28% | 2.32% | 6.0790 |
| Full period | 180 | 180 | 0 | -94.7021 | 15.56% | 9.49% | 25.3470 |

All monetary values are USDT. The split drawdowns restart each split at 1,000
USDT and use realized trade equity. The full-period final balance is 905.2979
USDT.

Exit breakdown under baseline costs:

- `STOP`: 84 trades, -84.0000 USDT net.
- `REVERSE`: 96 trades, -10.7021 USDT net.
- No `END_OF_DATA` close and no rejected decision.

## Cost sensitivity

| Fee per side | Slippage per side | Net PnL | Win rate | Max drawdown |
| ---: | ---: | ---: | ---: | ---: |
| 0 bps | 0 bps | -30.9205 | 36.67% | 4.61% |
| 5 bps | 0 bps | -63.0716 | 28.89% | 6.59% |
| 5 bps | 2 bps | -71.5709 | 26.11% | 7.27% |
| 5 bps | 5 bps | -81.8402 | 22.22% | 8.24% |
| 5 bps | 10 bps | -94.7021 | 15.56% | 9.49% |

At zero cost, `STOP` exits lost 84.0000 USDT and `REVERSE` exits made 53.0795
USDT. The combined result remains negative, so transaction costs are not the
primary cause of failure.

## Decision

Version 4 fails chronological validation. It loses in development, validation,
and quasi-OOS, and remains negative with zero fees and zero slippage. It must not
be promoted to mainnet or treated as having demonstrated a profitable edge.

Any next strategy revision must receive a new version and be evaluated on a new
untouched holdout. These results must not be used to tune Version 4 and then be
reported again as out-of-sample evidence.

## Reproduction

The research download is a public-data operation. The temporary environment
override selects the mainnet market-data endpoint; it does not submit orders.

```powershell
$env:BINANCE_ENV = "mainnet"
$env:BINANCE_ALLOW_MAINNET = "I_UNDERSTAND"
$env:PYTHONPATH = "src"

python -m autotrade.cli backfill-range `
  --symbol BTCUSDT --interval 5m `
  --start 2025-07-01 --end 2026-07-01 `
  --database .autotrade/research-mainnet-12m.db

python -m autotrade.cli replay-strategy `
  --instance divergence-btc-5m `
  --start 2025-07-01 --end 2026-07-01 `
  --database .autotrade/research-mainnet-12m.db `
  --fee-bps 5 --slippage-bps 10 --cooldown-bars 0
```
