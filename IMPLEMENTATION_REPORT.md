# Alpaca AI Paper Trader — 终版实现报告（2026-11-05）

> 本报告对应本地分支 `fix/paper-order-safety`。提交哈希见第 1 节。  
> 本版本**没有发送真实/Paper 订单、没有自动恢复 worker、没有推送 GitHub、没有把密钥写入仓库**。

## 1. Git 与基线

- 仓库：`D:\alpaca-ai\ai-paper-trader`
- 分支：`fix/paper-order-safety`
- 修复提交：`dcf2dec`（核心实现；后续安全修正提交见 `git log -1`）
- 修改前基线：`180 passed`（旧 IOC/target_weight/29:30 全撤语义）。
- 修改后：`165 passed, 0 failed, 0 errors, 0 skipped`（1 条第三方 websockets 弃用警告）。
- 检查：`compileall` 通过、`pip check` 无 broken、`node --check web/app.js` 通过、`git diff --check` 通过。
- 未执行：Paper 实际订单生命周期、长期 24/5 运行、真实 DeepSeek 下单决策调用。

## 1.5 2026-11-05 三阶段迭代（进行中）

### Phase 1：真实 Paper 账户数据

- `dashboard.py` 增加后台只读 `LiveAccountFeed`，每 5 秒读取 Alpaca Paper；页面请求读缓存，不在 Flask 请求线程里等待网络。
- 账户净值、现金、购买力、非杠杆可用现金、持仓数量/市值/成本/浮盈亏、开放订单和状态均来自 Alpaca；失败保留最后成功快照并标记 `account_stale/account_error/account_age_seconds`，没有数据时显示不可用，不填样例或 0。
- 实测 4200：`account_source=ALPACA PAPER 实时`，`stale=false`，`age≈0.2s`；现金、可用、持仓数量、成本价与直连 Alpaca 快照一致，净值/购买力/现价差异为两次请求间隔的行情变动。
- 2026-11-05 09:30 ET 已执行用户授权的 1 股 AAPL Paper 测试（DAY LIMIT 356.21，NONE）：Alpaca 返回 `filled`，成交价 353.63，本地已对账为 `FILLED`，dashboard 显示真实成交订单和 1 股 AAPL 持仓；提交后立即恢复暂停，未启动 worker，未自动撤单/平仓。10:00 后未成交 DAY 持续有效、DAY 到期、部分成交和次日再对账仍未验证。
- Alpaca 返回 0 个 open order、1 个 closed order（IREN 100 filled）；dashboard 已展示该真实历史订单的 `filled`/`FILLED`、数量、成交价和日期，没有提交新订单。
- 网络抖动由后台重试与最后成功快照处理；新增 `tests/test_dashboard_live.py`。

### Phase 2：Search/Fetch 与研究记忆

- 新增 `src/trading_bot/sources.py`：官方 `POST /v1/tools/search` 和 `POST /v1/tools/fetch`，Bearer 鉴权、超时、408/429/5xx 重试、错误显式降级；密钥不进入审计。
- 研究证据持久化：来源 URL、标题、发布时间、抓取时间、available time、内容哈希、摘要、verified；模型证据强制 `verified=false`，只有真实检索/Fetch 的 source adapter 可标记 verified。
- 支持 `research-bootstrap --symbols/--all --batch --force`；已有未过期档案跳过，过期/force 才重研；档案版本递增、revision 保留。
- 真实调用已验证 Search 与 Fetch；全池 48 只研究批次已完成 **48/48**，339 条证据；`SKHY`、`SPCX` 的日线历史不足，档案明确记录 `market_data_status=UNAVAILABLE`，但基本面来源与证据仍可用，没有伪造行情或档案。

### Phase 3：盘前 AI 计划

- `Engine._base_payload` 已加入 `research`、已验证 `news`、账户、策略、会话和执行契约；`_premarket_plan` 继续提供候选行情/报价/触发；Python 只做数据、条件比较、风控、订单执行和审计。
- 2026-11-05 09:00 ET 用真实账户/研究档案/行情运行一次 `premarket_once.py`：生成 7 个 `HOLD/WAIT` 计划，没有下单，没有启动 worker。
- 48/48 档案完成后再次用完整研究档案 replan：生成 7 个 `HOLD/WAIT/NO_TRADE` 计划，仍没有下单。
- `UNIVERSE_SCAN` 第一次因模型把 `evidence_ids` 返回为整数触发 schema error，自动修复后成功；随后对 `Candidate/ScanCandidate.evidence_ids` 增加数字到字符串的输入规范化，并在 prompt 中要求字符串。
- 独立 `POSITION_REVIEW`、`ADJUST_STOP/REMOVE_STOP`、真实 Paper 成交和长期运行仍未完成。



