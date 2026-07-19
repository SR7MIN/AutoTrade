# 系统架构

## 模块边界

```text
已收盘行情 -> 策略 -> StrategySignal / StrategyDecision -> Testnet 执行适配器
                                                                |
                                                                v
                                                     EntryIntent / 两阶段反手
                                                                |
                                                                v
人工命令队列 -> 硬性 RiskGovernor -> TradingService -> Binance REST
                    ^                    |
                    |                    v
             持久化控制状态 <- OrderJournal <- 用户数据流 / REST 对账
```

策略只能读取 `Candle` 并产生 `StrategySignal` 或目标仓位 `StrategyDecision`。策略不能获得
`BinanceRestClient`、`Settings`、API Key、数据库连接或直接下单接口。
历史回放和实时运行器负责将通过门禁的输出转换为 `EntryIntent` 或持久化两阶段反手命令。

## 组件职责

- `binance_rest.py`：认证、时间同步、幂等查询有限重试和限频预算。
- `rules.py`：实时交易所过滤器和 Decimal 价格/数量校验。
- `risk_control.py`：账户级硬性开仓门禁和持久化自动锁定。
- `trading.py`：持仓生命周期、保护单替换、平仓和执行结果恢复。
- `journal.py`：SQLite WAL 模式下的意图、订单、事件、成交、审计、控制、命令和 K 线。
- `daemon.py`：独占写入、命令处理、用户数据流恢复和账户对账。
- `candles.py`：不依赖交易或存储服务的纯收盘 K 线契约。
- `market_data.py`：行情去重、范围回补和 REST 断档补齐。
- `strategy/`：无交易依赖的指标、策略契约、背离检测、EMA/ATR 策略和验证夹具。
- `strategy/registry.py`：内置实现注册和 `autotrade.strategies` 插件发现。
- `strategy_manager.py`：TOML 实例配置、独立路径和单一执行实例管理。
- `backtest.py`：下一根 K 线成交、保守同柱处理和成本模拟的离线回放。
- `shadow.py`：只读实时决策、确定性状态重放和独立虚拟持仓状态。
- `strategy_adapter.py`：Testnet 限定的信号门禁和 `EntryIntent` 队列适配。

多个策略实例可以同时回放和 Shadow。活动执行实例保存在
`control_state.active_strategy_instance`，任何时刻只有与该值完全匹配的信号可以进入
Testnet 队列；该控制与 `entry_enabled` 相互独立。

目标仓位反手不是一笔放大的反向市价单。daemon 必须先 reduce-only 平仓并确认空仓，
再重新执行风险检查和反向入场。`strategy_reversals` 保存阶段状态；daemon 重启后以交易所
实际仓位为事实来源进行幂等恢复。
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
- `strategy_submissions`
- `strategy_reversals`
- `candles`

终态订单收到重复或过期事件时不能回退到早期状态。Algo 的 `FINISHED`
属于终态，但允许随后到达的实际执行 `FILLED` 完成本地成交状态。
