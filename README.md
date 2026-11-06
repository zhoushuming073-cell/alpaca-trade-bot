# 美股 AI Paper Trader（2026-11-05 终版实现）

本仓库是一个**只连接 Alpaca Paper** 的本地 AI 交易台：DeepSeek 负责研究、选股、交易判断、具体股数、限价、触发条件、止损选择和复核；Python 负责数据、时钟、研究记忆、订单合法性、真实非杠杆资金、幂等、恢复、对账、审计和硬性安全冻结。

> 当前运行状态：**Paper 自主交易运行中**（worker 已启动，`paused=false`），不连接真实账户。`BROKER_STOP` 尚未在 Alpaca Paper 上完成真实生命周期验证，默认禁用；AI 选择 `BROKER_STOP` 的意图会被整体拒绝，不会被降级成无止损交易。`NONE` 模式允许无自动止损，但离线风险由用户明确接受。

> 2026-11-05 09:30 ET 已执行一次**用户授权**的 1 股 AAPL Paper 测试（DAY LIMIT 356.21，NONE）；订单已 `filled`，成交价 353.63，提交后立即恢复暂停，未启动 worker，未自动撤单/平仓；当前留下 1 股 AAPL、无自动止损。其余情况下没有自动/真实订单。

## 0. 2026-11-05 三阶段迭代

- **Phase 1**：dashboard 增加后台只读 Alpaca Paper `LiveAccountFeed`，账户净值/现金/购买力/持仓/成本/浮盈亏/开放订单/订单状态直接来自 Alpaca；固定失败重试、字段校验、最后成功快照和 `stale/error/age` 标记；无实时数据时绝不填模拟值。
- **Phase 2**：接入 DeepSeek 官方 Search/Fetch，研究档案写入来源 URL、标题、发布时间、抓取时间、内容哈希、`verified`；支持增量重研、批次和 `--force`，后台已跑完全池 **48/48**；`SKHY`、`SPCX` 的日线历史不足，档案明确记录 `market_data_status=UNAVAILABLE`，基本面来源仍可用。
- **Phase 3**：盘前 payload 同时提供账户、研究档案、已验证新闻/证据和行情；AI 负责选股/计划，Python 仅做数据、触发比较、风控、订单执行和审计。

## 1. 最新运行规则

- **Paper only**：固定 `https://paper-api.alpaca.markets`，不存在实盘开关或实盘回退。
- **固定股票池**：`config/watchlist.yaml`（与工作区 `stockpool.txt` 一致），AI 不得越池下单。
- **启动时点**：worker 应在美东 09:00 前启动（LumiBot 的 `before_market_opens` 在开盘前 30 分钟触发盘前研究）。若在 09:30 后才启动，当天不会补做盘前计划，也不会为了“赶上开盘”使用过期数据下单；这是有意的 fail-closed。
- **只做美股/ETF 多头、整数股、DAY LIMIT、regular hours**；禁止 notional、碎股、融资/杠杆、卖空、期权、加密货币、盘前盘后订单。
- **AI 决定具体 `qty` 和限价**；Python 不缩小、不放大、不自动改为目标权重。资金不足、股数不合法、卖出超过可卖整股时**拒绝**，并记录原因。
- **取消人为仓位限制**：不再有单股 10%、总仓位 50%、固定持股数量等策略限制。仍保留真实非杠杆现金、未成交买单占用资金、已有卖单/止损占用、整数股、无卖空/超卖、防重复、当日亏损/下单尝试熔断、人工暂停、未知订单冻结。
- **AI 自主止损**：
  - `NONE`：不创建自动止损，允许 DAY 买单在 10:00 后继续成交并隔夜持有；Python 不得补设止损。
  - `BROKER_STOP`：要求 AI 给出合法止损参数；只有 `config/execution.yaml` 的 `allow_broker_stop` 与 `broker_stop_verified` 同时为 true 时才允许。当前默认 false，因此该类意图被整体拒绝。
  - `ADJUST_STOP` / `REMOVE_STOP` 的实盘修改尚未实现（见“未实现”）。
- **时段（America/New_York / 交易所日历）**：
  - 09:00–09:30 ET：盘前研究 + AI 生成 `PREMARKET_PLAN`；
  - 09:30–10:00 ET：Python 机械检测 AI 授权的数值触发条件，命中 `EXECUTE_ORDER` 才直接下单；
  - 09:59:30 ET 起不再发起新的本地策略写请求；
  - 10:00 后本地不主动操作：**不自动全撤 DAY 订单、不平仓、不撤券商止损**；已接受的券商订单继续按券商规则有效；
  - 次一交易日盘前先按 Alpaca 权威事实对账，再由 AI 复审持仓/订单。