## 2. 实际修改文件

新增：
- `src/trading_bot/order_state.py`：显式订单状态机、合法迁移、券商状态映射。
- `src/trading_bot/research.py`：固定池研究档案/证据/修订、来源不可用降级。
- `src/trading_bot/sources.py`：DeepSeek 官方 Search/Fetch、重试、证据规范化。
- `tests/test_dashboard_live.py`、`test_sources.py`、`test_order_lifecycle.py`、`test_new_rules.py`、`test_schema_v2.py`、`test_session_engine.py`、`test_watchdog_health.py`、`test_research.py`、`test_performance.py`。
- `IMPLEMENTATION_REPORT.md`。

重构：
- `storage.py`：schema v2、状态/版本/租约/dispatch 边界、事务、迁移、索引、看门狗健康、计划/触发/AI 调用/研究表、压缩 input blob。
- `execution.py`：预发送与 dispatch 后严格分离、NOT_SENT/UNKNOWN、按 client_order_id 对账、并发安全。
- `broker.py`：`prepare`/`dispatch` 两阶段、DAY 限价、非杠杆现金快照、IEX quote/trade、可选 OTO 路径、Paper invariant。
- `watchdog.py`：只读对账、心跳/连续失败/alive、循环隔离、无 10:00 自动撤单。
- `engine.py`：盘前计划 → 机械触发 → 主动复核 → 10:00 停止；无固定 5 分钟全量重研。
- `settings.py`：配置模型/哈希/active config；`execution.yaml` 新契约；删除 IOC 统一阻断。
- `risk_manager.py`：AI 具体 qty、无百分比仓位上限、非杠杆现金、卖单/止损占用、保留非仓位熔断。
- `decision_schema.py`、`deepseek_client.py`：v2 严格 Schema、JSON 修复、token/延迟/结果审计。
- `market_data.py`：ET/交易日历阶段、session、compact 原始结构行情、quote 字段。
- `dashboard.py`、`web/*`、`reporting.py`、`preflight.py`、`main.py`、`run.ps1`、`dry_run.py`、`offline_validation.py`、`strategy.py`。
- `config/execution.yaml`、`risk.yaml`、`strategy.yaml`、`tools/untrack_runtime.py`、README/VALIDATION/历史 REPAIR 报告标注。

## 3. 原有阻断性缺陷的修复机制

### P0-A 误发状态锁死

- `RESERVED → VALIDATING → READY_TO_DISPATCH → DISPATCH_INTENT_RECORDED` 与 `order_dispatches` 同事务。
- 预发送检查失败（暂停、窗口、报价、资产、可卖、配置/安全）→ `NOT_SENT`，**不查券商、不触发安全锁**。
- dispatch 之后的异常 → `SUBMISSION_OUTCOME_UNKNOWN`；按 cid 查询，找不到也不改 `NOT_SENT`。
- 崩溃恢复：`recover_stale_leases()` 只清 dispatch 之前的本地预留；dispatch 之后的只对账。
- 未知订单人工恢复：`resolve-order --cid ... --confirm`（审计），无需手改 SQLite。
- 最终 09:59:30 截止检查放在 dispatch 边界之前。

### P0-B 看门狗竞态

- 看门狗只查询 post-dispatch 状态；`RESERVED/VALIDATING/READY` 不可见。
- 刚记录 dispatch 的订单有 30 秒查询宽限，避免把在途 POST 误判为未知。
- 订单状态使用 version 条件更新 + SQLite `BEGIN IMMEDIATE`；过渡记录写入 `order_transitions`。
- 并发提交同一 plan/trigger 通过稳定 cid + `INSERT OR IGNORE` + claim 只允许一次 POST。

### P0-C 看门狗静默死亡

- 循环级 try/except；心跳、最近成功、连续失败、alive；连续 3 次失败 → `WATCHDOG_UNHEALTHY` 安全冻结。
- `finally` 标记 alive=false；看板显示 watchdog 健康/无心跳；主引擎在 watchdog 失效时拒绝新风险。
- 看门狗不再自动撤 DAY/止损（符合最新规则）。

### 后续安全修正（同一实现）

