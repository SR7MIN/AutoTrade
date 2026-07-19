# 5 分钟多指标背离反手策略

`multi-divergence-reversal-v1` 是参考 TradingView `Divergence for Many Indicators v4`
思路独立实现的工程候选策略。它使用项目统一的 `Candle`、无未来数据的确认 Pivot 和
目标仓位决策契约，不是 Pine Script 的逐行移植。

## 信号规则

- 仅接受 BTCUSDT 5 分钟已收盘 K 线。
- Pivot 左右确认周期均为 5，确认延迟约 25 分钟。
- 当前 Pivot 与最近最多 16 个同类确认 Pivot 比较，最多回看 100 根 K 线。
- 同时检测普通背离和隐藏背离。
- RSI、MACD、MACD Histogram、Stochastic、CCI、Momentum、OBV、VWMACD、CMF
  等权参与，不做相关性分组。
- 同一指标、同一方向在同一确认 K 线最多计一票。
- 空仓入场和持仓反手均要求至少两个不同指标同向背离。
- 多空同时达到阈值时保持当前状态；同方向信号不加仓。

每条决策保存完整 `DivergenceEvidence`：指标、普通/隐藏类型、方向、当前与历史 Pivot
时间、价格和指标值。`decision_id` 包含确认 K 线、目标仓位和排序后的证据，用于 Shadow
日志、命令队列和 SQLite 原子去重。

## 无重绘约束

策略不支持“不等待确认”模式。Pivot 必须等待右侧 5 根 K 线完成，信号时间记录在确认
K 线，而不是回写到 Pivot K 线。回测和 Shadow 最早在确认后的下一根 K 线开盘执行。
断线补回的数据只恢复状态，不追补历史交易。

价格和指标连接线之间不能被中间值穿越。每个指标只采用最近的有效历史 Pivot；如果
同一指标同时存在普通和隐藏证据，会保留两份证据，但方向计数仍为一票。

## 风险和保护

多头止损为当前看多 Pivot Low 减 `0.5 ATR(14)`；空头止损为当前看空 Pivot High 加
`0.5 ATR(14)`。止损距离必须位于 `0.5–3 ATR`，否则放弃决策。安全止盈为 3R，反向
背离可以在硬止损或安全止盈前触发两阶段反手。

## 目标仓位与反手

策略输出 `StrategyDecision`，目标仓位为 LONG 或 SHORT。反手命令持久化为
`STRATEGY_REVERSE`，阶段依次为：

```text
QUEUED -> CLOSING -> CLOSE_CONFIRMED -> ENTERING -> ENTRY_CONFIRMED
```

daemon 必须先用 reduce-only 平掉原仓，确认交易所实际仓位为零并清理旧保护单，然后
重新执行风险检查、开反向仓并创建新保护单。任何异常都会进入 `FAILED`、暂停入场，且
不会继续开反向仓。daemon 重启时会重新排队处于 RUNNING 的反手命令，并根据交易所
实际仓位幂等恢复。

## 配置与命令

默认实例在 `strategies.toml` 中命名为 `divergence-btc-5m`：

```powershell
autotrade replay-strategy --instance divergence-btc-5m `
  --database .autotrade/research.db --cooldown-bars 0

autotrade shadow --instance divergence-btc-5m `
  --database .autotrade/orders.db
```

Shadow 文件位于 `.autotrade/strategies/divergence-btc-5m/`。`submit-strategy` 会读取
最新已接受的目标仓位决策：ENTER 进入普通 `ENTRY_INTENT`，REVERSE 进入两阶段
`STRATEGY_REVERSE`。两者仍仅允许 Testnet、单一活动策略、人工显式确认和现有健康/
风险门禁。

## 当前基线回放

项目现有 `research.db` 包含 26,208 根 BTCUSDT/5m K 线。默认参数产生 709 条决策、
681 笔交易和 28 个入场缺口拒绝。计入 5 bps 手续费和 10 bps 滑点后，1000 USDT
基线余额下降到约 607.77 USDT，胜率约 16.45%，最大已实现回撤约 39.24%。

该结果证明无重绘信号、状态机和反手执行模型能够完整运行，但也明确表明默认参数没有
盈利证据，不得进入主网。下一阶段应分析普通/隐藏背离、指标组合、反手频率、手续费
占比和不同市场区间，而不是提高风险。
