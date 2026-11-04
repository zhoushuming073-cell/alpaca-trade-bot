# 美股 AI Paper Trader

## 一键启动与网页

双击工作区 `启动交易台.bat`，或本目录 `Start-Paper-Trader.bat`。启动本地网页和机器人，保留数据库中的暂停状态，不会自动解除暂停。重复双击会复用已运行服务。

网页地址：<http://127.0.0.1:4200>。仅在本机监听，不暴露到局域网。

网页每 5 秒刷新，显示进程存活、暂停状态、交易窗口、账户快照时间、净值轨迹、持仓、订单成交、AI 决策、事件和股票池筛选。没有最新账户快照时会标注旧数据。网页刷新不等同于实时请求 Alpaca 账户。

- **启动机器人**：启动进程，保留暂停状态。
- **恢复交易**：确认后解除暂停；仅 Paper 资金，在既定窗口内运行。
- **暂停下单**：持久化禁止新订单，保留截止撤单监控。
- **停止进程**：先暂停；有未决订单时拒绝停止，以保留监控。

关闭网页不停止后台进程。账户连接失败可能导致机器人退出，网页会显示进程未运行及异常类型；检查 `logs/worker-console.log` 后可再次启动。网页服务本身继续工作。原有命令行入口仍可使用。

基于 LumiBot 4.6.46、Alpaca Paper、DeepSeek Flash、SQLite。只连接 `https://paper-api.alpaca.markets`，不存在实盘模式或实盘回退。

## 当前用户配置

- 已从工作区 `stockpool.txt` 导入 48 个股票代码。
- `config/user_strategy.md` 保留用户策略原文，`strategy.yaml` 包含原文和后续修订。
- 使用最近 **6 个自然月完整日 K 线**，结合最近 40 根完整 5 分钟 K 线。历史不足、过期、缺失时安全终止该周期。
- 每 5 分钟重新分析。正常开盘后第 **5、10、15、20、25 分钟**是五个决策点；第 30 分钟起不再下单、撤单或调仓。
- 冬令时北京时间 22:30–23:00；夏令时 21:30–22:00。使用 NYSE 日历和 America/New_York 自动处理假日、夏令时及提前收市。
- 第 29 分 30 秒停止提交新订单并开始截止撤单。独立线程负责撤单，不受 DeepSeek 请求等待阻塞。网络故障无法保证券商已接受撤单；未确认订单保留为未决，自动暂停，必须人工检查纸账户。
- 没有联网新闻、行业行情、盘前涨幅数据。这些字段明确标为不可用，模型不得虚构；必要依据不足时必须 HOLD。

AI 分析定性的回撤反弹/箱体规则，Python 强制执行时间、股票池、账户和资金风险约束。`entry_checks/exit_checks` 可添加明确的数值条件；目前用户没有规定这些数值，不代为编造。AI 的 confidence 仅为主观评分。

## 安装和启动（PowerShell）

