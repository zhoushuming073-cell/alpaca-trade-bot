# P0 同步与验收报告 — 2026-11-05

**结论：代码已同步，功能验收未通过，保持 worker 停止。** DeepSeek 的改动已进入 GitHub main，现有测试通过，但独立故障注入复现了 11 个失败用例，涉及 8 组缺陷。不能据此恢复自动交易。本报告不评价策略收益。

本次以用户提供的 [终版需求](docs/ALPACA_AI_TRADING_FINAL_CODEX.md) 为准。它已替代旧的 IOC、目标仓位比例及 10:00 强制撤单要求。该文档第 16、18 节明确要求“本阶段不改任何交易代码、不发送任何订单”“P0 完成之后停止，不自动进入 P1”。本轮只新增审计材料和离线复现脚本、整理 Git 索引，没有修改交易代码、运行实际 AI 请求或发送 Alpaca 订单。

## 1. 仓库实况

### 同步和运行边界

| 项目 | 本轮核验结果 |
|---|---|
| 原工作区 | `D:\alpaca-ai\ai-paper-trader`，分支 `fix/paper-order-safety`，工作区干净 |
| 本地及同名远端分支 | `c2dc47d4f91b3fdd34eab5c0002defca13a749de` |
| fetch 后 origin/main | `3db4cd9ce3b7d66494873fd06f789a31c0e0f3a7`，合并了上述 DeepSeek 改动 |
| 代码比较 | 两提交中的 47 个源码、配置、测试、前端及构建文件逐项 Git blob 比较，差异 0；提交 hash 不同不等于源码落后 |
| 审计目录/分支 | `D:\alpaca-ai\_acceptance-20261105` / `audit/p0-acceptance-20261105`，从 origin/main 创建 |
| 另一现存 worktree | `_main-merge` 中已有用户修改的缓存文件，本轮未覆盖或清理 |
| 运行状态 | 检查时只有 dashboard 相关进程，没有交易 worker；原数据库只读检查 `paused=true`。旧 dashboard 进程不证明已加载新代码 |
| 真实数据库 | 原数据库尚为旧表结构，本轮没有用新版 Store 打开/迁移它，没有删除历史数据 |
| 敏感配置 | 没有移动、修改或重新配置凭据，没有上传凭据；没有更改 VPN/代理/TLS 设置 |

安装环境：Python 3.13.7、LumiBot 4.6.46、alpaca-py 0.44.0、openai 2.54.0（用于 DeepSeek 兼容 API）、Pydantic 2.14.2、pytest 9.2.0。

### 实际调用链

```mermaid
flowchart LR
  A[LumiBot 盘前钩子 / 周期回调] --> B[Engine 本地时段、暂停、配置与健康检查]
  B --> C[PaperGateway REST 账户 / 持仓 / 开放订单]
  C --> D[日线、分钟线、报价 / 研究摘要]
  D --> E[DeepSeek scan / plan / 条件触发 recheck]
  E --> F[Pydantic Schema / 计划与触发记录]
  F --> G[确定性风控 / 现金与可卖量]
  G --> H[OrderService 预留、认领、持久化发送意图]
  H --> I[PaperGateway / SDK 最终报文校验 / Alpaca Paper]
  I --> J[订单查询、状态迁移、成交 / SQLite 审计]
  K[独立 watchdog] --> J
  J --> L[本地 4200 dashboard]
```

SQLite 新模型包含 sessions、plans、plan_triggers、orders、order_transitions、order_requests、fills、ai_calls、research_profiles/evidence/revisions、runtime_health、events/event_blobs。已有脱敏、摘要/大载荷分离及迁移测试；这不等于真实旧数据库迁移已完成。

### 实际执行的验证

| 检查 | 通过 | 失败 | 错误 | 跳过 | 说明 |
|---|---:|---:|---:|---:|---|
| 现有完整 tests 套件 | 147 | 0 | 0 | 0 | 审计工作树复跑 21.49 秒；1 条第三方 websockets 弃用警告 |
| 新增 P0 验收 probes | 0 | 11 | 0 | 0 | 7.49 秒，均为预期安全契约不成立；不是 fixture/setup 错误 |
| 两组分别运行的合计 | 147 | 11 | 0 | 0 | 不将现有套件通过包装为全部验收通过 |

