# 运维与事故处理手册

## 启动

1. 执行 `autotrade health`、`autotrade account` 和 `autotrade snapshot`。
2. 确认运行环境正确、没有无法解释的持仓，并检查写锁中的 PID。
3. 完成审核后执行 `autotrade resume-entry --reason "启动检查已完成"`。
4. 启动 `autotrade daemon --symbols BTCUSDT --interval 1m`。
5. 检查 `.autotrade/autotrade.jsonl` 和 `autotrade audit`。

在确认锁文件中的 PID 对应进程已经停止前，不要删除 `writer.lock`。

## 下单结果未知

1. 不要重新提交入场订单。
2. 执行 `autotrade account`、`autotrade snapshot` 和 `autotrade commands`。
3. 使用记录下来的客户端订单号检查 Binance Testnet 网页。
4. 在持仓和保护单一致前保持开仓暂停。

## 发现无保护持仓

1. 执行 `autotrade pause-entry --reason "发现无保护持仓"`。
2. 检查标记价和当前持仓。
3. 预览 `protect-position`，确认触发方向和数量后再执行。
4. 如果无法建立保护，执行带平仓参数的 kill-switch。

## WebSocket 中断

daemon 会指数退避重连，并在重连前执行 REST 对账。如果持续出现 `USER_STREAM_RECONNECT` 告警，必须保持开仓暂停。风险降低类 CLI 命令会排入 daemon 队列。

代码级断线夹具由 `tests/test_daemon.py` 中的
`ReconnectingStreamFixture` 提供。它先投递一条账户事件，再抛出模拟断线，
随后保持第二次连接。运行：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_daemon.DaemonReconciliationTests.test_user_stream_fixture_reconciles_before_reconnect
```

验收重点是两次 `startup_reconcile`、一次 `USER_STREAM_RECONNECT`、入口锁定，
以及重连后的用户流健康状态。停止整个 daemon 只能验证进程恢复，不能替代这个
用户流专用夹具。

## HTTP 418、429、503

- `418`：系统会自动锁定开仓，先调查 IP 封禁原因。
- `429`：停止普通请求，风险降低类请求保留本地优先级。
- `503 unknown`：完成客户端订单号对账前，绝不能手动重复入场。

## 紧急停止

时间允许时先预览。 `kill-switch --close-positions --execute` 会暂停开仓、执行 `reduceOnly` 平仓，并清理普通单和 Algo 单。必须再运行 `autotrade account` 验证结果；撤单本身不能证明持仓已经关闭。

## 有持仓时重启

使用所有受管品种启动 daemon。系统会恢复当日成交、查询缺失订单状态、在可能时从活动意图恢复保护单；无法安全恢复时按照未保护持仓策略处理。

## 外部 Watchdog

`scripts/watchdog.ps1` 每个周期读取 `snapshot` 和 `health`，监控 daemon 锁、用户流、市场流、入口状态、持仓保护和限频。它只发送告警，不会自动下单或平仓。

配置 `.env` 中的 `AUTOTRADE_ALERT_WEBHOOK` 和可选的
`AUTOTRADE_ALERT_WEBHOOK_FORMAT=feishu`，然后在 PowerShell 中执行（默认使用当前用户登录触发）：

```powershell
.\scripts\register-watchdog.ps1
```

管理员环境可以改为系统启动触发：

```powershell
.\scripts\register-watchdog.ps1 -AtStartup
```

如果 Windows 禁止创建计划任务，注册脚本会自动在当前用户的“启动”文件夹中
创建 `AutoTrade-Watchdog.cmd`，登录后以隐藏窗口运行。

注册前可只运行一轮检查（不会持续运行）：

```powershell
.\scripts\watchdog.ps1 -Once
```

检查任务：

```powershell
Get-ScheduledTask -TaskName AutoTrade-Watchdog
Get-Content .autotrade\watchdog.jsonl -Tail 20
```

验证时可停止 daemon；Watchdog 应发送 `WATCHDOG_ALERT`，daemon 恢复后发送
`WATCHDOG_RECOVERED`。确认账户为空仓后再结束测试。
