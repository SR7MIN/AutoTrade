# 5 分钟多指标背离反手策略

`multi-divergence-reversal-v1` version 4 严格移植 TradingView Pine v4 指标
`Divergence for Many Indicators v4` 的指标、Pivot、四类背离和计数流程，并在其输出之上
保留项目已有的仓位状态机、ATR 保护和 Testnet 执行层。

背离移植代码位于 `src/autotrade/strategy/divergence.py`，遵循 Mozilla Public License
2.0；原指标版权归 LonesomeTheBlue。绘图、标签、颜色和 alertcondition 不参与交易计算，
因此没有移植。外部指标入口也没有启用，因为本项目只有 OHLCV 数据。

## 与 Pine 脚本对齐的计算

当前内置指标与原脚本一致：

- `rsi(close, 14)`
- `macd(close, 12, 26, 9)` 的 MACD 与 Histogram
- `sma(stoch(close, high, low, 14), 3)`
- `cci(hlc3, 10)`
- `mom(close, 10)`
- Pine OBV
- `vwma(close, 12) - vwma(close, 26)`
- 原脚本表达式 `sma(Cmfv, 21) / sma(volume, 21)`
- `mfi(close, 14)`

Pine EMA 从第一个非 `na` 值初始化，SMA 忽略 `na`，MFI 使用 `close` 而不是典型价。
这些初始化和缺失值规则也已移植，不能用通用技术指标库的默认实现替代。

当前项目实例使用以下 Pine 输入等价配置：

```text
prd = 5
source = Close
searchdiv = Regular/Hidden
showlimit = 1
maxpp = 10
maxbars = 100
dontconfirm = false
```

当前工作区配置使用 Pine 默认的 `Close` Pivot Source，且 `require_confirmation = true`
对应 Pine 的 `dontconfirm = false`。当前端点固定为上一根已收盘 K 线，避免即时模式的
盘中重绘。检测器仍同时支持 `close` 和 `high_low`。

## Pine 信号时序

每根新 K 线到来时，移植代码按原脚本顺序：

1. 用 `pivothigh/pivotlow(prd, prd)` 把本根刚确认的价格 Pivot 放入历史数组。
2. `dontconfirm = false` 时以偏移 1（上一根已收盘 K 线）作为端点；`true` 时以当前 K 线
   作为端点。
3. 在最近 `maxpp` 个已确认同类 Pivot 中按新到旧查找，距离必须大于 5 且不超过
   `maxbars`。
4. 分别运行正向普通、负向普通、正向隐藏、负向隐藏四个 Pine 函数。
5. 指标线与 `close` 连线均不得被中间值穿越；即使 Pivot Source 为 High/Low，原脚本的
   连线穿越检查仍使用 `close`，本实现保持这一细节。
6. `showlimit` 对全部指标和四种背离槽位的总数生效。

防重绘模式下，信号在当前 K 线收盘时可知，证据端点是上一根 K 线；回测和 Shadow 最早
在下一根开盘执行。`dontconfirm = true` 则严格复现原脚本的即时模式，但盘中结果可能消失，
不能称为无重绘信号。

## 交易规则

- 只接受所配置品种的 5 分钟已收盘 K 线。
- 普通背离和隐藏背离同时参与。
- 不做指标相关性分组。
- Pine 指标层仍保留全部“指标 × 背离类型槽位”，但交易门槛按不同指标计数。同一指标
  同时出现普通和隐藏背离只计一个指标。
- 空仓候选要求至少三个不同指标，并且至少来自两个指标组：振荡指标、趋势动量、成交量。
- 空仓只允许顺最后一根完整 1 小时 K 线的 EMA50/EMA200 趋势方向建立候选。
- 背离不会立即入场，而是等待未来 6 根 K 线内收盘价突破最近 3 根 K 线的结构边界。
- 突破前价格越过 Setup 极值 `0.25 ATR` 时失效；同一历史 Pivot 不会重新建立 Setup。
- 持仓反手不受 1 小时趋势和结构突破过滤：两个不同指标出现反向背离即执行平仓后反手。
- 一次有效决策会消费其证据涉及的全部历史 Pivot；同一方向、同一历史 Pivot 后续即使
  再次形成背离，也不能触发第二笔交易。
- 多空同时达到阈值时保持当前状态；同方向信号不加仓。

每条决策保存完整 `DivergenceEvidence`：指标、普通/隐藏类型、方向、当前与历史端点时间、
价格和指标值。version 4 增加小时趋势与待突破 Setup 状态，避免与 version 1/2/3 的决策
ID 和 Shadow 状态混用。

## 风险和保护

ATR 风险层不是 TradingView 指标的一部分。多头止损为看多证据价格减 `0.5 ATR(14)`；
空头止损为看空证据价格加 `0.5 ATR(14)`。止损距离必须位于 `0.5–3 ATR`，否则拒绝
决策。空仓入场还要求止损距离至少为价格的 60 bps，避免预期交易成本相对单笔风险过高。
version 4 不设置固定止盈价，也不会创建 `TAKE_PROFIT_MARKET` 保护单：

- 持有 LONG 时，至少两个不同指标出现看空背离，先平多，再开 SHORT。
- 持有 SHORT 时，至少两个不同指标出现看多背离，先平空，再开 LONG。
- 未出现合格反向背离时保持仓位，直至硬止损触发。

反手命令仍按以下阶段持久化：

```text
QUEUED -> CLOSING -> CLOSE_CONFIRMED -> ENTERING -> ENTRY_CONFIRMED
```

daemon 必须先 reduce-only 平掉原仓，确认交易所仓位为零并清理旧保护单，再重新执行
风险检查、开反向仓和创建新保护单。该执行层没有因 Pine 移植而绕过任何 Testnet、人工
确认、健康检查或风险门禁。

## 配置与命令

默认实例为 `divergence-btc-5m`：

```powershell
autotrade replay-strategy --instance divergence-btc-5m `
  --database .autotrade/research.db --cooldown-bars 0

autotrade shadow --instance divergence-btc-5m `
  --database .autotrade/orders.db
```

Shadow 文件位于 `.autotrade/strategies/divergence-btc-5m/`。已有 version 1/2/3 Shadow
状态与 version 4 不兼容；启用新版前应重新初始化该实例的 Shadow 状态并重新验证。

## Version 4 回放基线

按当前 `Close + 等待确认 + 1h EMA 趋势 + 结构突破 + 60 bps 成本过滤` 配置，现有
`research.db` 的 26,208 根 BTCUSDT/5m K 线产生 37 条决策、36 笔交易和 1 个实际开盘
止损距离拒绝。计入每边
5 bps 手续费和 10 bps 滑点后：

- 1000 USDT 降至约 994.7722 USDT，净损失约 5.228 USDT。
- 胜率约 30.56%，最大已实现回撤约 0.99%。
- STOP 11 笔，REVERSE 25 笔；没有固定止盈退出。
- 总手续费约 4.43 USDT。

成本敏感性结果：零成本为 `+19.06` USDT；5 bps 手续费且零滑点为 `+6.59`；滑点为
2 bps 时 `+3.37`，5 bps 时 `-0.47`，10 bps 时 `-5.23`。这表明新版已经从明显负毛收益
改善为正毛收益，但优势很薄，对成交成本敏感。

当前样本只有 40 笔交易，远不足以证明盈利能力。version 4 只能作为候选工程基线，不得
进入主网；必须扩充到至少 12 个月数据并进行时间顺序样本外验证。
