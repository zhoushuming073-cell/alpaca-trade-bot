# 纸账户交易机器人修复交付（2026-11-05）

本次完成的是代码修复、离线故障注入和模拟闭环验证。**没有向 Alpaca 实盘或 Paper 账户提交任何订单，没有调用真实 DeepSeek 做交易决策，没有恢复机器人交易，也没有推送 GitHub。**

## 验证结果

- 修复前基线：60 passed，0 failed，0 skipped。
- 修复后完整套件：**180 passed，0 failed，0 errors，0 skipped**；pytest 用时 8.83 秒。
- 1 条第三方 `websockets.legacy` 弃用提示，非测试失败。
- `pip check`：No broken requirements found。
- `compileall`：通过；`git diff --check`：通过。
- 测试统一禁止 socket 连接；订单、行情、时钟和 DeepSeek 响应均用 Fake/Mock。未使用真实凭据进行这些测试。
- JUnit 明细：`test-results/pytest.xml`；独立模拟结果：`test-results/offline-e2e.json`（运行产物不提交）。
- 本地网页已仅重启网页服务，仍为 `http://127.0.0.1:4200`。实际状态 API：`running=false, paused=true, ioc_permission_confirmed=false, unresolved_count=0`。这里的未决数是本地审计库状态，不能替代实时券商确认。

| 测试文件 | 通过数 |
|---|---:|
| test_system.py | 42 |
| test_adapter.py | 4 |
| test_dashboard.py | 3 |
| test_opening_window.py | 7 |
| test_six_months.py | 3 |
| test_watchdog.py | 1 |
| test_safety_repair.py（新增） | 90 |
| test_wire_guards.py（新增） | 29 |
| test_repository_cleanup.py（新增） | 1 |
| 合计 | 180 |

原有测试没有因难以修复而删除；将 DAY 订单、29:30 停止新单、整批数据失败等旧预期调整为本次明确要求的新行为。

## 核心修复与对应验证

| 问题 | 实现与验证 |
|---|---|
| 亏损/次数熔断阻止 SELL | 仅对增加风险施加日亏损、次数、单股、总仓位上限。loss/count/overweight/total/all 五组减仓测试通过。20%→15% 卖出 50 股通过。 |
| 无条件放行 SELL 的风险 | 仍执行暂停、窗口、策略条件、账户状态、真实多头、可卖整股、挂单占用、重复编号检查。卖出目标不得增加或维持原敞口；已有卖单计入目标，避免重复减仓。 |
| 碎股/notional | 金额换股向下取整，不足 1 股跳过；已有小数残余只报告。执行入口、Gateway、LumiBot 适配和最终 HTTP 请求都校验；SDK 内部浮点数量在发送前规范为整数字符串。小数、0、负数、bool、NaN、Infinity、notional、DAY/market 等测试通过。 |
| 长期挂单与截止 | 唯一允许整股 IOC LIMIT；不回退 DAY、FOK 或市价。不支持/拒绝触发持久化安全锁。HTTP 最后一关检查窗口、暂停、批准内容、报价有效期；29:30、29:59、30:00、跨截止慢请求、截止后撤单均有测试。 |
| POST 超时重复下单 | 先持久化周期和唯一 client_order_id，再记录一次性 dispatch。响应丢失后按编号查询；恢复或重复周期不重发。真实 alpaca-py 序列化路径加模拟传输验证只发送一次 POST。 |
| 撤单超时/断网 | DELETE 成功仅表示已请求撤单，必须查询终态。无法确认即 reconciliation_required，持久化告警和暂停。超时但实际已取消/仍 pending_cancel、断网再恢复均测试通过。 |
| 状态同步 | 保存请求、响应和累计成交变化；accepted/new/partial/pending_cancel 等保持未决。unknown、无法识别、replaced/suspended/calculated 等异常状态保持阻塞并要求核对。旧查询不会覆盖终态或降低累计成交；跨 Store 连接以 SQLite 写锁保护比较和更新。 |
| 重启/数据库不一致 | 重启按持久化编号恢复；预约后未 POST 的崩溃也不猜测重发。券商发现 aip- 前缀孤儿订单时导入并暂停；本地终态与券商活跃状态矛盾时重新置为待核对。 |
| 单股行情异常 | 分钟线和日线分别捕获并记录。48 只中 1/3/48 只异常、日线正常分钟缺失、持仓异常全部覆盖；AI 只收到有效标的的数据及排除原因。不补零、不伪造 K 线。 |
| 持仓数据异常 | 保留完整持仓，冻结异常持仓标的，阻止所有 BUY；其他有有效数据的合法 SELL 可继续，禁止把排除视为清仓。 |
| DeepSeek | 原始内容先脱敏入库，再解析；非法 JSON、超时、重复/池外股票、超限权重均被安全处理。有效 Mock 响应通过完整流水。 |
| 不必要轮询 | 本地时间、暂停、周期去重先检查。LumiBot 自身账户同步也有缓存；引擎风控使用强制刷新，未决订单查询不缓存，独立监控不受暂停/休市影响。 |
| 历史数据/适配 | 保留六自然月完整日线和 40 根完整 5 分钟线；验证日线内部缺失、OHLC、时区、夏令时、休市及提前收市。实测 LumiBot Strategy 层确实请求原生 5 分钟，而非改成 1 分钟重采样。 |
| 审计与保密 | 错误有时间/时区、阶段、股票、周期、类别、类型、具体脱敏原因、重试和结果；数据库保留 raw AI/解析、风险、意图、状态及累计成交。嵌套凭据、Bearer 和环境凭据脱敏测试通过。 |
| 网页安全锁 | 网页显示未确认 IOC、安全原因和详细错误。恢复按钮不能绕过未决订单、IOC 配置或安全锁；CLI 同样检查。人工确认异常不自动恢复交易。 |