另执行：现有 offline_validation 成功；41 个 Python 文件编译语法检查成功；`pip check` 无依赖冲突；`node --check web/app.js` 成功；Git whitespace 检查通过。离线测试禁止 socket，使用 Fake/Mock 券商、模拟时钟及临时 SQLite。

离线 E2E：48 只股票 → 盘前 scan/plan → 09:35 模拟 3 股 DAY LIMIT BUY → 10:05 不新建、不自动撤单 → 模拟稍后成交 → 次日盘前对账 FILLED，最终 3 股、未知订单 0、自动撤单 0。**外部真实/Paper 提交为 0**。此场景用 ScriptedAI，`input_blobs=0`，不构成真实 DeepSeek 输入/响应审计或研究资料完整性的验证。

完整机器摘要：[audit/results_20261105.json](audit/results_20261105.json)。独立复现：[audit/probe_contracts.py](audit/probe_contracts.py)。现有测试不包含这些新增探针；需显式运行：

```powershell
Set-Location D:\alpaca-ai\_acceptance-20261105
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& D:\alpaca-ai\ai-paper-trader\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
& D:\alpaca-ai\ai-paper-trader\.venv\Scripts\python.exe -m pytest audit/probe_contracts.py -q -p no:cacheprovider
& D:\alpaca-ai\ai-paper-trader\.venv\Scripts\python.exe -m trading_bot.offline_validation
```

## 2. 确定冲突与验收阻断

以下行号对应被审计的 main 源码。本轮不修改这些文件。优先级 P0 表示必须先修复的执行安全缺陷，与本报告的 P0 审计阶段不是同一概念。

| 编号 / 优先级 | 源码位置 | 现状与复现 | 所需修复及验收 |
|---|---|---|---|
| A1 / P0 | `broker.py:344,381`，`execution.py:120` | 上层有时间/暂停检查，但记录发送意图后，最终 SDK 发单 guard 只校验报文和数据库状态。模拟最终发单时已到 10:05 或人工暂停，POST 仍到达假 HTTP transport。2 项失败 | 在最后可控发单边界重读时钟、暂停、冻结、配置；在持久化边界前后区分“确定未发出”和“结果未知”。验证边界竞态，不仅验证慢 prepare |
| A2 / P0 | `execution.py:226,284`，`watchdog.py:49` | reconcile 把订单查询和账户快照超时吞掉，仍返回普通统计；watchdog 将连续 3 次真实 reconciliation 故障记录为成功，失败数为 0。1 项失败 | 对账结果必须明确完整性/错误；网络失败不能更新 last_success；未知事实冻结新风险。测试实际 service，而非仅 mock service 抛异常 |
| A3 / P0 | `engine.py:721,834` | 先把 REQUEST_AI_RECHECK 标成 fired，再尝试复核；冷却阻止复核或 AI 抛异常后，下次 tick 忽略已 fired 的复核触发，原 EXECUTE 买单得以提交。2 项失败 | 区分待复核、复核失败、已完成；关键复核未完成期间原执行授权阻塞，冷却不能当作复核通过 |
| A4 / P0 | `engine.py:693,701,776`，`execution.py:51` | 只在初始时刻检查计划有效期和触发。prepare 后虽刷新报价做风控，却不复核 AI 条件/计划有效期。模拟价格从满足 `last>=100` 变为 99，或准备过程中计划过期，仍发单。2 项失败 | 将计划版本、有效期和条件纳入最终授权校验；新鲜报价合法不等于 AI 触发仍成立 |
| A5 / P1 | `decision_schema.py:125`，`risk_manager.py:215` | 缺失 stop_policy 的 BUY 被默认 NONE，未明确选择也能变为无自动止损意图。1 项失败 | 新 AI 契约必须显式选择 NONE/BROKER_STOP；旧数据迁移的默认值与新授权解析分开，不得隐含替 AI 决定 |
| A6 / P1 | `research.py:23,89,109`，`engine.py:400,570` | 没有 bootstrap 档案仍能产生新 BUY；来源 provider 是占位实现，无每日自动增量/重大事件刷新闭环。1 项失败 | 新风险前检查研究初始化和必要证据，缺失/过期时明确不可交易；接入可验证来源和增量调度 |
| A7 / P1 | `engine.py:369`，`market_data.py` 的 compact_history | 分钟线仅规范时区、排序截断，未标记形成中 bar。本轮 09:35:10 输入 09:35 开始的 bar，原样进入 intraday_recent，无 is_complete。1 项失败 | 明确 bar 的时间区间与完成状态；进行 OHLC/时间/新鲜度检查。新需求允许形成中数据，但必须显式标记，不能冒充完整 K 线 |
| A8 / P1 | `engine.py:504,516,570`，`decision_schema.py` 的 PremarketPlan | 候选先加入，再加持仓，最后截断 24。12 候选 + 20 持仓时 8 个持仓被排除出重点研究；缺少持仓计划仅记录 HOLD。1 项失败 | 全部持仓和开放订单必须审阅；候选研究预算不能截断持仓，支持分批和显式缺数据状态，不能以默认 HOLD 冒充 AI 评估 |

