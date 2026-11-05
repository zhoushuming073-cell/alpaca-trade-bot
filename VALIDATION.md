# 验证记录（2026-11-05 终版）

本文件区分严格不同的验证等级，禁止把离线测试当作 Paper 成交验证。

## 1. 代码/离线自动化

- 基线（修改前）：180 passed；旧测试覆盖 IOC、target_weight、29:30 全撤等已被最新产品规则替代的语义。
- 终版：`159 passed, 0 failed, 0 errors, 0 skipped`（1 条第三方 `websockets.legacy` 弃用警告，非失败）。
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
- 账户现金、数量、成本价可与同时段直连 Alpaca 快照核验；价格类字段（净值/市值/浮盈亏/购买力）会随两次请求间隔的行情变动，页面显示抓取时间与年龄。
- 失败时保留最后一次真实快照并显示 `stale/error`；无快照时显示不可用，不填零或样例值。

### 3.3 Phase 2：DeepSeek Search/Fetch

- 官方接口已实际调用：
  - `POST https://api.deepseek.com/v1/tools/search`
  - `POST https://api.deepseek.com/v1/tools/fetch`
- 已用真实 CRWV 查询验证 Search 返回标题/URL/日期/authority，Fetch 返回 Markdown 正文。
- 研究档案已写入 SQLite：来源 URL、标题、发布时间、抓取时间、内容哈希、`verified`、摘要、档案版本和过期时间。
- 研究批次仍在后台运行；完成数量以最终数据库/报告为准，不能把已开始的 48 只说成全部完成。

### 3.4 Phase 3：真实盘前计划

- 2026-11-05 09:00 ET 使用真实账户、部分研究档案、行情和 AI 生成盘前计划；
- AI 返回 7 个 `HOLD/WAIT` 计划，未生成可执行下单意图；
- `UNIVERSE_SCAN` 首次因模型把 numeric evidence_ids 返回为整数触发 schema error，修复重试后成功；随后增加了 evidence_ids 数字到字符串的输入规范化与 prompt 约束。
- 本轮盘前脚本没有下单，也没有启动 worker。

## 4. Paper 实际订单生命周期

**未验证。** 本版本没有向 Alpaca Paper 提交任何订单，也没有测试真实 DAY 下午成交、部分成交、撤单竞态、OTO/止损组合、断网恢复或长期运行。任何“已通过全部测试”的说法都不包含这一级。

## 5. 长期运行

**未验证。** 看门狗心跳/SQLite 性能/输入压缩有离线测试，但没有 24/5 连续运行观测。

## 6. 当前运行状态

- worker：未运行；数据库持久化 `paused=true`；
- 看板：已用新代码重启，4200 上 `account_source=ALPACA PAPER 实时`，不是旧进程；worker 仍未启动；
- 研究：DeepSeek Search/Fetch 已真实验证，研究批次仍在后台构建，完成数量以数据库最终统计为准；
- 默认不自动恢复交易；本轮没有发出订单。
