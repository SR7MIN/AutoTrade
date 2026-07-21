# Hull Suite Protected Baseline

> Historical result only. This protected fixed-risk baseline was superseded by
> `hull-suite-full-equity-v1`; it is retained so the two risk models are not
> confused.

## Implemented baseline

- Strategy: `hull-suite-protected-v1`, version `1`
- Markets: `BTCUSDT/1d` and `ETHUSDT/1d`
- Direction: `all` (long and short)
- Hull mode: HMA
- Hull length: `55`
- Source: daily close
- Trend rule: current HMA compared with HMA two completed daily bars ago
- ATR: Wilder ATR(14)
- Structure lookback: 10 daily candles
- Initial stop: the farther of 2 ATR and the 10-day structure stop, with a
  0.25 ATR structure buffer
- Allowed stop range: 1.5 to 3.5 ATR
- Take-profit: none
- Normal exit: Hull direction flip on the next daily open
- Risk: 1 USDT per accepted entry, leverage capped at 2x

The strategy does not re-enter after a protective stop while the Hull direction
is unchanged. A new entry requires a later Hull direction change.

The implementation is the safe executable variant. It is not a strict
TradingView equity-percent reproduction: the Pine source uses 100% equity,
zero fees, zero slippage, and no protective stop, while this project requires
risk-budget sizing and an exchange-side stop.

## Dataset

Database: `.autotrade/research-mainnet-daily.db`

- Public Binance USD-M Futures mainnet klines
- Range: `[2020-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`
- BTCUSDT: 2,373 candles, no duplicates or gaps
- ETHUSDT: 2,373 candles, no duplicates or gaps
- All candles closed

## Validation protocol

The complete history was replayed chronologically. The strategy state was not
reset at split boundaries. Entries execute at the next daily candle open.

| Split | UTC range | Role |
| --- | --- | --- |
| Development | 2020-01-01 to 2024-01-01 | Four-year development sample |
| Validation | 2024-01-01 to 2025-01-01 | Forward validation |
| OOS | 2025-01-01 to 2026-07-01 | Untouched forward sample |

Baseline costs are 5 bps fee per side and 10 bps slippage per side. PnL below
uses the project's fixed 1 USDT risk budget per accepted entry.

### BTCUSDT

| Split | Trades | Net PnL | Win rate | Long PnL | Short PnL |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 58 | +10.495 | 27.59% | +18.222 | -7.727 |
| Validation | 13 | +2.326 | 38.46% | +3.579 | -1.253 |
| OOS | 19 | +1.044 | 31.58% | -1.068 | +2.112 |
| Full period | 90 | +13.865 | 30.00% | +20.733 | -6.868 |

Full-period maximum drawdown was 1.50%. Average holding time was 20.4 daily
bars, average MFE was 1.31R, and average MAE was 0.59R.

### ETHUSDT

| Split | Trades | Net PnL | Win rate | Long PnL | Short PnL |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 47 | +5.876 | 31.91% | +13.619 | -7.744 |
| Validation | 13 | +1.084 | 23.08% | +1.035 | +0.049 |
| OOS | 17 | +11.710 | 41.18% | +7.156 | +4.554 |
| Full period | 77 | +18.669 | 32.47% | +21.810 | -3.141 |

Full-period maximum drawdown was 0.86%. Average holding time was 25.2 daily
bars, average MFE was 1.54R, and average MAE was 0.64R.

### Cost sensitivity

| Market | 0 fee / 0 slippage | 5 fee / 10 slippage | 5 fee / 20 slippage |
| --- | ---: | ---: | ---: |
| BTCUSDT | +17.702 | +13.865 | +11.479 |
| ETHUSDT | +20.927 | +18.669 | +17.228 |

## Limitations and next gate

The backtest does not yet charge historical funding payments. Since these are
perpetual futures held for weeks, the reported PnL is pre-funding and is not a
final production profitability claim. Before Testnet or mainnet promotion, add
funding-rate data and rerun the same frozen baseline without changing its
parameters.
