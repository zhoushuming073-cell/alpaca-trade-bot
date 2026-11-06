# 验证记录（2026-11-05 终版）

本文件区分严格不同的验证等级，禁止把离线测试当作 Paper 成交验证。

## 1. 代码/离线自动化

- 基线（修改前）：180 passed；旧测试覆盖 IOC、target_weight、29:30 全撤等已被最新产品规则替代的语义。
- 终版：`226 passed, 0 failed, 0 errors, 0 skipped`（1 条第三方 `websockets.legacy` 弃用警告，非失败）。
  - 运行：`.venv/Scripts/python -m pytest -q`
- 覆盖重点：订单状态机/预发送 NOT_SENT/崩溃恢复/并发提交/看门狗竞态/看门狗静默死亡与心跳/DAY 订单 10:00 后不撤/次日对账/整数股与具体 qty/无仓位比例上限/非杠杆现金/卖单与止损占用/BROKER_STOP 不降级/严格 v2 Schema/计划过期与触发去重/AI JSON 修复/研究记忆/旧 SQLite 迁移/压缩 AI 输入/查询计划/看板有界查询。
- `python -m compileall`、`pip check`、`node --check web/app.js`、`git diff --check` 结果见最终交付报告。

## 2. 离线集成（无 socket）

`.venv/Scripts/python -m trading_bot.offline_validation` 使用临时数据库、合成行情/券商/AI，主动禁用 socket：

- 48 个固定池标的，盘前 `UNIVERSE_SCAN` → `PREMARKET_PLAN`；
- 09:35 ET 触发 `EXECUTE_ORDER`，提交 1 张合成 DAY LIMIT 买单，3 股；
- 模拟券商接受但暂不成交；10:05 ET 后没有新单，也没有自动撤单/平仓；
- 全天之后券商成交，次一交易日盘前先对账为 FILLED，再生成新计划；
- `NONE` 止损策略记录为 `NONE_BY_AI`；
- 真实/Paper 外部订单提交：0。

## 3. 真实 API 只读验证（已执行，无订单）

2026-11-05 使用 `./run.ps1 preflight` 读取 Alpaca Paper：

- 账户读取成功；已有持仓、无开放订单；`blocked=false`；具体余额和持仓不写入仓库文档；
- IEX 5 分钟 80 根、6 个自然月日线 128 根通过读取；`read_only_ready=true`；
- `BROKER_STOP` 状态：`UNVERIFIED`；`paper_order_lifecycle_verified=false`。

注意：只读账户读取不是成交验证；buying_power 的融资额度没有被用作可花现金。

### 3.1 真实全池只读行情测量

2026-11-05 盘前使用 4 个有界 worker 读取全部 48 个固定池标的（只读，不下单）：

- 48 只标的、日线 160 根 + 5 分钟 80 根，总耗时约 **8.4 秒**；单只最慢约 2.4 秒；
- 46/48 通过六自然月完整日线校验；`SKHY`、`SPCX` 因历史不足被按设计排除（不是崩溃）；
- 48/48 返回 5 分钟 K 线；
- 非交易时段读取最新报价会因陈旧而被拒绝（`Stale or abnormal IEX quote`），这是预期的 fail-closed 行为，不应放宽新鲜度来制造成交。

### 3.2 Phase 1：Web 与 Paper 真实账户

- dashboard 已切换为后台只读 `LiveAccountFeed`，不再用模拟数据填充账户字段。
- 本地实测 `account_source=ALPACA PAPER 实时`，`account_stale=false`，`account_age≈0`；
- 页面字段来自 Alpaca：净值、现金、购买力、非杠杆可用现金、持仓数量、成本价、市值、浮盈亏、开放订单和订单状态。
- 实盘只读订单验证：Alpaca 返回 0 个 open order；账户历史有 IREN 100 股与 AAPL 1 股两笔 filled；dashboard 已显示真实历史订单、`filled`/本地 `FILLED`、数量/成交价，均来自 Alpaca。
- 账户现金、数量、成本价可与同时段直连 Alpaca 快照核验；价格类字段（净值/市值/浮盈亏/购买力）会随两次请求间隔的行情变动，页面显示抓取时间与年龄。
- 失败时保留最后一次真实快照并显示 `stale/error`；无快照时显示不可用，不填零或样例值。

### 3.3 Phase 2：DeepSeek Search/Fetch

- 官方接口已实际调用：
  - `POST https://api.deepseek.com/v1/tools/search`
  - `POST https://api.deepseek.com/v1/tools/fetch`