## 离线端到端结果

使用实际 48 个固定池代码，但价格、持仓、券商、AI 响应完全合成，不代表策略收益：

1. 每个有效标的提供 126 根日线（该模拟日期之前的六自然月）和 40 根完整 5 分钟线。
2. 故意让 SOFI 缺失分钟数据；其余 47 只进入 AI 决策，排除原因完整入库。
3. 合成 BUY 目标换算为 5 股 IOC，模拟成交 2 股、剩余取消。
4. 重复执行同一周期，提交总数保持 1。
5. 关闭并重新打开 SQLite，在下一个周期合成 SELL，卖出已持有 2 股。
6. 30:00 后拒绝新策略交易；在暂停状态下撤销模拟遗留 DAY 订单。
7. 最终模拟提交 2 次，实际外部提交 0 次，最终持仓 0，未决订单 0。
8. 审计记录包含 2 次 raw AI、2 次解析决策、94 次风险结果、2 次委托请求、4 次订单状态记录、2 条累计成交记录和 6 次账户快照。
9. 控制台出现的两条 “Injected missing intraday bar” 是预期故障注入，最终结果断言全部通过。

重跑：

```powershell
.venv/Scripts/python.exe -m pytest -q --junitxml=test-results/pytest.xml
.venv/Scripts/python.exe -m trading_bot.offline_validation --output test-results/offline-e2e.json
```

## IOC/FOK 核实与未验证边界

- 本地已安装 LumiBot 4.6.46、alpaca-py 0.44.0；测试使用真实 SDK 请求模型，确认生成 Paper **v2** IOC LIMIT HTTP 请求。
- Alpaca 官方订单文档对整股 LIMIT 的 IOC/FOK 支持带星号，并提示联系 sales。SDK 枚举存在不等于这个具体 Paper 账户有权限，**本次没有实际验证该账户权限**。
- IOC 可立即部分成交并取消余量，符合需求；FOK 要求立即全额成交，会丢掉可接受的部分成交机会，因此不选用。
- `config/execution.yaml` 默认 `ioc_permission_confirmed: false`。确认权限前禁止新委托；拒单后重新锁定，不能默默回退。
- IOC 可以消除通常的 DAY 长期挂单问题，但不能保证所有成交时间绝对早于第 30 分钟：截止前在途 POST 可能延迟到达券商；网络中断也无法保证撤单及时确认。系统只保证在本地最终发送检查时禁止截止后的新指令，并对不确定状态持续告警。若要求券商撮合时间绝对不能越界，需要券商支持的服务器端截止机制，不能用本地撤单承诺替代。
- 本地时间与券商 clock 超过 5 秒偏差会拒绝交易。成交前实际资产、最新持仓可用量、报价、市场时钟和网络质量仍需下次开盘核验。
- 严格历史校验保留，因此免费 IEX 缺 K 线、IPO 不足六个月等情况会让一些股票长期被排除；不会为提高交易次数放宽或补造数据。
- 48 只股票初次历史加载和 DeepSeek 延迟可能让决策超过有效期，此时拒绝执行；真实全股票池性能仍待观测。
- 告警落在本地 SQLite、日志和网页，没有新增邮件/短信等外发渠道。
- `fills` 保存券商返回的累计成交数量/均价快照，可追踪数量变化；并不是每一笔交易所撮合的独立活动账单。
- 实际 Paper 成交、拒单正文、部分成交竞态、撤单竞态、断线恢复及长期运行均尚未实测。不以离线通过冒充真实闭环成功。