- LumiBot 的 `on_trading_iteration` 默认只在开盘后运行，因此新增 `minutes_before_opening=30` 与 `PaperStrategy.before_market_opens()`，在 09:00 ET 真正执行盘前计划；晚于 09:30 启动不会用过期数据补计划。
- 订单 owner 带进程实例前缀；陈旧本地预留只在确认同一进程 lease 失效后标 `NOT_SENT`，避免把同进程在途预留误清。
- 计划 id 按交易日命名空间，避免 AI 跨日复用 `plan-1` 时 SQLite 保留旧 `trading_day` 而使新计划不可见。
- 提交前重新读取报价并校验报价年龄；旧触发报价在慢检查期间过期时拒绝，不放宽新鲜度。
- 人工暂停在引擎层提前阻断；新增买卖方向/已有止损冲突检查；OTO 止损 leg 计入可卖股占用。
- AI 必须回显 `account_snapshot_id`；不一致拒绝计划/扫描；无有效日线的持仓只记录 `held_data_unavailable`，不生成可交易候选。
- `BROKER_STOP` 父单成交但无 stop leg 时标记 FAILED 并冻结；默认禁用，绝不降级。止损 leg（含 OTO 子单）计入可卖股占用，本地/券商同一 cid 的预留不会重复计算。
- 提交前再次比较 active/disk 配置哈希，并重读暂停/安全锁；慢预检期间发生配置漂移、暂停或安全冻结时转为 `NOT_SENT`。
- 看门狗必须同时满足 alive、心跳新鲜、最近一次 reconcile 成功、连续失败 <3；仅线程存活或仅心跳不算健康。
- 计划写入 config_hash；安全重启后旧配置计划自动 INVALIDATED，不能继续触发下单。
- 共享的 LumiBot 日线接口和数据报价客户端加锁串行，避免盘前/盘中有限并发假设 SDK 线程安全。

### P0-D 配置不一致

- worker 启动写 `active_config` 哈希；磁盘哈希变化只设置 `config_pending_restart`，冻结新风险，不部分生效。
- 看板读 active 配置显示；磁盘/active 不一致显示待重启。
- 安全重启按钮/流程：要求无未知/冲突订单，重启后保持暂停。
- `preflight.json` 已用当前代码重新生成；不再包含旧 IOC 的 `ready:true` 矛盾。
- 进程存活不再作为唯一健康指标；worker/watchdog 心跳与阶段可见。

## 4. 并发和崩溃测试结果

`165 passed`，关键用例：

- 预留后、校验前崩溃 → 新进程 `recover_stale_leases` 标 `NOT_SENT`，不查券商。
- 校验失败且从未 POST → `NOT_SENT`，0 submissions/0 lookup/safety 空。
- dispatch 记录后、HTTP 前崩溃 → 重启保持 DISPATCH，宽限后 UNKNOWN + 安全冻结，绝不 NOT_SENT。
- POST 超时但券商已接受 → 查询后 FILLED，不重复 POST。
- POST 超时且无券商记录 → UNKNOWN + 冻结，不重发。
- 两线程同一订单/同一 trigger → 仅 1 次 POST。
- 看门狗扫描正在准备提交的订单 → 状态不变、无查询、无安全锁。
- 看门狗扫描刚记录 dispatch 的订单 → 宽限内不误判；宽限后 404 → UNKNOWN + 冻结。
- 看门狗 SQLite locked/异常 → 循环继续；连续失败 3 次冻结；线程退出 alive=false。
- 部分成交后取消 → 保留累计成交；乱序轮询不会擦除 fill。
- 本地终态与券商活跃状态矛盾 → RECONCILIATION_REQUIRED + 冻结。
- 稳定 cid/重启/重复恢复 → 不重复 POST。
- 旧 SQLite 升级可重复、非破坏性，历史事件保留。

## 5. 新产品规则实现情况

- **DAY LIMIT**：新单为整数股 DAY LIMIT；`validate_order` 拒绝 IOC/市价/notional/碎股/扩展时段。
- **10:00 后**：本地不新增/不撤/不平仓；已接受 DAY 和券商止损留存；E2E 验证无自动 cancel。
- **次日对账**：盘前先 reconcile，再计划；E2E 验证 10:00 后成交在次日变 FILLED。
- **AI 具体 qty**：v2 Schema 必填正整数；风控原样返回/拒绝，不缩小。
- **无仓位上限**：删除 10%/50% 模型字段与逻辑；保留真实非杠杆现金、卖单/止损占用、防重复、非仓位熔断。
- **NONE/BROKER_STOP**：v2 `stop_policy`；NONE 记录 `NONE_BY_AI`；BROKER_STOP 未验证时整体拒绝，不降级；OTO 构造有离线测试，默认禁用。
- **盘前研究/触发/复核**：09:00–09:30 生成计划；09:30–10:00 机械触发；`REQUEST_AI_RECHECK` 有冷却/预算/去重；无固定 5 分钟全量重研。
- **研究记忆**：profiles/evidence/revisions/daily_scans；来源不可用标记；人工 `research-bootstrap`。
- **性能**：AI 输入压缩 blob；看板有界查询；events 索引；高事件量测试；全池真实只读 48 只约 8.4 秒。

