# 本地运行核验及盘前调度修复

检查基线：本地提交 `9984f04`；本轮不拉取、不推送 GitHub，不覆盖 DeepSeek 已有提交。以下修复保留在本地工作区。

## 当前运行状态（北京时间 2026-11-05 23:06 左右）

- 4200 网页与 worker 正在运行，worker/watchdog 心跳新鲜，账户快照约 3 秒内更新，无账户连接错误、无冻结。23:00 已结束主动交易窗口，显示 CLOSED 是正常时段状态，不代表崩溃。
- DeepSeek 当日 22:16 全池扫描、22:17 盘前计划成功；23:06 另做了一次认证只读 models 请求，成功返回 deepseek-flash/deepseek-v4-pro。本轮未触发新推理/研究，没有用测试结果授权订单；不能将“API 通”说成“AI 此刻持续思考”。
- Alpaca 实时账户/行情接口可用。只读 IEX 报价抽查：RDDT 买卖价 91.85/101.07，价差约 10.038%，确实超过当前 1% 阈值；RIOT 抽查为 13.71/13.74，约 0.219%。此前循环中的价差错误来自数据校验，并非所有接口断线；没有放宽校验。
- 实时券商历史显示今日 AAPL 1 股已 filled，属于此前 DeepSeek 操作留下的 Paper 测试，本轮未发单。该事实不证明自主 AI 交易、DAY 下午成交或次日恢复全部验证完成。
- 检查时 `autopilot=true`、`paused=false`。本轮没有改变真实账户控制状态、重启当前 worker 或发送/撤销订单。正在运行的进程仍加载修复前代码；新代码将在下次 supervisor 盘前重新启动 worker 时加载。

## 时序问题与修复

原 `ready_at = market_open + timedelta(minutes=2)` 实际为 **09:32 ET**，不是 README 写的 09:02。旧盘前分析本身没有检查暂停，所以不能断言延迟恢复必然导致错过钩子；但过晚健康检查、钩子重试很快耗尽且无独立盘前补执行，确实使自动化不可靠。

现在流程：

1. 08:55 ET 校验 autopilot、冻结、未知订单和配置状态。保留人工暂停的禁用语义。
2. 暂停并停止已有 worker 的 Windows 父子进程树，再启动 worker，确保明天使用新代码；无法停止则拒绝重复启动。此操作只在未来盘前执行，本次没有实际运行启动/停止函数。
3. 初始化立即写 worker 心跳；独立 watchdog 必须有本次启动之后的新鲜成功对账，无连续失败。检查通过即解除暂停，不再固定等到 09:32。若迟至 09:30 仍未通过，保留暂停。
4. 盘前钩子与后台每 5 秒检查共用 `Engine.premarket_tick`。它在取得锁之后再次判断时段、暂停与健康状态，仅能进入盘前分析；窗口已结束不会落入 active 下单逻辑。
5. 引擎串行锁与 SQLite stage 保证钩子、fallback、重复回调和重启不会重复生成成功计划。失败阶段新增持久化冷却，避免 30 秒内耗尽所有 AI 重试。AI 在 09:30 之后才完成时不发布新计划。
6. 修正对账失败假成功：reconcile 返回明确 errors；watchdog 不再将失败结果更新为 last_success，持续失败保留冻结机制。盘前分析也要求对账完整。

## 修改文件

| 文件 | 用途 |
|---|---|
| tools/paper_supervisor.py | 删除 09:32 等待；启动时段/双健康闸门；安全重载旧 worker；注入模拟时钟 |
| src/trading_bot/premarket_scheduler.py | 独立盘前补执行与后台循环，不调用主动交易 |
| src/trading_bot/strategy.py | 初始化心跳、启动/停止 fallback、钩子复用 |
| src/trading_bot/engine.py | 串行入口、盘前专用检查、对账失败拒绝继续 |
| src/trading_bot/execution.py、watchdog.py | 对账错误显式传播，避免假健康 |
| src/trading_bot/storage.py | 失败阶段持久化重试冷却 |
| tests/test_supervisor_schedule.py | 假进程/模拟时钟/真实 Engine+SQLite+FakeAI 调度集成 |
| tests/test_session_engine.py | 持久化冷却与共享钩子契约更新 |
| README.md | 更正调度时序和上线边界 |

## 验证与范围

基线现有测试：**165 passed，0 failed，0 errors，0 skipped**。

修复后完整测试：**188 passed，0 failed，0 errors，0 skipped**，耗时 31.29 秒，1 条第三方 websockets 弃用警告。其中新增 23 个调度/健康检查用例。所有测试禁用网络，进程启动/停止为 fake，临时 SQLite 不触碰生产数据。

覆盖明天 2026-11-06 的 08:50/08:55 正常启动、09:02/09:20 晚启动、09:00 钩子发生时仍暂停而 09:02 健康恢复、完全漏掉 LumiBot 钩子、后台循环补执行、AI 失败冷却后成功、重复/并发/重启防重、09:30 边界、AI 耗时越界、周末和夏/冬令时，以及 watchdog 实际网络错误。

复现命令：

```powershell
Set-Location D:\alpaca-ai\ai-paper-trader
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests/test_supervisor_schedule.py -q -p no:cacheprovider
```

**这不是全系统交易验收通过。** 上轮 11 个独立契约 probes 在当前修复代码复跑为 **3 passed / 8 failed / 0 errors / 0 skipped**：复核失败/冷却后误执行的两项已由 DeepSeek 修复；对账假健康由本轮修复。尚有最终发单时间/暂停闸门、准备后触发与有效期复核、stop_policy 必填、研究初始化交易闸门、未完成分钟线标记、全部持仓研究覆盖等 8 个失败用例。这些不在本次专门的调度修复范围，仍需处理，不能因为 188 个现有测试通过就宣称自动交易已完全正常。

## 明天生效条件

现有 Windows 任务 `AlpacaAIPaperSupervisor` 已启用，查询到下一次运行是 **2026-11-06 20:30 北京时间**，指向本地 `tools/start_supervisor.ps1`，无需新建任务。它应等到 **21:55 北京时间（08:55 ET）** 启动/检查，正常从 **22:00（09:00 ET）** 生成盘前计划。

本轮没有实际触发任务；操作系统自动唤醒/登录环境、明天的 VPN/API 可用性和 AI 输出无法由离线测试保证。电脑需保持可运行、autopilot 仍为 true 且无安全冻结/未知订单；AI 也可以合法输出 NO_TRADE。应查看 `logs/supervisor.log`、盘前 lifecycle 事件、当天 scan/plan 调用记录。若没有满足条件，系统保持暂停或没有计划，不能擅自放宽护栏来制造成功。

回退时先保持暂停，恢复本轮涉及文件至基线提交的版本；不得硬重置整个工作区或删除数据库。回退会恢复原有调度缺陷，不能据此恢复无人值守交易。
