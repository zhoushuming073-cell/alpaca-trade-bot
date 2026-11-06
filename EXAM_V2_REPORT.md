# 历史考试数据库 V2 修复与隔离验收

日期：2026-11-06。范围：2026-08-13—2026-11-05 的 60 个 NYSE 完整交易日、固定 48 只股票及仅作市场参考的 QQQ。**本轮没有调用 DeepSeek 生成历史决策，没有运行回测，也没有提交任何真实或 Paper 订单。**

## 结论

| 检查 | V1 | V2 | 说明 |
|---|---|---|---|
| 当前账户可获得的盘前分钟行情 | **FAIL** | **PASS（按 IEX 源及保守延迟规则）** | V1 的 60 个 09:00 快照含 31,734 条最近 15 分钟的 SIP 分钟线；V2 重新获取 IEX，库内 SIP 行为 0。STRICT 另对完成的 IEX 分钟线施加 15 分钟保守延迟，60 日排除 1,471 次展示。历史账户订阅状态仍为 UNKNOWN。|
| SPCX 证券身份 | **FAIL** | **PASS（已确认的旧代码段）** | 从 V2 排除 IPO 前 82 条旧 ETF / 停用段日线，SPCX 的历史可用起点为 2026-06-12；周线、月线由过滤后可见日线重算。V1 未改。|
| 09:00 时间边界及未来数据注入 | PASS | **PASS** | STRICT 与 EXPLORATORY 各 60 个 09:00 快照；未来行情、新闻、财务数值、收益标签注入后完整快照和历史 AI 输入哈希 **120/120 不变**。另检查盘中 09:35、09:35:30 边界。|
| 新闻正文、SEC 财务数值的原始历史版本 | UNKNOWN | **UNKNOWN** | 严格模式不提供这些内容；探索模式只以 UNKNOWN 标签提供。未把来源时间戳当成历史原稿证明。|
| 其他股票的完整历史身份、停牌及供应商修订 | UNKNOWN | **UNKNOWN** | 47/49 个序列仅有 IEX 观察证据，SKHY 代码变更来自现时企业行动 API；没有逐日历史证券主数据或原始接收日志。|
| 历史 AI 输入与生产长期记忆隔离 | 无独立输入构造器 | **PASS（构造路径）** | 历史提示词只由 V2 快照、显式模拟账户和静态策略文字生成；测试拦截生产 SQLite 和 HTTP 访问。正式 DeepSeek 模型及其预训练知识尚未验证。|

**总体状态：确定性的 V1 故障已修复；V2 可作为有明确 UNKNOWN 标记的历史考试数据源，但尚不足以声称严格历史真实性已完全验证，亦不应现在启动正式 60 日多空 AI 考试。**

## 数据差异与覆盖

| 指标 | V1 SIP 主序列 | V2 IEX 主序列 |
|---|---:|---:|
| 日线（含考试日前回看） | 8,915 | 8,833；差额正是 SPCX 旧身份 82 条 |
| 全部分钟线 | 1,735,677 | 1,026,162 |
| 60 日正常时段分钟线 | 1,144,865 / 1,146,600（99.85%） | 1,016,363 / 1,146,600（88.64%） |
| 正常时段空分钟 | 1,735 | **130,237**（11.36%） |
| 盘前分钟线 | 587,891 | **9,663** |
| 49 个序列 × 60 日中正常时段完全无数据 | 0 | 0 |
| 09:00 快照当日分钟线展示次数 | V1 审计中为 513,457 | STRICT 3,803；EXPLORATORY 5,274 |