- 已用真实 CRWV 查询验证 Search 返回标题/URL/日期/authority，Fetch 返回 Markdown 正文。
- 研究档案已写入 SQLite：来源 URL、标题、发布时间、抓取时间、内容哈希、`verified`、摘要、档案版本和过期时间。
- 全池批次已完成：**48/48** 个档案，339 条证据；`SKHY`、`SPCX` 的日线历史不足，档案明确记录 `market_data_status=UNAVAILABLE`，但基本面来源与证据仍可用，没有伪造行情或档案。

### 3.4 Phase 3：真实盘前计划

- 2026-11-05 09:00 ET 使用真实账户、研究档案、行情和 AI 生成盘前计划；
- 在 48/48 档案完成后再次用完整研究档案 replan：生成 7 个 `HOLD/WAIT/NO_TRADE` 计划，没有可执行下单意图；
- `UNIVERSE_SCAN` 首次因模型把 numeric evidence_ids 返回为整数触发 schema error，修复重试后成功；随后增加了 evidence_ids 数字到字符串的输入规范化与 prompt 约束。
- 本轮盘前脚本没有下单，也没有启动 worker。

## 4. Paper 实际订单生命周期

**已成交。** 2026-11-05 09:30 ET 执行用户授权的 1 股 AAPL Paper 测试：DAY LIMIT 356.21、NONE；Alpaca 返回 `filled`，成交价 353.63，本地状态已对账为 `FILLED`，dashboard 显示真实 filled 订单和 1 股 AAPL 持仓。提交后系统恢复 `paused=true`，未启动 worker，未自动撤单/平仓。**10:00 后未成交 DAY 单持续有效、DAY 到期、部分成交和次日再对账仍未验证。**

## 5. 长期运行

**未验证。** 看门狗心跳/SQLite 性能/输入压缩有离线测试，但没有 24/5 连续运行观测。

## 6. 当前运行状态

- worker：运行中；数据库持久化 `paused=false`；`autopilot=true`；当前美股已收盘，`phase=CLOSED`，没有新增短线订单；
- 看板：已用新代码重启，4200 上 `account_source=ALPACA PAPER 实时`；
- 运行库：`D:\alpaca-ai\ai-paper-trader-runtime\paper.sqlite`；备份：`D:\alpaca-ai\ai-paper-trader-backups`；
- 研究：48/48 档案、339 条证据；DeepSeek Search/Fetch 已真实验证；
- 当前持仓：AAPL 1 股、IREN 100 股；券商 open orders=0；本地 unresolved=0；
- 冻结：已按用户接管后的安全核对解除 `DATABASE_RESTORED_RECONCILIATION_REQUIRED`；解除前已完成全新 GET 对账、备份、225 项测试和 heartbeat 核验。

## 7. Dashboard UX

- 使用 headless Chrome 实际渲染并交互验证：独立页面路由、侧边栏收起/展开、导航胶囊、AI 简报、计划卡片展开、研究搜索/行业筛选、研究详情、AI 助手打开/关闭。
- 计划列表包含数据库中的历史计划；有效状态按 `valid_until` 分类；点击卡片展开分析、触发条件和风险因素。
- 研究详情通过 `/api/research/<symbol>` 读取真实档案与证据；行业标签是本地分类，缺失的“主营业务”等结构化字段不会被编造。
- 5 秒刷新通过 HTML 缓存和计划数据签名避免整页/整卡闪烁；已验证计划展开在刷新后保持。
- 「机器人状态」页使用 `/api/robot-events` 的 SQLite 事件游标做 3 秒增量更新；已验证 AI/执行/成交/系统筛选、详情展开、47 条真实事件、7 秒无重复增长，且页面不提供下单权限。
- 交互使用 Motion Tokens、`prefers-reduced-motion`、View Transitions（支持时）和 CSS transform/opacity；未引入大型动画依赖。

## 8. 网络/关机恢复

- 2026-11-06 实际发生网络中断：Alpaca 连接被重置，watchdog 连续对账失败并写入 `WATCHDOG_UNHEALTHY`；用户随后手动停止。
- 新增 `tools/paper_recovery.py`；在真实网络恢复后实际执行成功：读取 Alpaca、启动 worker、完成干净对账、清除 `WATCHDOG_UNHEALTHY`、恢复 `paused=false`，**没有提交任何订单**。
- 再次执行恢复为幂等 no-op：`worker healthy and autopilot already live; no action`。
- Windows 任务 `AlpacaAIPaperRecovery` 每 2 分钟运行；登录启动目录已放置 `AlpacaAIRecovery.cmd`。`ONLOGON` 计划任务因权限拒绝未创建，已用启动目录替代。
- 人工 `stop` 会关闭 `autopilot`，恢复脚本不会覆盖；未知订单、数据库恢复冻结等不会自动清除。
