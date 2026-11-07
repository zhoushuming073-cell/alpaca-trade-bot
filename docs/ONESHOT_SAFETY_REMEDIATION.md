# OneShotEngine 安全整改与离线验收记录

日期：2026-11-07。工作分支：`fix/oneshot-safety-audit`。基线提交：`3f6631c512a65016a2ec2442f5da1a6143b9c4d8`。

本记录以当前仓库代码和离线测试为证据。随附的两份 AI 文档用于定位待查问题；其旧行号、历史测试结果和结论未直接作为事实引用。本文不代表 Alpaca Paper 真实网络验收。

## A. 审计范围与入口

正式入口为 `main.py paper` → LumiBot `PaperStrategy` → `OneShotEngine.tick()` → `OrderService.submit()` → `PaperGateway.dispatch()` → Alpaca Paper SDK。主动撤单只由 OneShot 平仓决策在确认冲突挂单后进入 `PaperGateway.cancel()`。Watchdog 和普通对账只读。`tests/test_production_entry.py` 在完全离线的 PaperGateway 替身下验证了正式策略初始化与迭代调用 OneShot；生产目录中未找到旧 Engine 或 `REQUEST_AI_RECHECK` 可达交易入口。历史数据库表仍保留以兼容既有记录。

09:00–09:30 ET 为盘前，09:30–12:00 ET 为主动阶段，11:59:30 ET（含）以后禁止新订单与主动撤单。券商已接受的 DAY 单继续由券商管理；截止后本地只读观察和对账。

## B. 变更及原因

| 文件 | 改动 |
| --- | --- |
| `src/trading_bot/one_shot_engine.py` | 同股、同方向、同仓位意图的活跃挂单先按方向识别；改价或改量也必须有 AI 的 `intentional_additional_pending` 明确授权。AI 失败采用持久化有限退避。可选 CLOSED 历史故障降级并标记质量。账户指纹容许纯净值估值变化，但严格绑定现金、购买力、仓位和挂单。平仓撤单传递原 AI 决策。 |
| `src/trading_bot/broker.py` | POST/DELETE 绑定单次写许可、CID、订单身份、决策指纹、有效期、暂停/冻结/配置和纽约截止时间；在 SDK 请求入口及最外层项目可控传输入口复检。撤单目标必须匹配本地已接受订单的券商 ID。其他写动词和路径拒绝。记录不含密钥的写门禁事件。 |
| `src/trading_bot/execution.py` | 持久化派发边界前增加保守写缓冲检查。废止 `reconcile(cancel_lingering=True)` 自动撤单路径。 |
| `src/trading_bot/storage.py` | 增加决策只读查找和跨进程原子 AI 尝试计数；未变更表结构。 |
| `src/trading_bot/settings.py`、`config/execution.yaml` | 增加 2 秒保守写缓冲、15/30/60/120 秒 AI 失败退避、每日 60 次尝试上限及配置校验。 |
| `prompts/one_shot_decision.txt` | 明确告知 AI：同向挂单即使改价或改量，仍属需显式授权的追加挂单。 |
| `tests/test_one_shot_engine.py`、`tests/test_wire_guards.py`、`tests/test_order_lifecycle.py`、`tests/test_production_entry.py` | 增加方向重复、真实模块的模拟 HTTP POST 次数、SDK 末层截止/授权、撤单身份、退避/重启、可选历史、结构性账户变化和缓冲边界测试。 |

没有删除数据库、历史订单、备份或凭证。配置文件变更会触发既有活动配置哈希的安全冻结流程；需要在未来获授权的启动流程中处理，不能由本轮自动恢复。

## C. 安全不变量核对

