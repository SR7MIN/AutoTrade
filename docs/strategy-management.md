# 策略注册与实例管理

系统将策略分为三个层次：

1. **实现**：注册表中的 Python 策略类型，例如 `ema-atr-v1`。
2. **实例**：TOML 中带独立参数、品种和周期的命名配置，例如 `ema-default`。
3. **活动执行实例**：数据库控制状态中唯一允许进入 Testnet 队列的实例。

多个实例可以同时回放和 Shadow，但账户级执行权限始终只有一个。激活策略不会自动
执行 `resume-entry`，入口锁仍需单独人工审核。

## 配置实例

默认读取项目根目录的 `strategies.toml`：

```toml
[instances.ema-default]
implementation = "ema-atr-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.ema-default.parameters]
fast_period = 20
slow_period = 50
atr_period = 14
stop_atr_multiple = "2"
reward_risk = "2"
risk_usdt = "1"
leverage = 3
margin_utilization = "0.50"
```

环境变量 `AUTOTRADE_STRATEGY_CONFIG` 可以更换配置文件，
`AUTOTRADE_STRATEGY_STATE_DIR` 可以更换独立状态根目录。

查看注册实现、配置实例、路径和活动执行实例：

```powershell
autotrade strategies
```

## 回放与 Shadow

实例参数会同时用于回放和 Shadow：

```powershell
autotrade replay-strategy --instance ema-default `
  --database .autotrade/research.db

autotrade shadow --instance ema-default `
  --database .autotrade/orders.db
```

默认独立文件为：

```text
.autotrade/strategies/ema-default/state.json
.autotrade/strategies/ema-default/shadow.jsonl
.autotrade/strategies/ema-default/shadow.lock
```

不同实例可以同时运行 Shadow，因为它们拥有不同状态、日志和锁。

## 选择执行策略

显式激活一个实例：

```powershell
autotrade activate-strategy --instance ema-default `
  --reason "准备验证 ema-default Testnet 信号"
```

这只会设置 `active_strategy_instance`，不会打开 `entry_enabled`。需要停止所有策略执行
权限时：

```powershell
autotrade deactivate-strategy --reason "策略验证结束"
```

提交信号时必须指定同一实例：

```powershell
autotrade submit-strategy --instance ema-default
```

如果信号的实例、实现、版本、品种或周期与配置不一致，或者实例不是当前活动执行实例，
适配器会拒绝提交。

## 安装外部策略

外部 Python 包可以通过 entry point 注册策略实现：

```toml
[project.entry-points."autotrade.strategies"]
my-strategy-v1 = "my_package.strategy:registration"
```

目标对象必须是 `StrategyRegistration`，或返回该对象的无参数函数。注册名和版本不能与
现有实现冲突。策略包属于受信任代码，仍必须遵守纯 `Candle -> StrategySignal` 边界，
不得访问凭据、数据库或交易客户端。

## Testnet 生命周期实例

项目内置 `lifecycle-pulse-testnet-v1`，用于避免等待 EMA 交叉而拖慢订单生命周期验收：

```toml
[instances.lifecycle-pulse]
implementation = "lifecycle-pulse-testnet-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.lifecycle-pulse.parameters]
stop_bps = "10"
take_profit_bps = "15"
risk_usdt = "1"
leverage = 3
margin_utilization = "0.50"
cooldown_bars = 1
```

它在每根新收盘 K 线上发出一个方向脉冲：收盘价大于等于开盘价为 `BUY`，否则为
`SELL`。注册信息带有 `testnetOnly=true`，执行适配器在主网拒绝该实现。它仍然只能通过
`submit-strategy --execute --confirm-testnet I_UNDERSTAND` 人工提交，不存在自动提交路径。

## 多指标背离反手实例

`multi-divergence-reversal-v1` 使用扩展的 `StrategyDecision` 目标仓位契约。默认实例为
`divergence-btc-5m`，配置、无重绘规则、两阶段反手恢复和当前回放基线见
[5 分钟多指标背离反手策略](multi-divergence-strategy.md)。