官方来源：[Alpaca Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca)、[Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)。

## 轮询与缓存

| 数据 | 缓存及强制刷新规则 |
|---|---|
| 引擎账户/持仓/开放订单 | 启动恢复、每个新决策周期、每笔风控前、成交后强制刷新；不拿展示缓存做风险决定。开放订单达到 500 条时因可能截断而拒绝执行。 |
| LumiBot 框架展示/同步的 account、positions、orders、clock | 活跃窗口 30 秒；暂停或窗口外 300 秒；新交易请求成功后清除缓存。 |
| 持久化未决订单 | 独立线程约每 10 秒查询并对遗留挂单请求取消，暂停和窗口外照常；网络耗时会延长实际间隔。client_order_id 查询永不缓存。 |
| 日线 | 仅缓存验证通过的已完成日线，至下一纽约日期；无效响应不缓存。 |
| 分钟线、下单报价 | 每个有效周期/委托重新读取。没有尚未收盘 K 线进入 AI。 |
| 网络重试 | GET 对网络/429/指定 5xx 最多 3 次尝试，退避 0.25/0.5 秒；POST/DELETE 不自动重试。TLS 验证保持开启，未更改 VPN/代理。 |

## 修改文件

| 文件 | 原因 |
|---|---|
| src/trading_bot/risk_manager.py | 区分增减风险、整股换算、真实可卖量与挂单目标占用。 |
| src/trading_bot/safety.py（新） | 统一订单合法性校验、IOC 异常类型、递归凭据脱敏。 |
| src/trading_bot/broker.py | IOC 同步 SDK 执行、HTTP 最终保护、一次性授权、强制刷新/缓存、原生 5 分钟行情。 |
| src/trading_bot/network.py（新） | 仅幂等读取的有限重试和退避。 |
| src/trading_bot/execution.py | 持久化编号、提交超时查询、状态核对、孤儿/矛盾订单处理。 |
| src/trading_bot/storage.py | 非破坏性建表、原始意图、dispatch、累计成交、持久化安全锁、详细审计。 |
| src/trading_bot/watchdog.py | 全天独立订单监控及安全撤单，不依赖暂停和策略窗口。 |
| src/trading_bot/engine.py | 先本地预检、按股票隔离、冻结异常持仓、风险/阶段审计、批准有效期。 |
| src/trading_bot/market_data.py | 精确 30 分钟边界，缺失数量/时间诊断，日线完整性和 OHLC 校验。 |
| src/trading_bot/strategy.py | 注入安全网关和审计、启动恢复、只缓存有效日线。 |
| src/trading_bot/deepseek_client.py、prompts/decision.txt | 原始输出与格式错误审计，告知有效股票、冻结持仓和整股规则。 |
| src/trading_bot/settings.py、config/execution.yaml（新） | IOC 权限显式配置，默认拒绝。 |
| src/trading_bot/preflight.py | 只读预检明确不代表执行已验证，补充 IOC 门槛及脱敏原因。 |
| src/trading_bot/main.py | CLI 恢复保护、仅确认不恢复、UTC 日志与脱敏。 |
| src/trading_bot/dashboard.py、src/trading_bot/reporting.py、web/app.js | 展示安全锁、详细原因、真实数据库状态；恢复按钮不能绕过。 |
| src/trading_bot/dry_run.py | 可模拟整股全成/部分成/不成/拒单及真实资金持仓变化。 |
| src/trading_bot/offline_validation.py（新） | 可独立重跑的断网 E2E，使用临时数据库。 |
| tests/test_safety_repair.py、tests/test_wire_guards.py（新） | 关键缺陷、故障注入、真实 SDK 序列化和 HTTP 保护测试。 |
| tests/test_repository_cleanup.py（新）、tools/untrack_runtime.py（新） | 只取消运行产物跟踪；保留磁盘历史数据库与日志，含中文路径测试。 |
| tests/test_adapter.py、tests/test_opening_window.py、tests/test_watchdog.py | 将旧 DAY/截止撤单预期更新为 IOC 和全时段安全监控。 |
| tests/test_system.py、tests/test_dashboard.py | 更新无有效数据跳过语义，IOC 恢复门槛测试。 |
| .gitignore | 扩展 SQLite/WAL/SHM、测试报告和覆盖率产物忽略规则。 |
| README.md、VALIDATION.md、REPAIR_REPORT.md（新） | 修正旧碎股/DAY/截止描述，记录本次验证与开盘计划。 |