- **无固定 5 分钟全量重分析**；只有 AI 预先定义的 `REQUEST_AI_RECHECK` 触发、重大变化或记忆过期才再次调用 AI。`EXECUTE_ORDER` 是下单授权，`REQUEST_AI_RECHECK` 不是。
- **研究记忆**：SQLite 中的固定池档案、证据、修订、每日扫描；DeepSeek 官方 Search/Fetch 已接入并真实调用验证，来源、发布时间、抓取时间、内容哈希和 `verified` 标记持久化；来源不可用时明确标 `UNKNOWN/SOURCE_UNAVAILABLE`，不伪造。

## 2. 订单状态机（核心安全边界）

本地状态与券商状态严格分开：

```
RESERVED -> VALIDATING -> READY_TO_DISPATCH
         -> DISPATCH_INTENT_RECORDED   # 与 order_dispatches 同事务写入
         -> ACCEPTED / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED / REJECTED
         -> SUBMISSION_OUTCOME_UNKNOWN -> 按 client_order_id 对账
NOT_SENT    # 仅用于 dispatch 之前的确定失败；可证明没有 POST
RECONCILIATION_REQUIRED  # 本地/券商事实冲突，冻结新风险
```

关键约束：

- **预发送阶段**（`RESERVED`/`VALIDATING`/`READY_TO_DISPATCH`）：暂停、窗口、报价、资产、可卖数量等检查失败时，记录 `NOT_SENT`，**不查询券商**，不污染安全锁。
- **dispatch 边界**：`DISPATCH_INTENT_RECORDED` 与 `order_dispatches` 同一 SQLite 事务；之后任何异常都不能被简单判定为“未发送”。
- **POST 超时/断网**：保持 `SUBMISSION_OUTCOME_UNKNOWN`，按稳定 `client_order_id` 查询；不重复 POST。404 不等于未发送。
- **看门狗**：只读取/对账，不自动撤 DAY 或止损；启动前用 `recover_stale_leases()` 把**dispatch 之前**的陈旧本地预留安全标记为 `NOT_SENT`。对刚记录 dispatch 的订单有 30 秒查询宽限，避免把在途 POST 误判为未知。
- **人工恢复**：未知订单可在人工核对 Alpaca 后执行：
  ```powershell
  ./run.ps1 resolve-order -Cid aip-... -Reason "checked Alpaca by client_order_id" -Confirm
  ```
  只允许 `DISPATCH_INTENT_RECORDED`/`SUBMISSION_OUTCOME_UNKNOWN`，记录 `operator_resolution` 审计，不需要手改 SQLite。
- **看门狗健康**：`runtime_health` 记录心跳、最近成功、连续失败、alive；循环级异常隔离，连续失败 3 次进入 `WATCHDOG_UNHEALTHY` 安全冻结；看板显式展示线程失效/无心跳。

## 3. AI 输出协议（v2）

`decision_schema.py` 使用严格 Pydantic（`extra=forbid`）：

- `PREMARKET_PLAN`：`candidates[]`，每项 `setup_type` / `opportunity_grade` / `trade_decision` / `plan_id` / `plan_valid_until_utc` / `triggers[]` / `order_intent` / `invalidation`。
- `UNIVERSE_SCAN`：固定池轻扫候选，不发单。
- `INTRADAY_RECHECK`：`CONTINUE/REPLACE/INVALIDATE/NO_TRADE`；`REPLACE` 必须带完整 `order_intent`。
- `POSITION_REVIEW`：Schema 已定义（HOLD/REDUCE/SELL/ADJUST_STOP/REMOVE_STOP/NO_STOP），当前引擎将持仓复审合并在 `PREMARKET_PLAN` 的账户上下文和候选决策中；独立调用尚未接入。
- `RESEARCH_REFRESH`：事实/解释/假设/未知/关键价位/证据/过期时间；来源不可用时明确降级。

严格校验包括：整数正股、DAY LIMIT、两码价格精度、禁止 notional/IOC/市价/扩展时段/额外字段、`NONE` 不得带止损、`BROKER_STOP` 必须有低于买单限价的止损且只允许 BUY、计划必须未过期、股票必须在固定池。非法 JSON 只允许一次有边界、有审计的修复重试，不能借修复绕过 schema 或风控。