## 6. 尚未实现/未验证

- Paper 实际订单生命周期：未执行（用户禁止提交订单）；DAY 下午成交、部分成交、撤单竞态、真实 OTO/止损、断网恢复均未验证。
- `BROKER_STOP`：请求构造/拒绝路径已实现并有离线测试；Alpaca 账户是否支持所需 OTO/DAY/部分成交组合未知；默认禁用。
- 独立 `POSITION_REVIEW` AI 调用：Schema/Prompt 已定义，引擎目前把持仓复审合并在 `PREMARKET_PLAN` 的账户上下文/候选中。
- `ADJUST_STOP`/`REMOVE_STOP` 的券商改单/撤单：未实现。
- DeepSeek Search/Fetch、Vision、SEC/Alpaca News：未实际接入/验证；默认 `UnavailableSources`，不伪造。
- 首次全池深研不会自动执行；需人工 `research-bootstrap`，且会产生 DeepSeek 费用。
- 长期无人值守运行、Windows 强杀后的实盘对账、看板旧进程自动升级：未验证。
- 真实 DeepSeek 交易决策未调用；只验证了只读 Alpaca/IEX 数据。
- `BROKER_STOP` 的止损覆盖/部分成交覆盖/跨日规则未真实测试。

## 7. 当前 API 验证等级

| 能力 | 等级 | 依据 |
|---|---|---|
| Alpaca Paper 账户/持仓/订单只读 | 已真实验证 | 2026-11-05 `preflight` 成功；账户有持仓、无开放订单；未下单 |
| Alpaca Paper IEX 5 分钟/日线 | 已真实验证 | 全池 48 只只读测量；46/48 日线有效，2 只因历史不足排除 |
| Alpaca Paper 下单/成交/撤单 | 未验证 | 禁止提交订单；仅离线 Fake/模拟 |
| Alpaca OTO/券商止损 | 未验证/UNVERIFIED | 默认禁用；仅离线 request 构造测试 |
| DeepSeek 常规 JSON 调用 | 未在本次运行中实际调用 | 代码/离线 Fake 测试；PROMPT/JSON 修复有测试 |
| DeepSeek Search/Fetch、Vision | Search/Fetch 已真实验证；Vision 未启用 | 官方 `/v1/tools/search` 与 `/v1/tools/fetch` 实际调用成功 |
| SEC/新闻/IR | 未接入 | 明确 UNKNOWN/SOURCE_UNAVAILABLE |
| 长期运行 | 未验证 | 无 24/5 观测 |

## 8. 测试数量与命令

- `165 passed`：`.venv/Scripts/python -m pytest -q`
- 离线 E2E：`.venv/Scripts/python -m trading_bot.offline_validation --output test-results/offline-e2e-new.json`
- 编译：`.venv/Scripts/python -m compileall -q src tests`
- 依赖：`.venv/Scripts/python -m pip check`
- JS：`node --check web/app.js`
- 差异：`git diff --check`
- 真实只读：`./run.ps1 preflight`

## 9. 数据库迁移与历史保护

