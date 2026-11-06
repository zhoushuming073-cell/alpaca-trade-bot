# 三智能体决策系统（in-process）

## 1. 边界

三个角色由同一个 Python 进程在盘前运行，**不是**三个长期运行的服务，也不引入 LangGraph/CrewAI/AutoGen：

```text
Python 数据层 / 研究记忆 / 账户
        │
        ├─ MarketResearchAgent  (全池轻扫 + 候选深研)
        ├─ SkepticalAnalystAgent (逐份研究质疑)
        └─ PortfolioManagerAgent (唯一最终决策者)
        │
        └─ PremarketPlan（现有严格 Schema）
              │
        现有 Python 风控 / OrderService / Alpaca Paper
```

三角色共用现有 DeepSeek 客户端和 API Key，但调用参数、Prompt、输入、上下文、输出 Schema、预算和审计记录相互独立。它们**不共享聊天上下文**；共享的只有经过 Python 验证的数据快照、结构化研究和订单事实。

## 2. 角色与 Schema

| 角色 | 模块/模式 | 输出 | 能否下单 |
|---|---|---|---|
| 市场研究员 | `RESEARCHER_SCAN`、`RESEARCH_REPORT` | `ResearchScan`、`ResearchReport` | 否 |
| 反方分析师 | `SKEPTIC_REVIEW` | `CritiqueReport` | 否 |
| 基金经理 | `PORTFOLIO_MANAGER`、`PORTFOLIO_RECHECK` | 现有 `PremarketPlan` / `RecheckResponse` | 只能产生计划；由 Python 执行 |

Schema 位于 `src/trading_bot/agent_schemas.py`，均使用 `extra=forbid`。研究员/反方的 Schema 没有 `order_intent`、`side`、`qty` 或 `limit_price`。最终计划仍使用 `decision_schema.py` 的版本化严格 Schema。

字段覆盖：

- `ResearchReport`：`run_id`、`as_of_utc`、`universe_version`、`symbol`、setup/grade、技术结构、基本面、事件证据、支持/反证 evidence IDs、入场/失效条件、上行/下行情景、数据质量、未知项、研究结论。
- `CritiqueReport`：`run_id`、`research_report_id`、验证/未支持/矛盾声明、替代解释、数据缺口、重大风险、失效情景、严重度、总体评估、evidence IDs。
- `PremarketPlan` 扩展了可选 `run_id`、`decision_mode`、`research_report_ids`、`critique_report_ids`；`Candidate` 扩展了报告关联和 `counterevidence`。旧调用保持默认值兼容。

## 3. 执行顺序

盘前：

1. Python 生成同一份账户/持仓/行情/研究记忆快照。
2. 研究员做固定池轻扫（0–8 个候选，不设配额）。
3. 对每个候选做深研；最多 `max_deep_candidates` 个。
4. 反方读取该报告和原始证据，独立提出质疑；可同意。
5. 基金经理拿到报告、反方意见、账户和有效安全约束；不能使用“赞成票”规则。
6. Python 校验最终 Schema、研究/质疑关联、账户快照、窗口；再走现有风险与计划持久化。

失败语义：

- 研究员/反方/经理超时、429、5xx、空响应、截断、非法 JSON、Schema 错误：有界重试；仍失败则 fail-closed。
- 某个候选的反方失败：该股票被标记为 blocked；经理若对该股票下单，整个计划被拒绝，不会采用研究员的买入建议。
- 经理失败：没有计划、没有交易授权。
- 数据/研究不完整：允许 `NO_TRADE`/`WAIT`。
- 计划跨过 09:30 ET 或超过基金经理批准的有效期：不执行。

盘中 `REQUEST_AI_RECHECK`：

- 只对相关股票重新运行研究员 → 反方 → 基金经理。
- 使用新的 `PORTFOLIO_RECHECK` 严格响应；`REPLACE` 必须引用本轮研究和反方报告。
- 仍受原有冷却、每日复核预算、09:30–10:00 窗口、风控和 idempotency 约束。

## 4. 配置与预算

`config/multi_agent.yaml`：