Prompt 文件：
- `prompts/premarket_plan.txt`
- `prompts/universe_scan.txt`
- `prompts/intraday_recheck.txt`
- `prompts/position_review.txt`
- `prompts/research_refresh.txt`

旧 `prompts/decision.txt` 仅保留为历史 v1，不再被运行代码读取。

## 4. AI 输入、Token 与数据

- 盘前全池轻扫使用**原始结构但有界**的日线/周线/月线压缩窗口；重点候选再使用近 60 根日线 + 26 周 + 12 月 + 78 根 5 分钟原始 K 线。保留价格结构，不只给几个指标。
- `DeepSeek` 记录：模式、模型、prompt hash、input hash、output hash、prompt/completion tokens、延迟、finish_reason、结果；AI 输入以压缩 blob 保存（`event_blobs`），可复现且不膨胀主表。
- 外部新闻/SEC/搜索：DeepSeek 官方 Search/Fetch 已真实调用验证（`POST /v1/tools/search`、`POST /v1/tools/fetch`）；SEC/新闻专用适配器仍以搜索结果为准，不伪造来源。来源不可用时明确标 `SOURCE_UNAVAILABLE`。

## 5. 配置与安全重启

- `config/execution.yaml`：时段、截止缓冲、DAY/limit 契约、`submission_grace_seconds`、触发轮询、复核冷却/预算、看门狗间隔、数据并发、AI token/温度、`allow_broker_stop`/`broker_stop_verified`。
- `config/risk.yaml`：保留的非仓位型保护（当日亏损 2%、每日下单尝试 10、K 线/报价新鲜度、价差/偏离保护）。
- `config/strategy.yaml`：当前运行策略文本；`config/user_strategy.md` 与工作区 `策略.txt` 仅作历史资料。
- **运行中配置不可热改**：worker 启动时把配置哈希写入 `active_config`；磁盘文件变化时只设置 `config_pending_restart=true` 并冻结新风险，不会部分模块生效或显示未采用的配置。使用看板“安全重启”按钮（要求没有未知/冲突订单；重启后保持暂停，再单独 resume），或先 `pause` 再停止/启动进程。

## 6. 本地网页（127.0.0.1:4200）

启动：双击工作区 `启动交易台.bat`，或 `ai-paper-trader/Start-Paper-Trader.bat`。**普通启动脚本只启动本地网页，不会自动启动或恢复 worker。** 服务仅监听 loopback；需要手动点击“启动机器人”（仍保持暂停），再在安全门槛通过后点击“恢复交易”。

### 外部运行库与备份

- 当前运行库在 **`D:\alpaca-ai\ai-paper-trader-runtime\paper.sqlite`**，不在 Git checkout 内；项目 `data/paper.sqlite` 是保留副本，不要再作为运行库修改。
- 备份在 **`D:\alpaca-ai\ai-paper-trader-backups`**；`latest.json` 记录 SHA256、UTC 时间和核心表计数；SQLite backup API 一致性复制，先写 `.partial`，完整性检查通过才发布。
- Windows 任务 `AlpacaAIPaperDatabaseBackup` 每 15 分钟运行；同一电脑/磁盘备份，不防整盘损坏，也没有异地备份。
- `main.py paper` 启动前会先做一次验证备份；备份失败则不启动策略线程。

### 自动启动与交易日调度

- `tools/paper_supervisor.py` 计算美东 NYSE 交易日和开盘时间，自动适配 DST。
- Windows 任务 `AlpacaAIPaperSupervisor` 每日 **20:30（Asia/Shanghai）** 启动 supervisor；它在 **08:55 ET** 开始检查已有 `autopilot=true` 授权，暂时暂停 worker，重启已有 worker 进程树以加载本地新代码。初始化心跳及独立券商对账均健康后立即恢复，正常情况下在 **09:00 ET 前**完成。supervisor 不会自行把已关闭的 autopilot 打开。
- **09:00–09:30 ET**：LumiBot 盘前钩子与独立盘前调度共用防重入口；漏掉钩子、09:02 才就绪或稍晚启动，仍可补执行。失败任务按 300 秒冷却、每日最多 5 次实际尝试；成功阶段不会重复调用 AI。09:30 起不再补分析，也不因健康检查迟到而自动解除暂停。
- 盘前调度不执行主动交易 tick；它遵守暂停、冻结、健康检查和交易日历。初始化/后台心跳不是券商连接成功的证据，watchdog 只在对账无错误/冲突/未知状态时记录成功。
- 09:00–09:30 ET：worker 的 `before_market_opens` 完成 AI 盘前计划（失败最多 6 次、有界重试）。
- 09:30–10:00 ET：Engine 监测 AI 授权触发；AI recheck 失败不会消耗触发，下个冷却窗口可重试。
- 10:00 ET 后：本地不新增订单，不强制平仓/撤单。
- 手动 `pause` 会关闭 `autopilot`，supervisor 下一交易日不会擅自恢复。
- `tools/start_supervisor.ps1` 可由任务或手动启动；`tools/install_supervisor_task.ps1` 注册每日任务。

