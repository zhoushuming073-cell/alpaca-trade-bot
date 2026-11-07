# Alpaca AI Paper Trader

本仓库是一个只连接 **Alpaca Paper** 的本地 AI 交易工作台。

- DeepSeek 负责研究、候选筛选、多空判断、交易计划和持仓复核。
- Python 负责行情时间、账户/订单事实、时段、资金、风控、幂等、对账、冻结和审计。
- 当前运行实例保持 **暂停**；本仓库不包含任何真实账户、API Key、运行数据库或日志。
- 当前最新生产状态以本地 Dashboard 为准，不以 README 中的示例值为准。

> **安全状态：`paused=true`，不自动恢复交易。**
> 除非用户明确授权，否则不会提交真实或 Paper 订单。

## 1. 当前交易时段

时区固定为 `America/New_York`，使用 NYSE 交易日历，自动处理夏令时和提前收盘。

| 阶段 | 时间 |
|---|---|
| 盘前研究 | 09:00–09:30 ET |
| 主动交易/研究 | 09:30–12:00 ET |
| 新订单截止 | 11:59:30 ET |
| 12:00 ET 后 | 停止新增主动研究和新订单；已有券商订单仍有效 |

关键边界：

- `11:59:29 ET`：允许提交
- `11:59:30 ET`：`can_submit=false`
- `12:00:00 ET`：主动窗口关闭

仓库中的旧考试报告、历史交接记录和 `config/user_strategy.md` 保留了当时的 10:00 ET 规则与实测结果，仅用于追溯；它们不是当前运行配置。当前执行时间以 `config/execution.yaml` 和本节为准。

最终订单网关 `PaperGateway.prepare()` 会再次检查 `session.can_submit`，不是只依赖 AI 或计划状态。

## 2. 双层决策架构

### 2.1 每 10 分钟低成本巡检

交易窗口内每 10 分钟调用当前配置的最低成本巡检模型：

```yaml
patrol:
  interval_seconds: 600
  model: deepseek-flash
  max_tokens: 1200
  timeout_seconds: 25
```

巡检读取：

- 固定股票池行情摘要
- 持仓
- 未成交订单
- 最新新闻
- 历史研究记忆
- 已有交易计划

巡检只输出事件信号，不输出订单、数量或价格。可能信号：

- `NEW_LONG` / `NEW_SHORT`
- `BREAKOUT` / `BREAKDOWN` / `STABILIZATION` / `REVERSAL`
- `NEWS_EVENT`
- `POSITION_RISK`
- `PLAN_INVALID`
- `BETTER_OPPORTUNITY`

没有异常时静默写入 `patrol` 日志，不唤醒专家团队。

### 2.2 事件触发三智能体专家

当巡检信号为 `MEDIUM` 或 `HIGH`，或 Python 检测到以下事件时，立即唤醒专家团队：

- 没有有效计划（盘前缺失、失败或过期）
- 持仓浮盈达到 5% / 10%
- 持仓价格相对上次研究变化 ≥ 2%
- 计划失效、出现计划缺口

三个角色由同一个 Python worker 在进程内运行：

1. **Researcher**：深入机会和市场证据
2. **Skeptic**：风险、反证和逻辑漏洞
3. **Portfolio Manager**：综合判断，决定建立/调整仓位或 `NO_TRADE`

Portfolio Manager 可以生成新的 `INTRADAY_PLAN`，不依赖旧盘前计划。它没有 Alpaca 访问权，不能直接下单。

### 2.3 并发、去重和预算

- 相同巡检事件按 `category + symbol + severity + summary` 去重
- `_expert_running` 防止专家团队重复启动
- 专家超时会释放运行锁并记录错误
- 新计划通过 `supersede_plans` 替换旧计划
- 测试阶段 Token 总上限为 `null`（不设总上限）
- 单次请求仍受各角色 `max_tokens` 限制

## 3. 网页心跳与运行安全

- 浏览器每 10 秒向 `/api/heartbeat` 发送心跳
- 超过 30 秒未收到心跳：服务端停止新增决策和新增订单
- 网页关闭：发送 `/api/heartbeat/close`
- 网页重新连接：先执行只读账户/订单对账，再允许新增风险
- 人工暂停不会被心跳或重连自动解除
- 已接受的 DAY 订单或券商止损不会因暂停自动撤销
- 网页离线或暂停后，已有券商订单仍按券商规则存在

Web 状态包含：

- `paused`
- `running`
- `web_connected`
- `web_heartbeat_age_seconds`
- `config_pending_restart`
- `config_error`
- `patrol` / `expert`
- 订单、持仓、计划、健康状态

## 4. 配置归属

配置严格校验，`extra="forbid"` 未被关闭。

### `config/execution.yaml`

只负责执行和时段：