```yaml
enabled: false
agents:
  researcher: {model: deepseek-flash, max_tokens: 6000, timeout_seconds: 45, max_retries: 1}
  skeptic: {model: deepseek-flash, max_tokens: 5000, timeout_seconds: 45, max_retries: 1}
  portfolio_manager: {model: deepseek-flash, max_tokens: 8000, timeout_seconds: 60, max_retries: 1}
  baseline: {model: deepseek-flash, max_tokens: 8000, timeout_seconds: 60, max_retries: 1}
execution:
  paper_only: true
  require_complete_decision: true
  max_deep_candidates: 6
  max_parallel_requests: 2
  deadline_buffer_seconds: 45
budget:
  max_session_tokens: null
  max_session_cost_usd: null
  default_session_tokens: 120000
  default_session_cost_usd: 1.0
  cost_per_1m_input_usd: 0.0
  cost_per_1m_output_usd: 0.0
comparison:
  enable_single_agent_baseline: true
  baseline_can_trade: false
```

- `null` **不是无限**；使用 `default_*` 的有限上限。
- 默认成本费率是 0，UI 会显示“费率未配置”，不会伪造实际成本。配置真实费率后才计算 `cost_usd`。
- `baseline_can_trade` 被 Schema 固定为 false；基准只保存影子决策，不创建可执行计划。
- API Key 只来自现有环境/本地凭据；不在 YAML、数据库 payload 或日志中。

## 5. 记忆与审计表

非破坏性 SQLite 迁移增加：

- `agent_runs`：run_id、交易日、模式、状态、快照/策略版本、基准状态、错误。
- `agent_reports`：按 `run_id + role + symbol` 保存原始 `report_json` 与解析 `parsed_json`，不覆盖其他角色。
- `agent_plan_links`：run → 最终 plan → 经理报告；记录是否被 Python 执行。
- `agent_feedback`：计划/结果反馈，供下一轮研究检索；不自动修改策略或风控。
- `agent_cache`：同一 run/输入的短期缓存和命中次数，缓存内容仍要过同一 Schema。
- `agent_experiments`：单智能体基准 vs 三智能体的同一快照决策、Token 和延迟。
- `ai_calls` 新增 `run_id`、`agent_role`、`cost_usd`、`cached`；每个 provider 调用只有一行。

研究记忆仍复用 `research_profiles`、`research_evidence`、`research_revisions` 和 `daily_scans`；下一次只检索相关股票最近的报告/摘要，不发送完整历史对话。

## 6. 单智能体基准

- 基准调用现有 `PREMARKET_PLAN` Prompt，使用与三智能体相同的 `base_payload`、focus 和行情快照。
- 它永远不会写入 `plans`、`OrderService` 或 Alpaca；只写 `agent_reports(role='baseline')` 和 `agent_experiments`。
- 正式运行模式切换必须显式修改配置并重启，保留审计。当前默认关闭三智能体，基准也只在三智能体运行时作为影子记录。

## 7. Web

新增“智能体”页面：

- 角色状态、模型、最新运行/错误；
- 本轮 run、Token、费率/成本、基准状态；
- 每只候选的研究员结论、技术摘要、反方评估/严重度、证据数；
- 基金经理最终计划、股数、限价、止损、研究关联、关键反证；
- 按角色拆分的 API 调用、Token、缓存和延迟；
- 研究/计划/监测/订单/冻结分离的安全状态；
- 对照实验（基准只读、不下单）。

## 8. 启用与验证

默认 `enabled: false`。启用步骤：

1. 先保持 `paused=true`，确认无未知/冲突订单。
2. 明确配置角色模型、Token/成本上限和费率。
3. 将 `enabled: true`，安全重启 worker/dashboard（不自动 resume）。
4. 只做离线/影子验证；真实 DeepSeek 与 Paper 订单需要用户另行授权。
5. 任何真实 Paper 订单仍需原有触发、风控、窗口和授权链。

本仓库的离线测试覆盖三角色顺序、0 候选、反方同意/反对、反方超时、经理拒绝、池外股票、非法 JSON、Schema 额外字段、缺失证据、截止时间、预算、Token 记录、记忆持久化、次日恢复和 Fake Alpaca 成交。**未执行真实 DeepSeek 付费调用，也未向真实 Paper 账户提交订单。**

## 9. 文件

- `src/trading_bot/multi_agent.py`：编排器、预算、缓存、角色链路。
- `src/trading_bot/agent_schemas.py`：三个角色 Schema。
- `prompts/researcher_scan.txt`、`researcher_report.txt`、`skeptic_review.txt`、`portfolio_manager.txt`、`portfolio_recheck.txt`。
- `src/trading_bot/engine.py`：盘前/复核接入；不绕过原执行层。
- `src/trading_bot/storage.py`：agent_* 表及非破坏性迁移。
- `src/trading_bot/dashboard.py`、`web/app.js`、`web/index.html`：智能体页面。
- `tests/test_multi_agent.py`、`test_multi_agent_engine.py`、`test_agent_schemas.py`。
