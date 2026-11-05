# 验证记录（2026-11-04，北京时间）

- Python 3.13.7；LumiBot 4.6.46 导入成功。
- pytest：57 passed，1 个第三方 websockets 弃用提示；所有测试禁用网络。
- pip check：No broken requirements found；compileall 成功。
- Dry Run 已完整运行，HOLD 测试输入与决策保存到独立 SQLite。
- 使用既有本地密钥实际调用 DeepSeek Flash 审核用户策略成功；首次响应不是有效 JSON，按失败处理，后续精简审核请求返回有效 JSON。未将失败响应当成交易信号。
- Alpaca Paper 只读账户：equity/cash 均为 100000，未持仓、无未完成订单、未 blocked。
- 48 个股票代码通过 Alpaca active/tradable/us_equity 检查。
- CRWV IEX：42 根 5 分钟 K 线返回，最近六个月 128 根完整日 K 线通过验证。其余标的六个月历史尚未逐一实测。
- LumiBot 真正进入 Strategy 初始化及等待开盘，处于持久化暂停状态；随后停止进程。宿主未转发 Ctrl+C 到子进程，因此采用明确 PID 停止，已确认没有遗留 paper 进程。
- 真实模拟下单、成交、撤单和全天稳定性未测试；未发任何买卖单。
- 当前配置：6 个自然月日线 + 40 根完整 5 分钟线；每天开盘后第 5/10/15/20/25 分钟决策，第 29:30 开始撤单，第 30 分钟起停止操作。
- SQLite paper 控制状态保持 paused=true，需通过 resume 解除。

## 2026-11-05 修复验证（保留上方历史记录）

上方为 11 月 4 日的历史工程状态，碎股/DAY/29:30 撤单描述已由本次修复替代。

- 基线 60 项通过；修复后完整测试 180 项通过、0 失败、0 错误、0 跳过，1 条第三方弃用提示。
- 真实 SDK 序列化、低层 HTTP 安全校验、Mock 券商故障注入均通过，测试 socket 全部禁用。
- 离线 E2E：48 标的中排除 1 个，47 个继续；模拟买入 5 股成交 2 股，重启后卖出 2 股；重复周期不重下；截止后安全撤销遗留订单；未决 0。
- pip check、compileall、git diff --check 通过。
- 未发实盘/Paper 委托，未调用真实 DeepSeek 做本次交易决策，未推送 GitHub。
- 网页保持 4200，仅重启网页服务；机器人未运行、paused=true、IOC 权限确认=false。
- 具体账户 IOC 权限与真实 Paper 执行仍未验证。默认禁止下单，不能以本次离线结果当作实测闭环。
- 完整文件清单、测试矩阵、剩余风险、缓存规则及下次开盘方案见 REPAIR_REPORT.md。
