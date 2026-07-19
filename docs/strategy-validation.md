# 工程验证策略

本阶段只实现历史数据研究和离线回放，不包含实时 Shadow Runner，也不会把策略信号
写入 daemon 命令队列。

## 验证策略

`ema-atr-v1` 使用 BTCUSDT 5 分钟已收盘 K 线：

- EMA(20) 上穿 EMA(50) 产生 `BUY` 信号，下穿产生 `SELL` 信号。
- Wilder ATR(14) 的 2 倍作为止损距离。
- 止盈距离为止损距离的 2 倍。
- 单笔风险为 1 USDT，杠杆为 3，保证金利用上限为 50%。
- 回放器只允许一个虚拟持仓，平仓后冷却 3 根 K 线。

策略包只依赖 `Candle`，输出不可变 `StrategySignal`。它不能访问配置、数据库、
Binance REST、API 凭据或交易执行服务。未来执行适配器负责把通过门禁的信号转换为
会过期的 `EntryIntent`。

## 历史数据

范围参数使用左闭右开区间 `[start, end)`。日期和无时区时间按 UTC 解释，也可以直接
传入 epoch 毫秒。研究数据默认保存到独立数据库：

```powershell
autotrade backfill-range --symbol BTCUSDT --interval 5m `
  --start 2026-04-01 --end 2026-07-01 `
  --database .autotrade/research.db
```

命令输出分页数、插入数、已有记录数、交易所重复数和缺口范围。daemon 在线时，CLI
拒绝向实时 `orders.db` 执行回补。

## 离线回放

```powershell
autotrade replay-strategy --strategy ema-atr-v1 `
  --symbol BTCUSDT --interval 5m `
  --database .autotrade/research.db
```

信号在当前 K 线收盘时产生，最早在下一根 K 线开盘成交。回放计入配置的滑点和双边
手续费，仓位数量会把预期止损滑点和手续费计入 1 USDT 风险预算；如果一根 K 线同时
触及止损和止盈，按止损优先处理。输出包含交易、拒绝原因、
净盈亏、胜率和最大已实现回撤。

该模型不模拟资金费率、强平、盘口深度、交易所价格和数量过滤器，也无法判断同一根
K 线内部的真实价格路径。它用于验证策略流水线的确定性和安全边界，不构成盈利证据。