- 每个 `decision_id` 在 SQLite 保留唯一指纹，订单 CID 持久化；同一决策最多一次 POST。若派发后结果未知，只通过 CID 对账并冻结，不自动重发。
- `NO_TRADE` 不撤销挂单。未授权的同向挂单增量为零；明确追加仍须通过资金、股票池、报价、时间和状态机检查。
- 部分成交的冲突挂单需先凭 AI 平仓授权撤单，确认券商状态、已成交量与仓位没有变化，才允许平仓；无法确认即停止。
- 11:59:30 ET（含）以后，模拟传输最外层 POST/DELETE 调用数为零；GET 不受写许可限制。2 秒保守缓冲降低本地检查后跨界的概率。此防护不能保证远端券商的实际接收时间，因为 SDK 检查后仍有操作系统调度与网络延迟。
- 写许可单次消费，绑定已记录的持久化派发意图或已追踪的活跃订单和已保存的 AI 平仓决策。裸 SDK 写调用在传输前拒绝。

## D. 测试证据

基线：Python 3.13.7，`pytest -q` 为 **306 passed，1 warning**。红灯复现：B1 的改价/改量三种未授权追加测试、R1 的查询或 SDK 入口跨过截止测试、B2 的 AI 短故障重试测试、B3 的可选 CLOSED 历史测试、B4 的净值微变测试，在对应修复前均失败。修复后完整套件为 **335 passed，1 warning，29.11s**。唯一告警来自第三方 `websockets.legacy` 弃用提示。一次中间回归的唯一失败是旧测试匹配原错误文案；已保持原文案兼容，最终回归通过。

`python -m compileall -q src tests`、`node --check web/app.js`、`git diff --check` 均通过。测试使用 `tmp_path` SQLite、FakeBroker 或拦截到 SDK 最外层的模拟传输；`tests/conftest.py` 全局禁止网络 socket。未执行 Paper/真实下单、撤单或付费模型调用。

覆盖的重点场景包括 11:59:29.999/11:59:30.000/11:59:30.001、纽约夏令时及节假日、截止跨越、直接 SDK 写绕过、合法/不合法撤单、部分成交、重复决策、现金不足、AI 超时/429/坏输出、退避与重启尝试上限、午后只读观察、正式 LumiBot 策略入口。既有 306 项还覆盖订单状态机、未知结果、重启恢复、并发与风险管理。

## E. 数据库与运行状态

新增退避及次数记录使用既有 `controls` 表，决策身份使用既有 `one_shot_decisions` 表；没有新增或删除列，无数据迁移。历史交易记录保留。2026-11-07 使用 SQLite `mode=ro` 只读查询外部运行库的两个控制项，结果为 `paused=true`、`autopilot=false`；没有写入该库，也没有连接券商账户。

## F. 剩余风险及下一阶段

| 项目 | 状态与下一步 |
| --- | --- |
| B5：48 标的上下文耗时 | 未作性能改造。当前每日历史有缓存，但 ACTIVE 报价对整个股票池串行读取，SDK 客户端还由锁保护。需先用固定延迟和实际只读网络测量请求数、P50/P95、报价年龄及缺口，再评估安全的批量读或有界缓存；不能声称性能改善。 |
| B7：进程死亡恢复 | 仓库已有 Windows Supervisor/Recovery 安装脚本和进程内 Watchdog；本轮未安装、启用或演练外部任务。无人值守运行仍需单独验证崩溃重启、只读对账、失败限流及持续暂停语义。 |
| Alpaca SDK/服务端契约 | 本轮只验证 SDK 路由与模拟 HTTP 边界，没有真实网络、真实 429/超时、券商接收时间或 Paper 订单验收。需在用户另行授权后有人值守验证。 |
| 审计报告 B6 | 当前 HEAD 的完整基线已是 306 通过，所述 10 项 DuckDB 缺失失败在此工作目录未复现；本轮未改变 exam 数据或跳过测试。 |

下一阶段离线测试：**PASS**。未来有人值守 Paper 测试：**CONDITIONAL**，需明确单独授权并确认暂停/配置重启流程、真实 SDK 和服务端行为。无人值守：**BLOCKED**，直至外部监督与长时间运行演练完成。当前保持暂停，不自动启动或恢复 Autopilot。

## G. 未执行操作

未提交任何真实或 Paper 订单、未发撤单/替换请求、未改券商账户、未修改生产 SQLite、未启动交易进程、未启用定时任务或外部部署、未调用付费 DeepSeek、未推送远端。所有变更只在本地工作分支的源码、测试、配置和本文件中。
