# AutoTrade 策略接入前核心系统

这是一个 Testnet 优先的 Binance USDⓈ-M 永续合约执行与监督核心。系统提供策略接入前必须具备的账户控制、硬性风控、持久化状态、断线对账、行情数据契约和人工操作能力，并包含一个只用于离线工程验证的 EMA/ATR 策略。该策略不会接入 daemon 或发送订单。

主网默认锁定，只有同时设置 `BINANCE_ENV=mainnet` 和 `BINANCE_ALLOW_MAINNET=I_UNDERSTAND` 才能解锁。

## 安全特性

- 使用 `.autotrade/writer.lock` 强制单一交易写入进程。
- daemon 在线时，人工操作和入场请求都通过持久化命令队列执行。
- 入场意图 30 秒后过期，且不能访问 API 凭据。
- 交易 POST 请求不会盲目重试，未知结果按客户端订单号对账。
- 风险降低类请求保留本地限频优先级。
- 修改止损时先创建新保护单，再撤销旧保护单。
- 部分平仓会同步保护数量，持仓归零会清理两类订单。
- 开仓暂停状态可持久化，必须提供明确理由才能解锁。
- 用户数据流重连前始终执行 REST 账户、订单和成交对账。
- 只保存已收盘 K 线，断线后通过 REST 补齐缺口。

详细说明见 [架构说明](docs/architecture.md)、[运行手册](docs/runbook.md) 和 [策略接入前审核清单](docs/pre-strategy-checklist.md)。

## 安装

```powershell
cd C:\Users\22276\Desktop\AutoTrade
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

在 `.env` 中填写 Binance Futures Testnet 凭据。实际执行订单前必须逐项审核其中的风险上限。

## 只读检查

```powershell
autotrade health
autotrade quote --symbol BTCUSDT
autotrade account
autotrade snapshot
autotrade income
autotrade audit --limit 20
```

回补已收盘 K 线，不启动 daemon：

```powershell
autotrade backfill --symbol BTCUSDT --interval 1m
```

分页回补 UTC 历史范围到独立研究数据库：

```powershell
autotrade backfill-range --symbol BTCUSDT --interval 5m --start 2026-04-01 --end 2026-07-01 --database .autotrade/research.db
```

使用相同的已收盘 K 线契约离线回放工程验证策略：

```powershell
autotrade replay-strategy --strategy ema-atr-v1 --symbol BTCUSDT --interval 5m --database .autotrade/research.db
```

执行模型和限制见 [工程验证策略](docs/strategy-validation.md)。

只读 Shadow 运行和显式 Testnet 信号提交：

```powershell
autotrade shadow --strategy ema-atr-v1 --symbol BTCUSDT --interval 5m --database .autotrade/orders.db
autotrade submit-strategy --log .autotrade/shadow.jsonl
```

Shadow 不会自动下单。提交命令默认仍是预览，实际入队必须同时使用 `--execute` 和
`--confirm-testnet I_UNDERSTAND`，并通过全部健康、风险和重复信号门禁。

## 启动监督进程

```powershell
autotrade daemon --symbols BTCUSDT --interval 1m
```

daemon 持有交易写锁，负责用户数据流重连、REST 对账、保护单管理、人工命令处理和收盘 K 线存储。`watch` 仅用于只读诊断，不负责自动修复。

## 入场流程

先预览，止损和止盈必须位于当前标记价的正确一侧：

```powershell
autotrade preview --symbol BTCUSDT --side BUY --risk-usdt 1 --stop-price 63000 --take-profit 65000 --leverage 3
```

提交同样的不可变入场意图：

```powershell
autotrade bracket --symbol BTCUSDT --side BUY --risk-usdt 1 --stop-price 63000 --take-profit 65000 --leverage 3 --execute
```

daemon 运行时返回 `mode: queued`，由 daemon 执行。使用以下命令查看结果：

```powershell
autotrade commands --limit 10
```

## 持仓操作

所有账户变更默认只预览，只有添加 `--execute` 才会执行：

```powershell
autotrade close-position --symbol BTCUSDT
autotrade close-position --symbol BTCUSDT --quantity 0.001 --execute
autotrade replace-stop --symbol BTCUSDT --trigger-price 62500 --execute
autotrade replace-take-profit --symbol BTCUSDT --trigger-price 65500 --execute
autotrade protect-position --symbol BTCUSDT --stop-price 62500 --take-profit 65500 --execute
autotrade cancel-all --symbol BTCUSDT --execute
```

`cancel-all` 不会平仓；`close-position` 使用 `reduceOnly`，然后清理或调整保护单。

## 风控控制

```powershell
autotrade pause-entry --reason "计划维护"
autotrade resume-entry --reason "账户与交易所状态已审核"
```

只撤单的紧急停止：

```powershell
autotrade kill-switch --symbols BTCUSDT --reason "人工紧急停止" --execute
```

暂停开仓并平掉持仓：

```powershell
autotrade kill-switch --symbols BTCUSDT --close-positions --reason "人工紧急停止" --execute
```

kill-switch 会先持久化暂停开仓，再处理账户敞口。

## 对账

daemon 会自动对账。停止 daemon 后，也可以手动预览或执行一次完整 REST 对账：

```powershell
autotrade reconcile --symbols BTCUSDT
autotrade reconcile --symbols BTCUSDT --execute
```

当发现无法安全恢复止损时，系统会根据 `AUTOTRADE_UNPROTECTED_ACTION` 选择暂停开仓或平仓。

## 测试

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
```

## 生产边界

当前实现适合扩展 Testnet 故障测试和主网 Shadow 数据采集，不应直接用于无人值守主网交易。外部告警、服务管理器自动重启、地区和法律检查、API Key IP 白名单以及连续 72 小时 Testnet 监督运行仍属于部署验收工作。

## 本地验证

在项目虚拟环境中执行完整测试和编译检查：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q src tests
```

用户流断线夹具的单独运行方式：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_daemon.DaemonReconciliationTests.test_user_stream_fixture_reconciles_before_reconnect
```

外部监控使用 `scripts\watchdog.ps1`。将 webhook 写入本机 `.env` 后，在管理员 PowerShell 执行
`.\scripts\register-watchdog.ps1` 注册开机任务。Watchdog 只报警，不会自动下单或平仓。

不要直接使用未安装本项目的系统 Python；否则会出现
`ModuleNotFoundError: autotrade`，这不代表交易逻辑测试失败。