其他静态审计缺口（未冒充新增动态失败数）：

- `broker.py:66,468`：底层单笔 DELETE 和 gateway.cancel 无本地时段闸门。当前新 watchdog 不调用撤单，但这不是可接受的统一写入口保证；后续手工/策略撤改路径应同样禁止 10:00 后主动写请求。
- `settings.py:170`：实际股票池来源是 `config/watchlist.yaml`，不是需求指定的 `stockpool.txt`。当前 48 只匹配，但修改 txt 不会自动改变生效配置及其 hash，须确定唯一权威来源和一致性检测。
- `research.py:149`：AI 自报 evidence 会被标成未验证，但非空 evidence 时 profile 的 OK 状态仍可能保留。不能把版本化档案存在或 AI 写了 URL 当作证据已核实。
- `POSITION_REVIEW` 有 Schema/Prompt，独立日常调用和已有止损 ADJUST/REMOVE 执行闭环尚缺。现有 OTO 买入入口不等于完整持仓止损管理。
- `strategy.py:71` 的短间隔盘前重试与 engine 的 300 秒失败阶段冷却需要联合验证；不能仅从“循环 3 次”宣称真正完成 3 次盘前研究重试。
- watchdog 每 10 秒读对账并刷新账户，即使非主动窗口也会继续；这不会自动取消 DAY，但仍有 REST 负担。是否保留只读监控应与一小时本地在线安排一致，后续按必要性分级缓存。

兼容性判断：AI 明确 qty、整数股/无 notional、删除人为比例上限、DAY 留存、NONE 不强制补止损、未验证 BROKER_STOP 不降级、正常重复触发幂等均已有实现及通过测试；**边界发单、失败复核、真实对账故障、研究前置完整性仍未满足**。

## 3. 订单与网络技术结论

### NONE

需求允许明确选择 NONE 的 DAY 限价单在 10:00 后继续有效，之后成交且无自动止损；这不是旧需求下的“跨窗挂单 bug”。官方 DAY 规则与 OTO 说明见 [Alpaca Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca)。现有模拟已覆盖该分支次日对账，但本轮没有 Paper 实验，下午成交/部分成交/过期/实际账户限制均仍待验证。

### BROKER_STOP

当前配置 `allow_broker_stop=false`、`broker_stop_verified=false`；要求止损的意图被整项拒绝，没有自动改成 NONE。代码构造的是 OTO 入场+止损腿；官方支持 OTO 只附止损，但**特定账户上 DAY 入场、止损有效期、部分成交保护和跨日存续的组合能力仍为 UNKNOWN**。SDK 枚举存在、构造 fake 请求通过不等于账号兼容。不能仅把两个布尔值改为 true 就宣告可用。

此 UNKNOWN 只阻断要求 BROKER_STOP 的意图；它不应单独阻断明确 NONE 模式。本报告阻断恢复自动交易，原因是 A1–A4 等通用缺陷。

### 断网与恢复

已有：唯一 client_order_id、持久化发送边界、认领/幂等、POST 不盲目重试、超时先按 ID 查询、未知状态冻结、部分成交记录、重启与次日恢复测试。REST GET 有界重试，`connect_stream=False`，本轮不归因 WebSocket，不关闭 SSL 验证。

缺口：A1 最终时窗竞态和 A2 对账健康假成功；它们使“超时后有查询逻辑”和“监控健康可靠”不能等同。查询失败时不得假装没订单、已撤单或对账成功。后续需按订单和账户事实分别记录失败、重试/结果、最新权威时刻。

技术边界：即使最终本地闸门修好，也不能保证 09:59:29 发出的请求必在 10:00 前抵达券商或取得回执。需采用提交缓冲并把跨界超时保留为待对账事实，不能截止后补发/主动撤单来掩盖未知状态。正常已接受 DAY 的下午成交本来就是新需求允许的行为。

