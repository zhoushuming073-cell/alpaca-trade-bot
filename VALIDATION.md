# 验证记录（2026-11-05 终版）

本文件区分严格不同的验证等级，禁止把离线测试当作 Paper 成交验证。

## 1. 代码/离线自动化

- 基线（修改前）：180 passed；旧测试覆盖 IOC、target_weight、29:30 全撤等已被最新产品规则替代的语义。
- 终版：`140 passed, 0 failed, 0 errors, 0 skipped`（1 条第三方 `websockets.legacy` 弃用警告，非失败）。
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

## 4. Paper 实际订单生命周期

**未验证。** 本版本没有向 Alpaca Paper 提交任何订单，也没有测试真实 DAY 下午成交、部分成交、撤单竞态、OTO/止损组合、断网恢复或长期运行。任何“已通过全部测试”的说法都不包含这一级。

## 5. 长期运行

**未验证。** 看门狗心跳/SQLite 性能/输入压缩有离线测试，但没有 24/5 连续运行观测。

## 6. 当前运行状态

- worker：未运行；数据库持久化 `paused=true`；
- 看板：本机 4200 的旧进程可能仍加载修改前代码；升级后需安全重启 dashboard 才会显示新页面；
- 默认不自动恢复交易；没有发出订单。