在本项目目录操作。已建立 Python 3.13.7 虚拟环境。重建环境：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
.venv/Scripts/python -m pip install -e . --no-deps
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pip check
```

完整依赖固定在 `requirements.lock.txt`，包括 Windows/Python 3.13 环境依赖，跨平台应另行验证。关键版本：LumiBot 4.6.46、alpaca-py 0.44.0、OpenAI 2.54.0、Pydantic 2.14.2、pandas 2.3.3。

```powershell
./run.ps1 check                 # 只显示凭证是否存在，不输出值
./run.ps1 preflight             # 只读账户、股票可交易性、IEX 历史行情
./run.ps1 dry-run               # 完全离线，合成 TEST 标的，独立数据库
./run.ps1 status -Mode dry-run
./run.ps1 pause                 # 紧急禁止新订单；截止撤单监控继续运行
./run.ps1 paper                 # LumiBot 常驻；暂停状态下保持不交易
./run.ps1 resume                # 在另一终端解除暂停
./run.ps1 status                # 纸账户记录、持仓、收益、订单、最近决策
```

正常退出使用 Ctrl+C；若宿主不能转发中断，可终止对应 Python 进程。停止进程不会自动清仓或撤销所有账户订单。存在委托时应让截止监控运行完毕，或在纸账户界面检查。不要停止后假定订单已取消。

`pause` 状态持久化，重启不会自动解除。实例文件锁阻止同一数据库的多个交易进程。重启重新读取账户、持仓、未完成订单，并按已持久化 client_order_id 查询。未知提交结果绝不直接重发。未知订单若长期查无记录，需人工核对后修复，程序宁可保持阻塞。

## 凭证

推荐复制 `.env.example` 到 `.env` 并在本地填写。不得提交 `.env`。程序只消费环境变量。

现有 `run.ps1` 可兼容读取项目上一级的 `api-key.txt` 与 `alpaca key.txt`，仅注入进程环境；不会复制、打印或上传密钥。环境中已有值优先。`alpaca key.txt` 支持 `Key: 值`、`Secret: 值`，其中 URL 文本不参与端点选择。`ALPACA_IS_PAPER=false` 会立即拒绝启动。DeepSeek 只接收策略和行情/持仓信息，不接收 Alpaca 凭证，也没有工具执行权限。

## 修改股票池、策略和风控

编辑 `config/watchlist.yaml` 的 `symbols`。编辑 `config/strategy.yaml` 的策略文本、版本、`enabled`、指标及可选数值检查，重启生效。空股票池或缺少规则会阻止启动。支持 `sma_N` 和 `rsi_N`；RSI 为最近 N 个差分的简单平均版本，非 Wilder 平滑。

风险示例值：单标的最多 10%、总仓位最多 50%、日内亏损上限 2%、每日最多 10 次下单尝试。它们是可修改的工程保护参数，没有最优性或盈利保证。实际买入同时受现金和购买力约束，不使用额外融资。支持最多六位小数的碎股数量，提交前检查标的 `fractionable`。使用 day 限价单；市价移动可能导致不成交。

持仓上限依据提交前权益和报价估算；市场波动可能使成交后权重漂移。日内亏损使用 Alpaca `last_equity`，触发后禁止新订单，不自动清仓。账户入金/出金会影响盈亏、累计收益和回撤口径；报告显示的是快照观测统计，不是经现金流调整的绩效分析。

## 模块与审计

- `settings.py`：凭证与配置验证；`decision_schema.py`、`deepseek_client.py`：严格 JSON、超时及有限重试。
- `market_data.py`：交易日历、完整 K 线、历史质量、指标。
- `strategy.py`：真实继承 LumiBot Strategy，使用其生命周期、调度、行情和下单接口。
- `broker.py`：固定 Paper SDK 客户端、IEX 适配、只读快照及订单查询。
- `engine.py`、`risk_manager.py`：周期与独立确定性风控。
- `execution.py`：先写数据库再提交、按 client_order_id 恢复。
- `watchdog.py`：独立截止撤单线程。
- `storage.py`、`reporting.py`：SQLite WAL、事件、订单状态、净值及 CLI 报告。

数据库分为 `data/paper.sqlite` 与 `data/dry-run.sqlite`。记录输入、时间、策略及配置哈希、模型及调用耗时、决策、拒绝、订单状态和累计成交数量/均价、持仓快照、错误类型。`new/accepted`、`partially_filled`、`filled`、`canceled`、`rejected`、`expired` 等原始状态均保留。这里保存的是订单成交快照，不声称已实现每一笔交易所撮合的独立账单。

日志位于 `logs/bot.log`；外部异常仅记录类型，不持久化可能携带敏感信息的异常正文。行情/策略原文和账户财务信息属于本地审计数据，应自行保护。

## 框架适配原因

LumiBot 支持 AI AgentManager 和 DeepSeek provider。本项目使用直接的 OpenAI 兼容 SDK 做无工具权限的结构化决策，以隔离模型与交易权限；调度、策略生命周期、行情接口和订单提交仍由 LumiBot 承担。

LumiBot 4.6.46 Alpaca 数据适配默认可能有 16 分钟延迟，且多分钟 K 线的常规时段过滤/历史长度估算需补充。本项目显式设 `delay=0`、IEX、日线拆股复权/分钟线原始价格，增加按真实日历的过滤和足量回看。共享缓存的 feed 键在请求前生成，而注入客户端之后才设置 IEX，因此该客户端绕过共享缓存，避免误标数据来源。IEX 成交量绝非全市场成交量。

## 验证边界

测试默认禁止 socket 网络连接，使用虚假凭证，不接触真实资金。覆盖 BUY/SELL/HOLD、无效响应、股票池、仓位/现金/总敞口、亏损/次数、暂停、重复周期、超时查询、未决阻塞、重启恢复、休市/假日/夏令时/提前收市、完整 K 线、真实 LumiBot 适配契约、客户端订单编号与 Paper 端点防护。

已完成只读 Paper 账户及 IEX 接入、DeepSeek 实际策略审核和暂停状态下 LumiBot 初始化。尚未验证真实纸账户下单/部分成交/撤单竞态、全天无人值守稳定性、完整股票池历史可用性及策略收益。未做策略回测；没有声称策略有效。`data/preflight.json` 保存最近一次只读检查结果。
## Acknowledgements

This project is built with [LumiBot](https://github.com/Lumiwealth/lumibot),
an open-source Python framework for algorithmic trading.

LumiBot is licensed under GPL-3.0.

官方核查：

- [LumiBot Alpaca](https://lumibot.lumiwealth.com/brokers.alpaca.html)
- [Alpaca Paper](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca K 线参数](https://docs.alpaca.markets/us/reference/stockbars)
- [DeepSeek 模型](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek JSON](https://api-docs.deepseek.com/guides/json_mode/)