## 4. 资金与沿用风控

- 旧 `target_weight`、10% 单股/50% 总仓位限制已被新代码移除；合法 AI qty 原样返回，不自动缩量。`test_ai_large_qty_is_not_silently_reduced` 和现金不足拒绝用例通过。
- 风控使用账户 `cash` 而非放大后的 `buying_power`；扣除券商及本地未决买单预留，同 client_order_id 避免重复扣减。实际可用性仍以券商账户规则/结算情况为准，不能仅凭 cash 推断所有资金无约束。
- `config/risk.yaml` 保留旧 `max_daily_loss=0.02`、`max_daily_orders=10`。`risk_manager.py:261` 仅在 BUY 分支应用；两项“达到阈值仍允许合法减仓”测试通过。它们是**沿用旧安全参数、尚待独立评估**，没有在本轮关闭。
- SELL 仍检查真实整数可卖量、待成交 SELL/止损占用、禁止做空、相反订单冲突、暂停/时段/冻结；碎股残余不会自动卖出。相关通过测试见 `test_new_rules.py`。未知订单冻结可阻止 SELL，这是缺少可靠可卖事实时的安全约束，不是普通亏损阈值误伤减仓。
- 整数 qty、禁止 notional、禁实盘端点及直接绕过 OrderService 有低层检查和测试。但“整数报文合法”不抵消 A1/A4 的授权过期问题。

## 5. 数据与 AI 能力

