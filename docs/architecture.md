# 系统架构

## 模块边界

```text
已收盘行情 -> 策略 -> 不可变 StrategySignal -> 未来执行适配器 -> EntryIntent
                                                              |
                                                              v
人工命令队列 -> 硬性 RiskGovernor -> TradingService -> Binance REST
                    ^                    |
                    |                    v
             持久化控制状态 <- OrderJournal <- 用户数据流 / REST 对账
```

策略只能读取 `Candle` 并产生 `StrategySignal`。策略不能获得
`BinanceRestClient`、`Settings`、API Key、数据库连接或直接下单接口。
历史回放和未来实时运行器负责将通过运行门禁的信号转换为 `EntryIntent`。

## 组件职责

- `binance_rest.py`：认证、时间同步、幂等查询有限重试和限频预算。
- `rules.py`：实时交易所过滤器和 Decimal 价格/数量校验。
- `risk_control.py`：账户级硬性开仓门禁和持久化自动锁定。
- `trading.py`：持仓生命周期、保护单替换、平仓和执行结果恢复。
- `journal.py`：SQLite WAL 模式下的意图、订单、事件、成交、审计、控制、命令和 K 线。
- `daemon.py`：独占写入、命令处理、用户数据流恢复和账户对账。
- `candles.py`：不依赖交易或存储服务的纯收盘 K 线契约。
- `market_data.py`：行情去重、范围回补和 REST 断档补齐。
- `strategy/`：无交易依赖的指标、策略契约和 EMA/ATR 工程验证策略。
- `backtest.py`：下一根 K 线成交、保守同柱处理和成本模拟的离线回放。
- `observability.py`：结构化 JSON 日志、敏感字段脱敏和持久化告警。

## 恢复模型

每次用户数据流连接前，daemon 会读取账户、持仓、普通挂单、Algo 挂单、本地非终态订单和当日成交。缺失的本地订单状态按客户端 ID 查询。持仓归零后关闭本地意图并清理遗留订单。持仓存在时，保护单数量必须与当前持仓一致。

用户数据流中的 `ALGO_UPDATE` 会按 `caid` 关联本地保护单，并记录
`NEW`、`TRIGGERING`、`TRIGGERED`、`FINISHED` 和 `CANCELED` 状态。
`TRADE_LITE` 可以先建立轻量成交记录；随后到达的
`ORDER_TRADE_UPDATE` 会按交易 ID 幂等补齐手续费和已实现盈亏。

## 数据库

SQLite 使用 WAL 模式并支持原地建表迁移。核心表包括：

- `trade_intents`
- `orders`
- `order_events`
- `fills`
- `audit_events`
- `control_state`
- `account_snapshots`
- `operator_commands`
- `candles`

终态订单收到重复或过期事件时不能回退到早期状态。Algo 的 `FINISHED`
属于终态，但允许随后到达的实际执行 `FILLED` 完成本地成交状态。