- schema v2 使用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` + 索引；不删除表/行/日志。
- 旧 `orders` 终态映射到 FILLED/CANCELED/EXPIRED/REJECTED；旧开放行映射为 RECONCILIATION_REQUIRED（因为无法证明旧版本 dispatch 边界）。
- 旧事件/账户/heartbeat/controls 保留；迁移在真实数据库副本上验证；可重复运行。
- 未在真实 `data/paper.sqlite` 上执行新 worker；启动时会做同样的非破坏性迁移。
- 未删除 `data/`、`logs/`、历史数据库或密钥文件；未 `reset --hard`、未强推。

## 10. Web 与 Worker 实际状态

- Worker：未运行；数据库 `paused=true`；没有自动 resume。
- 本机 4200 已用新代码重启；`account_source=ALPACA PAPER 实时`，worker 未运行。
- 新页面展示会话阶段、配置哈希/待重启、worker/watchdog 健康、非杠杆现金、持仓止损状态、DAY 订单本地状态、计划/触发、AI token/延迟、研究档案和重要事件。
- 没有 IOC 统一开关；`NONE_BY_AI` 不再显示为错误，`BROKER_STOP` 未验证会显示 UNVERIFIED。

## 11. 仍存在的技术限制与风险

- 无自动止损的 `NONE` 仓位在 10:00 后/隔夜没有本地保护；跳空和滑点可能造成超出预期损失。
- 即使券商止损，也不能保证成交价；不能称为亏损上限。
- 看门狗/本地进程停止后没有云端接管；网络抖动可能导致 UNKNOWN 冻结，需要人工对账。
- 48 只顺序/有限并发数据获取已验证约 8.4 秒；但 AI 延迟、真实盘前 IEX 报价可用性、Alpaca 速率限制仍需开盘实测。
- 46/48 日线有效；`SKHY`、`SPCX` 历史不足被排除，策略池并非全部可交易。
- 现有账户持仓（只读确认存在）必须由首次盘前计划复审；没有自动止损，不能假定有保护。
- 看板 token 暴露给本机 GET；只应 loopback 使用，不暴露到局域网。
- 配置变化必须安全重启；不要直接改运行进程。

## 12. 下一次开盘最小 Paper 验证步骤

1. 先备份 `data/paper.sqlite` 和日志；确认没有未知/冲突订单；保持 worker 停止。
2. 停止旧 dashboard，重新启动新 dashboard；确认页面显示“未运行/暂停”、active config、watchdog 无心跳为正常（worker 未启动）。
3. 运行 `./run.ps1 preflight`，确认只读账户/持仓/开放订单/止损事实；确认 `BROKER_STOP=UNVERIFIED`。
4. （可选、需用户授权费用）运行 `research-bootstrap` 为持仓和候选建档案；否则接受研究记忆为空。
5. 启动 worker（保持暂停），观察一次盘前 `UNIVERSE_SCAN`/`PREMARKET_PLAN`；**不下单**，核对 AI 的 qty/limit/stop_policy/触发条件与账户上下文。
6. 只有用户另行明确授权后，才做 1 股 `NONE` DAY LIMIT Paper 生命周期测试：接受/部分/成交/DAY 到期/10:00 后不撤/次日对账；全程保留审计。
7. 再单独评估是否支持 `BROKER_STOP`（OTO/DAY/部分成交/跨日）；不支持就保持禁用，绝不降级。
8. 完成以上之前，不恢复自动交易。

## 状态表

| 状态 | 项目 |
|---|---|
| **已完成且离线验证** | 状态机/NOT_SENT/UNKNOWN/租约/幂等；看门狗竞态/健康/无自动撤单；配置哈希/待重启；v2 Schema/具体 qty/DAY/无仓位上限/非杠杆现金/卖单与止损占用；盘前计划/触发/复核/10:00 停止/次日对账；研究记忆服务；SQLite 索引/压缩/迁移；看板有界查询/新 UI |
| **已完成但等待 Paper 验证** | NONE 的 DAY 订单真实提交/成交/到期；10:00 后真实下午成交；次日真实对账；断网/重启恢复；Web 实际浏览器渲染；48 只真实盘前 AI 节奏 |
| **依赖外部 API 权限** | Alpaca Paper 下单/OTO 止损组合；DeepSeek 真实交易决策；DeepSeek Search/Fetch/Vision；SEC/News |
| **未完成** | 独立 POSITION_REVIEW 调用；ADJUST_STOP/REMOVE_STOP；自动全池研究 bootstrap；止损覆盖/部分成交覆盖；长期运行 |
| **阻断自动交易的问题** | 未完成真实 Paper 订单生命周期验证；未获用户当次 Paper 下单授权；BROKER_STOP 未验证（只阻断该类意图）；研究记忆未初始化；旧 dashboard 未重启；真实 DeepSeek 决策未验证 |

## 结论

**当前是否满足恢复 Paper 自动交易的条件？**

**不满足。**

阻断条件：① 真实 Paper 订单生命周期（DAY/部分成交/10:00 后成交/次日对账）未验证，且用户本次明确禁止提交 Paper 订单；② 真实 DeepSeek 交易决策未调用验证；③ `BROKER_STOP` 组合未验证（仅阻断该类止损交易，不阻断 `NONE`）；④ 首次全池研究记忆未建立；⑤ 旧 dashboard 进程未重启到新代码；⑥ 长期无人值守运行未验证。即使测试全绿，也不会自动启用机器人。