- `premarket_start`
- `active_start`
- `active_end`
- `submit_cutoff`
- `order_type`
- `time_in_force`
- `allow_shorting`
- `allow_broker_stop`
- 触发轮询、Watchdog、数据并发、AI 请求参数

### `config/multi_agent.yaml`

负责研究和智能体：

```yaml
enabled: true

agents:
  researcher: ...
  skeptic: ...
  portfolio_manager: ...
  baseline: ...

patrol:
  interval_seconds: 600
  model: deepseek-flash
  max_tokens: 1200
  timeout_seconds: 25

active_research:
  interval_seconds: 900   # 保留的旧版全量研究节流参数
  min_cooldown_seconds: 120
  max_tokens: null
```

当前主动研究是 **10 分钟巡检 + 事件触发专家**；`active_research.interval_seconds` 不再单独驱动 15 分钟全量研究，`min_cooldown_seconds` 用于专家唤醒冷却，`max_tokens: null` 表示测试阶段不设总量上限。
budget:
  max_session_tokens: null
  max_session_cost_usd: null
```

`patrol_*` 和 `active_research_*` **不再属于 `Execution` / `execution.yaml`**。

### `config/strategy.yaml` / `config/risk.yaml`

- 策略文本和策略开关
- 非仓位型风险保护：报价新鲜度、价差、单日亏损、订单尝试次数等

## 5. 配置校验与热更新

`load_config()` 一次性验证：

- `watchlist.yaml`
- `strategy.yaml`
- `risk.yaml`
- `execution.yaml`
- `multi_agent.yaml`

任何一项失败都抛出 `ConfigError`，带：

```text
file
field
reason
```

示例：

```text
config/execution.yaml: patrol_interval_seconds: Extra inputs are not permitted
```

行为：

- 不会部分应用新配置
- 磁盘配置与 worker 启动时的 `active_config` 不一致时，设置 `config_pending_restart=true`
- 阻止新增交易
- 记录 `config_load` / `config_pending` 事件
- 必须安全重启并重新验证后才能生效

## 6. 订单与安全边界

本地订单状态独立于 Alpaca：

```text
RESERVED
  -> VALIDATING
  -> READY_TO_DISPATCH
  -> DISPATCH_INTENT_RECORDED
  -> ACCEPTED / PARTIALLY_FILLED / FILLED
  -> CANCELED / EXPIRED / REJECTED
  -> SUBMISSION_OUTCOME_UNKNOWN
  -> RECONCILIATION_REQUIRED

NOT_SENT
```

关键约束：

- 所有新增订单必须经过 `OrderService` 和最终 `PaperGateway` 风控
- 整数股、DAY LIMIT、regular hours
- 不允许 notional、市价、IOC、扩展时段、期权、加密
- 不允许自动跨零仓位
- `BROKER_STOP` 在未验证时整体拒绝，不降级为 `NONE`
- IEX 只是 IEX：单只股票报价异常只影响该标的，不是全局暂停
- AI 不能直接访问 Alpaca

## 7. 当前限制

- 当前运行实例 **暂停**，不会自动恢复交易
- 没有长期无人值守验证
- 没有真实或 Paper 订单由本 README 修改触发
- `BROKER_STOP` 仍未在真实 Alpaca Paper 组合上完成生命周期验证
- 网页关闭后没有云端监控
- IEX 数据不是完整 SIP / NBBO
- 历史考试 `data/exam_replay_runs/full15/` 是 `AUDIT_FAIL_EXPLORATORY`，不能覆盖或改写
- `patrol` 和专家决策是研究/决策路径，不保证收益
- 测试阶段的 `max_session_tokens: null` 表示不设总上限，实际成本需另行监控

## 8. 常用命令

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
.venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m pytest -q
```

```powershell
./run.ps1 check
./run.ps1 config
./run.ps1 preflight
./run.ps1 pause
./run.ps1 resume
./run.ps1 status
.venv/Scripts/python -m trading_bot.main research-only
```

本地 Dashboard：

```text
http://127.0.0.1:4200
```

## 9. 验证状态

最近一次离线验证：

```text
357 passed, 1 warning
python -m compileall 通过
node --check web/app.js 通过
```

覆盖：

- 12:00 / 11:59:30 时间边界
- 配置字段归属
- `ConfigError` 文件/字段/原因
- 配置失败阻止交易
- 网页心跳/断开/重连
- 巡检 10 分钟、无事件、事件升级、去重、模型异常
- 专家重复启动和超时保护
- 计划替换
- 订单网关最终截止检查

## 10. 仓库与安全

- 运行密钥、SQLite、日志、备份不进入 Git
- Dashboard 只监听 loopback
- 不提交真实或 Paper 订单，除非用户明确授权
- 不自动恢复 `paused` / `autopilot`
- 远端仓库：

  <https://github.com/zhoushuming073-cell/alpaca-trade-bot>