### 网络中断/关机后的自动恢复

- `tools/paper_recovery.py` 由 Windows 任务 `AlpacaAIPaperRecovery` **每 2 分钟**检查一次；另在用户登录启动目录放置 `AlpacaAIRecovery.cmd`，用于电脑重启/重新登录后的首次恢复。
- 只有在持久化 `autopilot=true` 时才动作；手动 `pause/stop` 会写入 `autopilot=false`，恢复任务不会覆盖人工停止。
- 访问 Alpaca Paper 失败时只记录并退出，等下一次网络恢复后重试；不会在断网时猜测订单状态。
- 只自动清除 `WATCHDOG_UNHEALTHY` / `STARTUP_RECONCILIATION_UNAVAILABLE` 这类网络/对账造成的冻结，且必须先做全新只读账户读取和干净订单对账。
- `ORDER_UNKNOWN`、券商不一致、数据库恢复冻结等状态保留给人工；恢复脚本只观察和对账，不提交、不撤销、不平仓。
- 网络恢复后最多约 2 分钟恢复 worker；电脑重启后由登录启动项触发，再按同一安全门槛恢复。

看板展示：会话阶段（ET）、配置哈希/待重启、worker 与看门狗健康、账户/非杠杆现金、持仓及止损状态、DAY 订单的本地状态与券商状态、当日计划/触发、AI 调用 Token/延迟、研究档案、重要事件。`NONE_BY_AI` 显示为“AI 选择不设”而不是系统故障；`BROKER_STOP` 未验证时显示 UNVERIFIED 并提示该意图会被拒绝。

> 本仓库修改后，**已在运行的旧 dashboard 进程不会自动加载新代码**；需要安全重启网页服务。启动脚本会复用已运行服务；升级后请先停止旧 dashboard 再重新启动。worker 代码更新后同样需要安全重启。

