# 工程验证策略

系统包含历史研究、离线回放、只读 Shadow Runner，以及严格限定在 Testnet 的显式
信号提交适配器。Shadow 默认不会写 daemon 命令队列；只有人工选中已接受信号并提供
双重执行确认时，才会创建会过期的 `EntryIntent`。

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

## Shadow Runner

Shadow 以 SQLite 只读模式打开行情库。首次运行只预热指标并把游标移动到最新已收盘
K 线，不输出历史信号；之后只为新增 K 线记录决策：

```powershell
autotrade shadow --strategy ema-atr-v1 --symbol BTCUSDT --interval 5m `
  --database .autotrade/orders.db
```

决策追加到 `.autotrade/shadow.jsonl`，游标、虚拟待入场、虚拟持仓和冷却状态原子保存
到 `.autotrade/shadow-state.json`。运行器会从全部历史 K 线确定性重放内部状态，因此
重启不依赖序列化 EMA/ATR 对象。Shadow 不调用 Binance 私有接口，不写交易数据库，
也不创建 `operator_commands`。

`--once` 只处理当前数据库快照后退出，适合验收和计划任务。持续模式默认每 5 秒轮询。
Shadow 周期必须与 daemon 实际保存的周期一致；当前 daemon 若只保存 `1m`，需在后续
部署窗口改为保存 `5m` 后再运行上述配置。

## Testnet 信号提交

先预览最近一条已接受的 Shadow 信号：

```powershell
autotrade submit-strategy --log .autotrade/shadow.jsonl
```

显式提交到正在运行的 Testnet daemon：

```powershell
autotrade submit-strategy --log .autotrade/shadow.jsonl `
  --signal-id SIGNAL_ID --execute --confirm-testnet I_UNDERSTAND
```

适配器仅批准 `ema-atr-v1`、`BTCUSDT`、不超过 1 USDT 风险和 3 倍杠杆。信号必须新鲜，
入口必须已人工解锁，用户流和对应行情流必须健康，本地不得已有活动意图或订单，且
daemon 必须持有写锁。重复信号不能再次提交。入队后，现有 `RiskGovernor`、交易所仓位
检查和规则校验仍会在真正执行前再次运行。主网无条件拒绝该适配器。