## Git 与资料保护

- 初始本地工作树干净；基线为 `7614bf6`，在本地分支 `fix/paper-order-safety` 修复。
- 已读取远端 main `91c0da2083fc87b35ca7be58328920344f1ba13f`。24 个源码/测试文件与本地基线忽略换行后完全相同，未覆盖用户额外代码修改。
- Git HTTPS fetch 遇到连接失败，改用 GitHub 只读连接器核对远端；未修改代理。
- 远端确认仍包含 `data/paper.sqlite`、日志、`__pycache__`；当前本地分支原本未跟踪这些产物，清理工具运行结果为 0 个待取消文件。
- 本地历史数据库没有删除或重建；建表使用 CREATE TABLE IF NOT EXISTS。历史日志没有删除、改写或覆盖；重启网页使用新的带日期日志文件。
- 没有 reset --hard、强制推送、远端提交或历史改写。由于禁止自动推送，远端运行产物仍需在后续正常发布此修复时清理。
- `tools/untrack_runtime.py` 用 `git rm --cached` 仅取消 data/、Python 缓存等生成文件的跟踪；不删除磁盘内容，不动历史 logs/。可以在远端对应的本地 checkout 运行后审查再正常提交。
- 本次提交范围不包含凭据、.env、运行数据库、原始日志或测试运行产物。

## 下一次开盘最小验证方案（未执行）

1. **先确认具体 Paper 账户 IOC 权限。** 如果 Alpaca 不支持，不启用、不回退 DAY；停在安全模式并向用户说明。权限确认不能以 SDK 枚举代替。
2. 保持暂停，用现有 preflight 做只读连接检查：Paper v2、账户/持仓/开放订单、clock、IEX 分钟及六个月日线。检查所有未决订单与历史碎股残余，确保没有需要人工处理的异常。
3. 获得用户对当次 Paper 执行验证的明确授权后，才设置 IOC 权限确认、核对安全告警、按需执行 `acknowledge-safety`，再单独恢复。安全告警没有查清前不得确认。
4. 首先观察一轮自然策略决策，核对 AI 原始/解析结果、有效与排除股票、Python 风控、整股量及限价。**若 AI 为 HOLD，就记录未触发交易，不伪造信号以制造闭环。**
5. 对首笔获准的 IOC，逐项核对唯一编号、真实 broker ID、time_in_force、整数 qty、成交量、余量取消、账户变化及 SQLite 一致性。需要专门 1 股接口冒烟单时应另行授权并保留审计，本次没有发出或安排它。
6. 确认暂停后仍能同步既有订单，30:00 后无新策略 POST，且仍有安全查询/撤单。不要以主动断 VPN 的方式留下无人监控订单；网络竞态已先用离线故障注入覆盖。
7. 完成首笔后暂停并核对终态，必要时重启再核对。只有真实 Paper 返回与审计一致，才能标注该执行环节已验证；部分成交等未出现的场景继续标为未实测。

源码检查和离线测试未再发现可直接复现的阻断性工程缺陷；**当前实际启用的阻断条件仍是未确认 IOC 权限、保持暂停，以及真实开盘闭环尚未验证**。这些不是由离线测试可以消除的事实。
