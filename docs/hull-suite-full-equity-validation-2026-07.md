# Hull Suite Full-Equity Baseline

## Frozen rules

- Strategy: `hull-suite-full-equity-v1`, version `1`
- Markets: `BTCUSDT/1d` and `ETHUSDT/1d`
- Hull: Pine v4 HMA(55), close source
- Direction option: `long` or `all`; configured value is `all`
- Position size: 100% of current equity on every entry and reversal
- Leverage in the research model: 1x notional
- Protective stop option: `protective_stop_enabled`; configured `false`
- Take-profit: none
- Exit and reversal: Hull direction flip, filled at the next daily open
- Same-direction entry calls are not repeated

The optional stop implementation is retained behind the configuration switch.
When enabled, it uses the farther of 2 ATR and the buffered 10-day structure
level, constrained to 1.5-3.5 ATR. It is not active in the results below.

The strategy registration is `researchOnly`. Shadow simulation and historical
replay are allowed, but the Testnet submission adapter rejects its signals and
decisions. This prevents an unprotected full-equity signal from reaching the
daemon.

## Data and protocol

- Database: `.autotrade/research-mainnet-daily.db`
- Binance USD-M Futures mainnet daily klines
- Range: `[2020-01-01, 2026-07-01)` UTC
- 2,373 closed candles per symbol
- No gaps or duplicate open times
- Initial balance: 1,000 USDT per independent symbol replay
- Fee: 5 bps per side
- Slippage: 10 bps per side
- Funding: not included

The replay is continuous across chronological splits. Trades are attributed to
the split containing their entry. One trade crosses each of the first two split
boundaries, so split returns are trade-attributed rather than exact boundary
mark-to-market returns.

## Full-period results

| Market | Trades | Final balance | Return | CAGR | Win rate | Realized max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BTCUSDT | 113 | 1,439.99 | +44.00% | 5.77% | 32.74% | 84.31% |
| ETHUSDT | 97 | 9,927.87 | +892.79% | 42.35% | 38.14% | 70.78% |

The final open trade is marked to the last data close by the backtester. It
contributes +279.39 USDT for BTC and +2,358.17 USDT for ETH.

Worst single-trade adverse price excursion was 26.58% for BTC and 44.28% for
ETH. The ETH maximum came from a short position. These excursions are not
liquidations at the modeled 1x notional, but they demonstrate the exposure
created by removing the stop.

## Chronological attribution

| Market | Development 2020-2023 | Validation 2024 | OOS 2025-2026-06 |
| --- | ---: | ---: | ---: |
| BTCUSDT | -10.60% | +11.67% | +44.24% |
| ETHUSDT | +371.39% | -22.05% | +170.19% |

The splits are not uniformly profitable. BTC fails the development period and
ETH fails validation, despite both ending positive over the full period.

## Direction attribution

| Market | Long contribution | Short contribution |
| --- | ---: | ---: |
| BTCUSDT | +2,851.61 USDT | -2,411.61 USDT |
| ETHUSDT | +7,779.60 USDT | +1,148.26 USDT |

The contributions are path-dependent because every trade compounds from the
current balance. BTC short trading is materially destructive over the complete
sample.

## Cost sensitivity

| Market | Zero cost | 5/10 bps baseline | 5/20 bps stress |
| --- | ---: | ---: | ---: |
| BTCUSDT | +103.55% | +44.00% | +14.18% |
| ETHUSDT | +1,234.02% | +892.79% | +714.13% |

BTC is highly sensitive to execution costs. ETH remains positive in the stress
case, but its result is dominated by compounding through a small number of large
trends.

## Interpretation

This variant satisfies the requested Pine-style full-equity behavior, but it is
not suitable for execution. Realized drawdown reaches 70-84%, funding is not
charged, liquidation and maintenance margin are not simulated, and daily
intratrade mark-to-market drawdown can exceed the reported realized drawdown.
The results are research evidence only, not a deployment gate.