| 能力 | 官方/代码事实 | 本账户、本次验收状态 |
|---|---|---|
| DeepSeek Search/Fetch | 官方列出 `/v1/tools/search`、`/v1/tools/fetch`；仓库 provider 仍是 UnavailableSources | 当前 Key 权限、费用、成功率 UNKNOWN；未实际请求，没有假报已接入。[官方说明](https://api-docs.deepseek.com/guides/search/) |
| Vision | 官方提供 deepseek-flash 图像输入说明；当前首要输入是结构化 OHLCV | 没有图像/混合输入对比实验或质量结论。[官方说明](https://api-docs.deepseek.com/guides/vision/) |
| JSON | json_object 加明确 JSON prompt，仍需处理空白、截断及 Schema 错误；现有 fake 测试覆盖修复/错误 | 真实本次模型响应、当前 Key 可用性与实际 token 费用未核实。[官方说明](https://api-docs.deepseek.com/guides/json_mode/) |
| IEX | Basic 为 IEX 口径，不等于全市场成交量或全国最优报价覆盖 | 不能把 last/bid/ask/VWAP 解释为完整全市场；现有报价时效/价差保护不应因低流动性而随意放宽。[Alpaca 数据文档](https://docs.alpaca.markets/us/docs/about-market-data-api) |
| 六个月日线 | 单股票独立验证，历史不足可排除，现有测试通过 | 本轮是合成数据；没有复跑真实全池读取。仓库原 VALIDATION 中的真实读取记录属于此前记录，不是本轮证据 |
| 分钟线 | 引擎接收近端分钟历史，但 A7 完成标识与校验不足 | 新鲜报价通过不代表历史分钟数据完整 |
| 长期研究记忆 | 有版本、摘要、来源表及手工 bootstrap 框架 | A6 未初始化仍交易；无完整联网核实、每日增量、重大变化刷新及可交易门槛 |

不能用“DeepSeek 可生成研究文本”替代有来源的事实，也不能用 placeholder 为 UNKNOWN 的来源推导关键证据齐全。无真实 API 调用，本轮无法核实 Key 对新能力的权限或真实费用；实际能力/成本验证应单列范围和预算。

## 6. 分阶段 PR 提议与回退

以下只是后续文件级工作安排，本轮没有实施 P1。

| 阶段 | 文件/工作 | 必须通过的验证 | 回退/阻断 |
|---|---|---|---|
| P0 交付 | 本报告、终版需求副本、audit 探针与机器摘要；运行产物取消跟踪 | 现有 147 结果、11 个失败可复现、源码无变化、磁盘历史不变 | 独立审计分支，不覆盖 main，不恢复 worker |
| P1a 发单与监控 | broker.py、execution.py、watchdog.py、storage.py | A1/A2；最终 POST/DELETE 时段、暂停/冻结竞态；超时、重启、不确定状态；绝不重复 POST | 只 dry-run；出现未知事实维持暂停；数据库改动先备份并保留向前兼容，不破坏旧数据 |
| P1b AI 授权完整性 | engine.py、decision_schema.py、risk_manager.py | A3/A4/A5；复核失败与冷却；最新 quote、plan 版本/expiry；止损选择必须显式 | 不恢复默认 NONE、不替 AI 缩量；回退代码仍保持暂停，不能回滚为可交易的已知缺陷版本 |
| P2 研究和数据 | research.py、market_data.py、engine.py、settings.py、prompts、web | A6/A7/A8；初始化/过期/证据失败；全部持仓覆盖；分钟完成标识；stockpool 一致性；来源故障不伪造 | 未准备好的标的禁新风险，保留已有研究版本；不得默认全池就绪 |
| P3 Paper 闭环 | broker/订单审计、受控验证脚本与报告 | 分别验证 NONE 和获支持的 BROKER_STOP 精确组合、下午留单、次日对账、部分成交、未知结果 | 需用户另行明确批准 Paper 订单范围；未知止损组合继续禁用，禁止 live fallback |
| P4 前向评估 | 统计/审计报告 | 样本外表现、集中度/滑点/费用、研究有效性，不宣称收益保证 | 变更核心策略/资金规模另行确认 |

### 后续最小 Paper 验证方案（尚未执行）

1. 先修复上述通用阻断项，使独立 probes 转为通过，并复跑全套；备份真实 SQLite 后验证迁移。确认 worker 停止、paused、唯一实例、Paper 端点、有效股票池和实时时段。
2. 经明确订单授权后，先验证**显式 NONE**：在固定池可交易标的上按批准的数量/限价和总测试金额做一笔受控 DAY LIMIT；用 client_order_id 全程关联 intent/POST/回执/成交。测试量应最小化且事先确定，不用市价追成交。
3. 记录 accepted 与 filled 区别；10:00 不新建、不主动撤单、不补止损。允许符合新规则的既有 DAY 在之后成交/过期；次日从权威账户和订单对账。真实部分成交和响应丢失未出现时标“未观察”，不能冒充覆盖；故障注入优先在 fake 层。
4. BROKER_STOP 单独审批并验证具体 OTO/有效期、部分成交覆盖、跨日、既有止损与主动卖出协同。任一组合不明则仅该组合禁用。普通 NONE 测试不能证明止损有效。
5. 不自动恢复连续交易；逐项区分“离线通过”“只读成功”“真实 Paper 生命周期已观察”。

### Git 整理交付

main 仍跟踪运行产物；`.gitignore` 并不能取消历史跟踪。审计分支调用现有 `tools/untrack_runtime.py`，仅取消索引中的 41 个数据库、日志、缓存、egg-info 等文件。校验其中 11 个 data/log 文件在磁盘存在且 SHA-256 不变；原工作区数据库、历史日志及 `_main-merge` 用户修改没有被覆盖。**历史日志仍保留在本地和既有 Git 历史中，没有历史改写**；已公开的旧内容不会因取消跟踪而从历史消失。

审计分支正常提交/推送，不强推、不自动合并 main。main 上的运行产物要等该整理分支被合并后才会从最新树移除。本轮不删除远端历史，也不擅自更新旧 IMPLEMENTATION_REPORT 的结论。

## 7. 当前状态结论

- **已核验**：本地交易源代码与 main 一致；147 个既有测试通过；合成 DAY/NONE 主流程可跑通；新规则下整数股、AI 原 qty、基本现金/减仓风控、幂等状态模型已有实质改进。
- **验收未通过**：11 个新增失败用例（8 组问题）表明边界授权、监控健康、研究与持仓覆盖仍有缺陷。不是把旧 IOC 规则强加给新 DAY 规则。
- **尚未验证**：真实旧数据库迁移、当前 DeepSeek Key 的 Search/Fetch/Vision 能力/费用、真实 AI 全流程、真实 Paper 生命周期及长期运行。
- **尚未证明技术不可能**：不能仅凭 SDK/文档宣称具体 BROKER_STOP 组合可用或不可用；目前为 UNKNOWN。请求跨越截止点的网络延迟无法由本地绝对保证，只能限制本地发起时点、留缓冲并保留不确定状态。
- **交付后状态**：完成 P0 后停止，不进入 P1，不恢复交易、不发送订单、不覆盖 main。现有 dashboard 可查看信息，但旧进程需在后续安全部署时重启才能确认显示新代码。