## 7. 安装与命令

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
.venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m pytest -q
```

```powershell
./run.ps1 check                 # 只显示配置哈希/凭证是否存在，不输出值
./run.ps1 config                # 当前磁盘配置快照
./run.ps1 preflight             # 只读 Alpaca Paper + IEX 数据检查
./run.ps1 dry-run               # 完全离线、合成数据、独立 dry-run.sqlite
./run.ps1 pause                 # 持久化暂停新订单
./run.ps1 resume                # 安全门槛通过后恢复
./run.ps1 acknowledge-safety    # 仅确认已人工核对，不自动恢复
./run.ps1 resolve-order -Cid ... -Reason ... -Confirm
./run.ps1 status                # 有界查询，不加载全部历史
./run.ps1 paper                 # LumiBot 常驻；当前默认暂停
```

研究档案首次分批构建（会调用 DeepSeek，**不会**下单；需要显式执行并自行承担 API 费用）：

```powershell
.venv/Scripts/python -m trading_bot.main research-bootstrap --symbols CRWV NBIS AMD --batch 3
```

## 8. 验证等级（不得混为一谈）

1. **单元测试通过**：状态机、Schema、风控、研究、SQLite、看板。
2. **离线集成测试通过**：无 socket、合成行情/券商/AI；premarket → trigger → DAY 订单 → 10:00 后不撤 → 次日对账。
3. **真实 API 只读验证**：`preflight` 于 2026-11-05 实际读取 Paper 账户、IEX 5 分钟线与日线；账户已有持仓、无开放订单；具体余额和持仓不在仓库文档中记录；**没有提交订单**。
4. **Paper 实际订单生命周期验证：未完成**。本版本没有发出任何订单；部分成交、撤单竞态、真实 DAY 下午成交、真实券商止损组合均未验证。
5. **长期无人值守运行：未验证**。

## 9. 当前未实现 / 限制

- `BROKER_STOP` 的 Alpaca OTO 请求构造与默认拒绝路径已实现并有离线测试；**该 Paper 账户是否支持所需 OTO/DAY/部分成交组合未验证**，默认禁用，不能据此认为可用。
- 独立的 `POSITION_REVIEW` AI 调用、`ADJUST_STOP`/`REMOVE_STOP` 的券商改单/撤单流程未接入；当前持仓复审依赖盘前计划上下文，不会自动删除已有止损。
- DeepSeek Search/Fetch 已真实接入并验证；SEC/新闻专用抓取仍以搜索结果为准，未实现额外供应商适配。
- 全池首次深研由 `research-bootstrap --all` 显式分批执行；后台批量运行会记录每个结果的来源与失败原因，不会伪造。
- 看板/worker 的“安全重启”是进程级；Windows 被强杀时仍以 broker 事实为最终依据，次日必须先对账。
- 止损不能保证跳空/极端流动性下的成交价，不能描述为亏损上限。
- 2% 当日亏损和 10 次下单尝试是沿用的非仓位型保护，尚未独立评估最优性；减少风险的合法卖出不受它们阻断。
- 所有本地进程/网页关闭后没有云端监控；10:00–次日盘前不保证发现异常或自动保护。
- IEX 仅覆盖 IEX，不代表全市场成交量/NBBO；缺数据只排除该标的，不编造。2026-11-05 全池只读测量为 48 只约 8.4 秒，46/48 通过六个月完整日线校验；`SKHY`、`SPCX` 因历史不足被排除。非交易时段报价会因不新鲜被拒绝，这是有意的 fail-closed。

## 10. 密钥与仓库保护

- 本地密钥文件在仓库外并由用户本地管理，不进入 Git；仓库跟踪文件中没有 key/.env/数据库/日志。
- 日志/事件写入前做递归脱敏；测试验证 `Authorization`、嵌套 key 和环境密钥不会出现在事件/日志。
- `tools/untrack_runtime.py` 已改为同时取消 `data/`、`logs/`、`test-results/`、`.pytest_cache/`、`htmlcov/`、`__pycache__`、SQLite/日志/覆盖率产物的**跟踪**，只保留磁盘内容，不动 Git 历史。发布前必须在远端对应 checkout 运行；不要直接覆盖 `main`。
- 看板是 loopback-only，`/api/status` 会返回一次性 control token；只允许本机访问，不应把 4200 暴露到局域网。

## 11. 一键启动与 AI 互动台（2026-11-06）

双击仓库根目录 `Start-Paper-Trader.bat`。本机上层目录的 `启动交易台.bat` 已转发到该入口，无需另开 PowerShell。
启动器复用已有网页服务；首次启动最多等待 120 秒，确认本地服务就绪后才打开 http://127.0.0.1:4200 。重复点击由启动互斥锁阻止重复启动。失败窗口保留错误提示，可检查 `logs/dashboard-error.log`。网页就绪不代表 Alpaca 或 DeepSeek 已连接，请查看页面各自的状态。

启动器只负责 Dashboard，不自动启动或恢复交易，也不停止现有 worker。关闭启动器窗口后后台网页服务继续运行。升级后的 Python 代码仍需重启对应服务才能加载。

右下角 AI 互动台提供快捷问题、历史对话和展开回复；默认回复不超过 150 字，展开不超过 400 字。它复用 DeepSeek 和数据库中的持仓、订单、计划、研究档案及已保存新闻，明确缺失和过期信息，不会独立获取全天候新闻，也没有下单、撤单或修改交易计划的权限。

真实决策、成交和风险事件触发主动消息，普通事件合并并去重，普通消息间隔至少 5 分钟、重要消息至少 60 秒；每日最多 24 次主动生成尝试，失败有限重试。对话与主动消息保存于本地 SQLite，浏览器关闭不影响留存，但 Dashboard 服务必须运行。聊天调用与交易 AI 调用分别记录。

## 官方核查入口

- Alpaca Orders: <https://docs.alpaca.markets/us/docs/orders-at-alpaca>
- Alpaca Paper: <https://docs.alpaca.markets/us/docs/paper-trading>
- Alpaca Market Data: <https://docs.alpaca.markets/us/docs/about-market-data-api>
- DeepSeek JSON: <https://api-docs.deepseek.com/guides/json_mode/>
- DeepSeek Search/Fetch: <https://api-docs.deepseek.com/guides/search/>
- SEC EDGAR API: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