IEX 是生产机器人目前使用的免费实时源，但仅覆盖 IEX 成交，不能把它与全市场 SIP 的成交量或分钟完整性等同。缺失分钟没有插值，也没有填零。正式模拟成交若只依赖 IEX，触价和成交判断会偏稀疏，尤其盘前研究。[Alpaca Basic 市场数据说明](https://docs.alpaca.markets/us/docs/about-market-data-api)、[SIP 最近 15 分钟权限说明](https://docs.alpaca.markets/us/docs/market-data-faq)。

V2 仍保存原始来源、首次发布时间、更新时间、抓取时间、内容哈希和状态标记：新闻 7,261 条、SEC 公告元数据 767 条、CompanyFacts 3,491 条。STRICT 的 60 个 09:00 快照展示 **0** 条未经历史版本核验的新闻正文、**0** 个财务数值，但展示 39,107 次已按 SEC 接收时间过滤的公告元数据。EXPLORATORY 展示 190,537 次新闻及 172,219 次财务事实，均标为历史内容/数值版本 UNKNOWN；这是跨日展示次数，不能当作独立文章数量。46/48 只股票有采集到的 SEC 数值，且严格模式不会使用这些数值。

## 证券身份与企业行动

旧 `SPCX` ETF 代码段的识别依据是 [ETF 的 SEC 改代码文件](https://www.sec.gov/Archives/edgar/data/1719812/000199937126007611/cist-497_040226.htm)与 [SpaceX 官方 IPO 公告](https://ir.spacex.com/updates/releases-details/2026/Space-Exploration-Technologies-Corp--Announces-Pricing-of-Initial-Public-Offering/default.aspx)。V2 将 SpaceX 的 `SPCX` 身份限定于 2026-06-12 起；旧 ETF 和后续零量旧代码日线均不能进入其回看。`SKHY` 的旧 `SKHYV` 代码变更保留现时 API 证据和 UNKNOWN 时点风险，缺失的旧代码历史未填造。

现时 Alpaca 企业行动接口返回 40 项：36 项现金分红、2 项名称/代码变更、2 项并购；没有返回拆股记录。数据使用 `adjustment=raw`。这说明分红及并购可能影响价格连续性，**不证明**历史中绝无拆股、停牌或其他身份变动。Alpaca 明示企业行动记录的创建时间不保证，因此这些记录不能直接作为历史决策时点的可见公告。[Alpaca 企业行动 API](https://docs.alpaca.markets/us/reference/corporateactions-1)。历史行情采集的 `asof=-` 跳过证券身份映射，也是旧 SPCX 污染的直接技术原因；[Alpaca 历史行情文档](https://docs.alpaca.markets/us/reference/stockbars)说明了该语义。

## 历史 AI 隔离实现

- `src/exam_v2/snapshot.py` 提供 `get_v2_snapshot(date, asof, symbols, mode=...)`。仅接受版本标记正确的 IEX V2；日线按收盘可用时间，分钟按完成时间和延迟过滤，周/月线只从可见、身份有效的日线重算。QQQ 自动作为市场参考，不能进入交易股票池。
- `src/exam_v2/prompts.py` 仅从该快照、明确传入且时间戳**恰好等于历史截止时刻**的隔离模拟账户、静态策略文字及生产 JSON 输出模板构造完整 system/user 输入。输入包含 QQQ 历史行情，显式标记现时固定股票池与现时策略的事后选择偏差。无默认“每天重置”的假历史持仓。
- 生产 `Engine._base_payload()` 会读现时 `research.summaries()` 和 `latest_news()`；`main.py` 的研究路径还可使用联网搜索。因此生产 Engine **不能直接**用于历史考试。新构造器没有导入该研究管线；测试把生产 SQLite 和 HTTP 访问设置为立即报错，仍能生成提示词。这个结果证明当前构造路径的代码隔离，不证明未来第三方 AI 调用或模型预训练知识绝对无泄漏。
- `src/exam_v2/ai_ledger.py` 为两种模式分别准备 SQLite 调用账本，保存精确 system/user 输入、输入与快照哈希、模型配置、调用和完成时间、原始输出、解析结果或错误。默认没有 DeepSeek 网络客户端，未来调用需显式授权并注入无工具的传输函数；配置字段白名单拒绝凭据及工具配置。本轮只用假传输做离线测试，**账本没有真实 AI 决策记录**。
- `src/exam_v2/simulation.py` 提供与 Alpaca 完全隔离的整数股限价单模拟接口：多/空开平、减仓、部分成交、日单到期、手续费、滑点、参与率、隔夜借券费以及有日期的借券许可。无历史借券证据时拒绝开空。`src/exam_v2/evaluation.py` 提供多空及整体收益、回撤、夏普、胜率、盈亏比、持有时间、资金利用率以及 QQQ 与固定股票池简单基准的纯计算函数。**接口和单元测试不是已完成的 60 日回测。**

## 验证与可复现文件

- 全量审计：`data/exam_v2_audit/audit_results.json`；逐日双模式哈希：`data/exam_v2_audit/all_60_hashes.json`。
- 供人工核对的完整 09:00 AI 输入：`data/exam_v2_audit/2026-08-27_0900_{strict,exploratory}_ai_input.json`、`2026-09-25...`、`2026-11-05...`，分别覆盖重大新闻、普通和高波动样本。
- 审计结果：60/60 日双模式快照，时间/身份检查 **0** 项失败；隔离库未来注入 **0/120** 哈希差异；盘中边界测试 **0** 失败。源 V1、V2 在审计前后哈希各自不变。
- 完整离线测试：`python -m pytest tests -q -p no:cacheprovider` → **284 通过、0 失败、0 跳过**，另有 1 条第三方 `websockets` 弃用警告。V2 专项测试 10 项通过。
- V1 原数据库、审计文件及新清单共 14 份已逐文件记录哈希并设为本机只读，清单位于 `data/v1_original_audit.manifest.json`。V1 SHA-256：`794b7dba40fde9a79446b4f766a656d9bbdb7f33980b6e49666db0f9c177b09c`。V2 冻结清单位于 `data/exam_v2.manifest.json`，SHA-256：`7ca7a83932420eeeffb78f5a17b77940252b02fe21ab33ba488399a1da14c502`。V2 数据库位于 `data/exam_v2.duckdb`，含 `freeze_version` 写保护标记。两个数据库与生产订单 SQLite 完全隔离。

最小只读调用：

```python
from exam_v2.snapshot import ExamMode, get_v2_snapshot
from exam_v2.prompts import build_exam_prompt

day = "2026-09-25"
asof = "2026-09-25T09:00:00-04:00"
snapshot = get_v2_snapshot(day, asof, ["AMD"], mode=ExamMode.STRICT)
simulated_account = {
    "source": "ISOLATED_SIMULATOR", "asof_utc": "2026-09-25T13:00:00+00:00",
    "cash": 100000.0, "equity": 100000.0, "positions": {}, "open_orders": [],
    "historical_short_locates": "UNKNOWN",
}
prompt = build_exam_prompt(day, asof, ["AMD"], simulated_account=simulated_account)
```

## 尚未解决的 UNKNOWN 与下一阶段阻断

1. 历史 IEX 行情为事后抓取，原始到达时间和供应商修订前的价格版本未存档。STRICT 的 15 分钟盘前缓冲是保守规则，不是原始接收日志证据。其 09:00 可见价格的**时间边界已验证**，内容逐笔历史一致性仍为 UNKNOWN。
2. 新闻原始历史正文版本和 SEC 数值历史版本不能独立核验；STRICT 已排除，EXPLORATORY 只可做探索性对照。SEC 公告仅限接收时间元数据，公告正文不在 STRICT 中。
3. 其余证券的逐日身份、停牌、历史借券许可及券商保证金条件缺档。当前 48 股股票池和策略均是事后选定；60 日窗口过短，不能证明全市场选股能力或统计显著性。
4. **正式多空考试阻断**：历史借券许可没有可核验的逐日来源；模拟器因此拒绝没有证据的开空。生产提示词仍含 Alpaca Paper 的空头权限表述，正式考试前还需用隔离模拟权限替换该表述并验证生产决策到模拟执行的端到端适配。当前只复用静态策略与生产输出 schema，未直接运行生产 Engine。
5. **模拟成交精度限制**：IEX 正常时段空分钟 130,237 根，无法由现有数据严格重建全市场触价、排队和实际成交。若下阶段接受 IEX 近似回放，结果只能标成探索性；若要更严谨，需要另设计只供未来价格回放、绝不进入 AI 决策输入的允许数据源及其成交模型，并记录误差假设。不得把当前 IEX 模拟结果称为 Alpaca 真实成交验证。
6. 模型预训练知识可能包含考试日期之后的事实；封闭输入和禁止搜索降低外部泄漏，但不能证明模型内部没有未来知识。需要在正式考试记录和评价时保持 UNKNOWN 标记。

**下一步状态：等待用户授权；本轮未启动正式 60 日历史 AI 考试。**
