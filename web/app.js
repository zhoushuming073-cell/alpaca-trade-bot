const $ = (id) => document.getElementById(id);
let state = null;
let token = null;
let busy = false;
let online = false;
let refreshing = null;
let lastUpdate = 0;
let pendingAction = null;
let toastTimer;

const esc = (x) => String(x ?? "—").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const money = (x) => x == null || !Number.isFinite(Number(x)) ? "—" :
  Number(x).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const percent = (x) => x == null || !Number.isFinite(Number(x)) ? "—" : (Number(x) * 100).toFixed(2) + "%";
const time = (x, zone = "Asia/Shanghai") => x ? new Date(x).toLocaleTimeString("zh-CN", {
  timeZone: zone, hour12: false, hour: "2-digit", minute: "2-digit"
}) : "—";
const date = (x) => x ? new Date(x).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) : "—";
const shortDate = (x) => x ? new Date(x).toLocaleDateString("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit"
}) : "—";
const badge = (text, cls = "") => '<span class="badge ' + cls + '">' + esc(text) + '</span>';
const dot = (ok) => '<i aria-hidden="true" class="dot ' + (ok ? "ok" : "") + '"></i>';
function table(headers, rows, empty) {
  if (!rows.length) return '<div class="empty">' + esc(empty) + '</div>';
  return '<div class="table-wrap"><table><thead><tr>' +
    headers.map((h) => '<th scope="col">' + esc(h) + '</th>').join("") +
    '</tr></thead><tbody>' + rows.map((r) => '<tr>' +
      r.map((v) => '<td>' + esc(v) + '</td>').join("") + '</tr>').join("") +
    '</tbody></table></div>';
}
const STATUS = {
  RESERVED: "待处理", VALIDATING: "校验中", READY_TO_DISPATCH: "待提交",
  DISPATCH_INTENT_RECORDED: "正在提交", SUBMISSION_OUTCOME_UNKNOWN: "结果待确认",
  ACCEPTED: "已受理", PARTIALLY_FILLED: "部分成交", FILLED: "已成交",
  CANCELED: "已撤销", EXPIRED: "已到期", REJECTED: "已拒绝", NOT_SENT: "未提交",
  RECONCILIATION_REQUIRED: "待核对", BROKER_OPEN: "处理中", BROKER_CLOSED: "已结束",
  new: "已挂单", accepted: "已受理", filled: "已成交", partially_filled: "部分成交",
  canceled: "已撤销", expired: "已到期", rejected: "已拒绝", pending_cancel: "撤单中",
  pending_new: "提交中", done_for_day: "当日结束", replaced: "已替换", held: "待生效",
  ACTIVE: "等待条件", WATCHING: "观察中", TRIGGERED: "已触发", EXECUTED: "已执行",
  SUPERSEDED: "已更新", INVALIDATED: "已失效", NO_ORDER: "暂不下单", NO_TRADE: "暂不交易",
  BUY: "买入", SELL: "卖出", HOLD: "持有", REDUCE: "减仓", WAIT: "继续观察",
  BUY_IF_TRIGGER: "等待买入条件", WATCH: "继续观察",
  OK: "已更新", STALE: "待更新", UNKNOWN: "待核实", SOURCE_UNAVAILABLE: "来源暂不可用",
  AVAILABLE: "可用", UNAVAILABLE: "暂不可用", VERIFIED: "已验证", UNVERIFIED: "未验证"
};
const MODE = {PREMARKET_PLAN: "生成盘前计划", UNIVERSE_SCAN: "扫描关注股票",
  INTRADAY_RECHECK: "复核交易计划", POSITION_REVIEW: "评估当前持仓", RESEARCH_REFRESH: "更新研究档案"};
const STOP = {NONE_BY_AI: "未设置", NONE: "未设置", REQUESTED: "待确认", VERIFIED: "已设置",
  FAILED: "设置失败", UNKNOWN: "待核实", BROKER_STOP: "券商止损"};
const label = (x, fallback = "待确认") => STATUS[x] || fallback;
const healthy = (h) => Boolean(h?.alive && !h.stale && h.last_success && !h.consecutive_failures);
const hasBlock = (s) => Boolean(s?.freeze_reason || s?.safety_reason || s?.config_pending_restart || s?.unresolved?.length);
const fresh = () => online && Date.now() - lastUpdate < 15000;
function updateControls() {
  const ready = fresh() && !!state && !busy;
  const canPause = state?.running && !state?.paused;
  $("toggle-trading").textContent = !state ? "正在连接" : busy ? "处理中…" :
    canPause ? "暂停交易" : state.running ? "继续交易" : "启动并运行";
  $("toggle-trading").disabled = !ready || (!canPause && hasBlock(state));
  $("restart").disabled = !ready || !state?.running || Boolean(state?.unresolved?.length);
  $("stop").disabled = !ready || !state?.running || Boolean(state?.unresolved?.length);
  $("confirm-action").disabled = !ready;
  if (typeof updateChatControls === "function") updateChatControls();
}
function renderSymbols() {
  const query = $("search").value.trim().toUpperCase();
  const symbols = (state?.symbols || []).filter((s) => s.includes(query));
  $("symbols").innerHTML = symbols.map((s) => '<span>' + esc(s) + '</span>').join("") ||
    '<p class="empty">没有匹配的股票</p>';
}
function eventMessage(e) {
  const p = e.payload || {};
  const titles = {operator: "运行设置已更新", error: "运行异常", safety_alert: "交易保护已触发",
    order: "订单更新", order_not_sent: "订单未提交", order_imported: "订单同步",
    order_recovery: "订单状态恢复", premarket_plan: "盘前计划已生成", universe_scan: "股票扫描已完成",
    intraday_recheck: "AI 已复核计划", plan: "计划状态更新", plan_invalidated: "计划已失效",
    intent_rejected: "本次交易未通过检查", config_pending: "配置等待更新",
    research_profile: "研究档案更新", source_call: "研究来源请求", source_fetch_error: "资料读取失败",
    research_news: "研究资讯更新", premarket_data: "盘前行情已读取", quote_errors: "行情暂未通过检查",
    recheck_suppressed: "复核稍后重试", startup_reconciliation: "启动时同步订单",
    after_close_reconciliation: "收盘后同步订单", after_close_reconcile: "收盘后同步订单",
    premarket_lifecycle: "盘前分析进度", ai_call: "AI 分析", stopped: "机器人已停止"};
  let detail = "";
  if (e.kind === "quote_errors") {
    const errors = p.errors || [];
    detail = errors.slice(0, 3).map((v) => (v.symbol || "报价") +
      (String(v.reason).includes("spread") ? " 价差偏大，本轮已跳过。" : " 行情暂不可用，本轮已跳过。")).join(" ");
  } else if (e.kind === "operator") {
    detail = {resume: "自动交易已启用。", autopilot_resume: "自动交易已启用。", pause: "新订单已暂停。",
      start: "机器人已启动。", restart: "机器人已重启，交易保持暂停。", stop: "本地运行已停止。",
      autopilot_blocked: "未满足启动条件，自动交易保持暂停。"}[p.action] || "运行设置发生变化。";
  } else if (e.kind === "error" || e.kind === "source_fetch_error") {
    detail = p.ticker ? p.ticker + " 暂时无法完成处理，详情可在技术记录中查看。" : "处理暂未完成，请查看运行详情。";
  } else if (e.kind === "premarket_plan") {
    detail = "今日计划已记录，可在「AI 计划」查看。";
  } else if (e.kind === "ai_call") {
    detail = (MODE[p.mode] || "AI 分析") + (p.outcome === "ok" ? "已完成。" : "未完成，请查看运行详情。");
  } else if (p.symbol || p.ticker) {
    detail = String(p.symbol || p.ticker) + (p.status ? " · " + label(p.status) : "");
  } else if (e.kind === "premarket_lifecycle") {
    detail = {premarket_planned: "盘前计划已生成。", premarket_failed: "分析未完成，将按计划重试。",
      premarket_blocked: "等待恢复运行。", premarket_waiting_health: "等待连接恢复。",
      premarket_no_trade: "本轮暂无交易计划。", premarket_not_claimed: "本轮任务已处理或等待重试。"}[p.result] || "等待下一步处理。";
  }
  return [titles[e.kind] || "运行状态更新", detail];
}
function render(s) {
  state = s;
  token = s.control_token;
  const accountFresh = !!s.account && !s.account_stale && !s.account_error && s.account_age_seconds != null && s.account_age_seconds < 120;
  const watched = healthy(s.health?.watchdog);
  const worker = healthy(s.health?.worker);
  const lastAI = s.ai_calls?.[0];
  $("today").textContent = new Date(s.now).toLocaleDateString("zh-CN", {
    timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long"
  });
  $("equity").textContent = money(s.account?.equity);
  $("cash").textContent = "账户现金 $" + money(s.account?.cash);
  $("pnl").textContent = s.daily_pnl == null ? "—" : (s.daily_pnl > 0 ? "+" : "") + money(s.daily_pnl);
  $("pnl").className = "value " + (s.daily_pnl > 0 ? "positive" : s.daily_pnl < 0 ? "negative" : "");
  $("return").textContent = percent(s.return);
  $("drawdown").textContent = percent(s.drawdown);
  $("process").textContent = !s.running ? "未启动" : !worker ? "状态待确认" : s.paused ? "已暂停交易" : "运行中";
  $("process").className = "badge " + (s.running && worker && !s.paused && !hasBlock(s) ? "ok" : "");
  let heading, copy;
  const recovering = (s.freeze_reason || s.safety_reason) === "DATABASE_RESTORED_RECONCILIATION_REQUIRED";
  if (recovering) {
    heading = "交易暂停，等待对账";
    copy = "可以查看持仓、研究档案和使用交易助手。完成对账并经你确认后，才会恢复交易。";
  } else if (hasBlock(s)) {
    heading = "需要你确认一下";
    copy = "交易恢复前有待处理事项，请查看下方运行详情。";
  } else if (!s.running) {
    heading = "准备好了，再出发";
    copy = "启动后，机器人会在设定时段研究股票并执行模拟交易。";
  } else if (s.paused) {
    heading = "交易已暂停";
    copy = "目前不会提交新订单。已有委托仍按券商规则执行。";
  } else if (!worker || !watched) {
    heading = "正在确认运行状态";
    copy = "机器人已启动，等待最新心跳与订单同步。";
  } else if (s.phase === "PREMARKET") {
    heading = "盘前研究时段";
    copy = "机器人正在准备今日计划；最新分析结果会显示在下方。";
  } else if (s.phase === "ACTIVE") {
    heading = "留意今天的机会";
    copy = "机器人正在检查计划条件，仅在条件满足时提交订单。";
  } else if (s.phase === "NO_SESSION") {
    heading = "今天休市";
    copy = "今天没有主动交易安排，等待下一个交易日。";
  } else if (s.session_reason === "before_premarket") {
    heading = "等待盘前研究";
    copy = "美东 09:00 开始分析，09:30 进入主动交易时段。";
  } else {
    heading = "今天的交易窗口已结束";
    copy = "不再发起新的策略订单。已有委托仍可成交，持仓继续保留。";
  }
  $("session-title").textContent = heading;
  $("session-copy").textContent = copy;
  $("overview-subtitle").textContent = accountFresh ? "账户、持仓与 AI 的最新进展。" : "账户信息等待更新，当前显示最近记录。";
  ["research", "trade", "rest"].forEach((part) => $("stage-" + part).classList.remove("current"));
  if (s.phase === "PREMARKET") $("stage-research").classList.add("current");
  else if (s.phase === "ACTIVE") $("stage-trade").classList.add("current");
  else if (s.phase === "CLOSED" && s.session_reason !== "before_premarket") $("stage-rest").classList.add("current");
  $("broker-connection").innerHTML = dot(accountFresh) + (accountFresh ? "Alpaca · 已同步" : "Alpaca · 等待同步");
  $("ai-connection").textContent = lastAI ? "AI · 最近分析 " + shortDate(lastAI.ts) + " " + time(lastAI.ts) +
    (lastAI.outcome === "ok" ? "" : " · 未完成") : "AI · 暂无分析记录";
  $("refresh").textContent = "更新于 " + time(s.now) + " · 每 5 秒刷新";
  $("account-time").textContent = s.account_asof ? "账户更新 " + time(s.account_asof) : "等待账户同步";

  const alerts = [];
  if (recovering) alerts.push("历史资料已从备份恢复。网页查询与聊天可用，自动交易保持暂停，等待对账。");
  else if (s.freeze_reason || s.safety_reason || s.unresolved?.length) alerts.push("有订单或运行状态需要核对，暂不能恢复交易。");
  if (s.config_pending_restart) alerts.push("配置已更新，请从「更多」重启机器人后再继续交易。");
  if (s.running && (!worker || !watched)) alerts.push("机器人或订单同步暂未确认正常，请查看运行详情。");
  if (!accountFresh) alerts.push(s.account ? "账户数据尚未更新，金额可能不是最新值。" : "暂时无法读取账户数据。");
  if (s.recent_orders_error) alerts.push("历史订单暂未同步，当前记录可能不完整。");
  if ((s.orders || []).some((o) => o.state_mismatch)) alerts.push("部分订单记录待核对，当前展示券商返回的状态。");
  $("notice").hidden = alerts.length === 0;
  $("notice").innerHTML = esc(alerts.join(" ")) + (alerts.length ? ' <a href="#system">查看详情</a>' : "");
  const positions = Object.entries(s.account?.positions || {});
  $("position-count").textContent = positions.length ? positions.length + " 只" : "";
  $("position-table").innerHTML = table(["股票", "持有股数", "市值 / USD", "平均成本", "浮动盈亏", "止损"], positions.map(([symbol, value]) => {
    const d = s.account.position_details?.[symbol] || {};
    return [symbol, s.account.quantities?.[symbol], money(value), money(d.avg_entry_price), money(d.unrealized_pl),
      STOP[d.stop_status] || "待核实"];
  }), s.account ? "当前没有持仓" : "等待账户数据");
  $("order-table").innerHTML = table(["股票", "方向", "股数 / 已成交", "限价", "状态"], (s.orders || []).slice(0, 8).map((o) => [
    o.symbol, ({buy: "买入", sell: "卖出", BUY: "买入", SELL: "卖出"})[o.side || o.action] || "—",
    (o.qty ?? o.quantity ?? "—") + " / " + (o.filled_qty ?? "—"), money(o.limit_price),
    label(o.status, label(o.state))
  ]), "暂时没有订单");
  $("pending").textContent = s.unresolved?.length ? s.unresolved.length + " 笔待核对" : "最近 8 笔";
  const plans = s.plans || [];
  $("plan-count").textContent = plans.length ? plans.length + " 个计划" : "暂无新计划";
  $("plan-list").innerHTML = plans.map((p) => {
    const c = p.candidate || {}, intent = c.order_intent || {};
    const expired = p.valid_until && new Date(p.valid_until) <= new Date(s.now);
    const description = String(c.analysis || c.thesis || "暂无详细分析记录。");
    const decision = label(c.trade_decision, "等待分析");
    return '<article class="plan"><div class="plan-top"><span class="ticker">' + esc(c.symbol || intent.symbol || "计划") +
      '</span>' + badge(expired ? "已到期" : label(c.trade_decision, label(p.status))) + '</div><p>' +
      esc(decision + (intent.qty ? " · 条件满足后按计划执行。" : " · 暂无下单安排。")) + '</p><div class="plan-meta">' +
      '<span>有效至 ' + esc(time(p.valid_until)) + '</span>' +
      (intent.qty ? '<span>' + esc(intent.qty) + ' 股 · $' + esc(money(intent.limit_price)) + '</span>' : '<span>暂无下单安排</span>') +
      (intent.stop_policy ? '<span>止损：' + esc(STOP[intent.stop_policy] || "待核实") + '</span>' : "") +
      '</div><details class="plan-note"><summary>查看 AI 分析</summary><p>' + esc(description) + '</p></details></article>';
  }).join("") || '<div class="empty">今日暂无生效计划</div>';
  $("ai-list").innerHTML = (s.ai_calls || []).slice(0, 6).map((a) =>
    '<div class="ai-row"><div><strong>' + esc(MODE[a.mode] || "AI 分析") + '</strong><small>' +
    esc(shortDate(a.ts) + " " + time(a.ts) + " · " + a.model) + '</small></div><div>' +
    badge(a.outcome === "ok" ? "已完成" : "未完成", a.outcome === "ok" ? "" : "warn") +
    '<small>' + (a.latency_ms != null ? esc((a.latency_ms / 1000).toFixed(1)) + " 秒" : "") + '</small></div></div>'
  ).join("") || '<div class="empty">还没有 AI 分析记录</div>';
  const research = s.research || [];
  $("research-count").textContent = research.length + " 只股票已建档";
  $("research-table").innerHTML = table(["股票", "档案", "资料来源", "记录数", "最近核验"], research.map((r) => [
    r.symbol, label(r.status), label(r.source_status), r.evidence_count ?? 0, date(r.verified_at)
  ]), "还没有研究档案");
  const seen = new Set();
  const events = [...(s.events || [])].reverse().filter((e) => !["input", "ai_raw"].includes(e.kind)).filter((e) => {
    const key = eventMessage(e).join("|");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 12);
  $("events").innerHTML = events.map((e) => {
    const [title, detail] = eventMessage(e);
    return '<div class="event"><time datetime="' + esc(e.ts) + '">' + esc(shortDate(e.ts) + " " + time(e.ts)) +
      '</time><div><strong>' + esc(title) + '</strong><p>' + esc(detail) + '</p></div></div>';
  }).join("") || '<div class="empty">暂无运行记录</div>';
  $("symbol-count").textContent = (s.symbols || []).length + " 只 · 固定股票池";
  renderSymbols();
  $("system-summary").textContent = alerts.length ? "有事项需要确认" : "连接与账户设置";
  $("watchdog").textContent = watched ? "同步正常 · " + time(s.health.watchdog.last_success) : s.running ? "等待成功对账" : "机器人未运行";
  $("broker-stop").textContent = s.broker_stop === "VERIFIED" ? "已验证" : "未验证 · 暂不可用";
  $("available").textContent = "$" + money(s.account?.available_non_margin_cash ?? s.account?.non_margin_cash ?? s.account?.cash);
  $("config").textContent = s.config_pending_restart ? "等待重启生效" : "已生效";
  $("source").textContent = s.account_source || "暂无数据";
  $("now-et").textContent = time(s.now, "America/New_York") + " ET";
  $("version").textContent = "美股 · 模拟交易";
  $("diagnostic-data").textContent = JSON.stringify({
    配置: s.active_config_hash, 冻结原因: s.freeze_reason || s.safety_reason || null,
    账户错误: s.account_error, 历史订单错误: s.recent_orders_error, 心跳: s.health,
    订单: (s.orders || []).map((o) => ({股票: o.symbol, 日期: o.day, 订单编号: o.cid, 券商状态: o.status,
      本地状态: o.local_state || o.state, 止损: o.stop_status})),
    事件: events, 模型调用: (s.ai_calls || []).map((a) => ({时间: a.ts, 类型: a.mode,
      模型: a.model, 输入用量: a.prompt_tokens, 输出用量: a.completion_tokens, 结果: a.outcome, 错误: a.error}))
  }, null, 2);
  updateControls();
}
async function refresh() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const response = await fetch("/api/status", {signal: AbortSignal.timeout(12000)});
      if (!response.ok) throw new Error("status");
      const next = await response.json();
      if (typeof next.running !== "boolean" || typeof next.paused !== "boolean") throw new Error("invalid status");
      online = true;
      lastUpdate = Date.now();
      render(next);
      return true;
    } catch {
      online = false;
      $("process").textContent = "连接中断";
      $("process").className = "badge warn";
      $("session-title").textContent = "暂时无法连接工作台";
      $("session-copy").textContent = "正在重试连接。当前保留上次数据，运行状态尚未确认。";
      $("broker-connection").textContent = "Alpaca · 状态待确认";
      $("refresh").textContent = "更新失败 · 显示上次记录";
      $("notice").hidden = false;
      $("notice").textContent = "工作台连接中断，已暂时禁用操作。连接恢复后会自动更新。";
      updateControls();
      return false;
    } finally { refreshing = null; }
  })();
  return refreshing;
}
function closeMenu() {
  $("control-menu").hidden = true;
  $("more").setAttribute("aria-expanded", "false");
}
function confirmAction(action) {
  closeMenu();
  if (!fresh() || busy) return;
  pendingAction = action;
  const content = {
    resume: [state.running ? "继续自动交易？" : "启动并运行机器人？",
      "将在设定时段分析股票并提交模拟订单。" +
      (state.phase === "CLOSED" || state.phase === "NO_SESSION" ? "当前不在主动交易时段，运行后将等待下一时段。" : ""),
      state.running ? "继续交易" : "启动并运行"],
    restart: ["重启机器人？", "重新加载程序，并保持交易暂停。已经提交的委托不会撤销。", "重启"],
    stop: ["停止机器人？", "停止本地运行并关闭自动运行。已经提交的委托仍可能成交。", "停止"]
  }[action];
  if (!content) return;
  $("confirm-title").textContent = content[0];
  $("confirm-copy").textContent = content[1];
  $("confirm-action").textContent = content[2];
  $("confirm").showModal();
}
function allowed(action) {
  if (!fresh() || !state) return false;
  if (action === "resume") return !hasBlock(state);
  if (action === "pause") return state.running && !state.paused;
  return state.running && !state.unresolved?.length;
}
async function control(action) {
  if (busy || !allowed(action)) return;
  busy = true;
  updateControls();
  try {
    // Confirm against a fresh snapshot; never send a stale toggle or retry a POST.
    if (!await refresh() || !allowed(action)) throw new Error("状态已变化，请确认最新状态后重试。");
    const response = await fetch("/api/control/" + action, {
      method: "POST", headers: {"X-Control-Token": token}, signal: AbortSignal.timeout(15000)
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "操作未完成，请查看运行详情。");
    toast({resume: "已启用自动交易", pause: "已暂停新订单", restart: "已请求重启，交易保持暂停", stop: "已请求停止机器人"}[action]);
    await refresh();
  } catch (error) {
    toast(error.name === "TimeoutError" ? "操作结果尚未确认，请等待状态更新。" : error.message);
  } finally {
    busy = false;
    updateControls();
  }
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 7000);
}
$("toggle-trading").onclick = () => {
  if (!fresh() || busy) return;
  if (state.running && !state.paused) control("pause");
  else confirmAction("resume");
};
$("more").onclick = () => {
  const open = $("control-menu").hidden;
  $("control-menu").hidden = !open;
  $("more").setAttribute("aria-expanded", String(open));
};
document.addEventListener("click", (e) => { if (!e.target.closest(".controls")) closeMenu(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMenu(); $("more").blur(); }
});
["restart", "stop"].forEach((action) => { $(action).onclick = () => confirmAction(action); });
$("cancel-confirm").onclick = () => $("confirm").close();
$("confirm-action").onclick = () => {
  const action = pendingAction;
  $("confirm").close();
  if (action) control(action);
};
$("confirm").addEventListener("close", () => { pendingAction = null; });
$("search").oninput = renderSymbols;
document.querySelectorAll('a[href^="#"]').forEach((a) => a.addEventListener("click", () => {
  const target = document.querySelector(a.getAttribute("href"));
  if (target?.tagName === "DETAILS") target.open = true;
  document.querySelectorAll("nav a").forEach((link) => link.classList.toggle("active", link === a));
}));
refresh();
setInterval(refresh, 5000);

let chatBusy = false;
let chatLoading = false;
let chatMessages = new Map();
let chatFirst = null;
let chatLast = 0;
let chatSeen = 0;
let chatHasEarlier = false;
try { chatSeen = Number(localStorage.getItem("paper-chat-seen") || 0); } catch {}
function updateChatControls() {
  $("chat-send").disabled = chatBusy || !fresh() || !$("chat-input").value.trim();
  document.querySelectorAll("[data-question]").forEach((b) => { b.disabled = chatBusy || !fresh(); });
  $("chat-state").textContent = chatBusy ? "正在回复" : fresh() ? "可以提问" : "等待连接";
}
function markChatSeen() {
  chatSeen = chatLast;
  try { localStorage.setItem("paper-chat-seen", String(chatSeen)); } catch {}
  $("assistant-unread").hidden = true;
}
function setChatOpen(open) {
  $("assistant-panel").hidden = !open;
  $("assistant-open").setAttribute("aria-expanded", String(open));
  $("assistant-open").hidden = open;
  const modal = open && window.innerWidth <= 800;
  $("assistant-panel").setAttribute("aria-modal", String(modal));
  $("chat-backdrop").hidden = !modal;
  document.body.style.overflow = modal ? "hidden" : "";
  if (open) {
    markChatSeen();
    loadChat();
    $("chat-input").focus();
    $("chat-body").scrollTop = $("chat-body").scrollHeight;
  } else {
    $("assistant-open").focus();
  }
}
function renderChat() {
  const box = $("chat-body");
  const nearEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const list = [...chatMessages.values()].sort((a,b) => a.id-b.id);
  $("chat-welcome").hidden = list.length > 0;
  // Keep existing DOM nodes: polling must not re-announce the entire conversation.
  const existing = new Set([...$("chat-messages").children].map((n) => Number(n.dataset.id)));
  for (const m of list) {
    if (existing.has(m.id)) continue;
    const el = document.createElement("article");
    el.className = "chat-message " + (["assistant","user","system"].includes(m.role) ? m.role : "system");
    el.dataset.id = String(m.id);
    const who = m.role === "user" ? "你" : m.role === "system" ? "系统提示" : m.source === "proactive" ? "助手 · 主动消息" : "交易助手";
    el.innerHTML = '<div class="chat-meta"><b>' + esc(who) + '</b><time>' + esc(shortDate(m.ts) + " " + time(m.ts)) +
      '</time></div><div class="chat-text">' + esc(m.body) + '</div>';
    if (m.role === "assistant") {
      const expand = document.createElement("button");
      expand.className = "chat-expand";
      expand.textContent = "展开聊聊";
      expand.onclick = () => sendChat("请展开说明这条回答，并区分事实与判断：" + m.body.slice(0, 350), true);
      el.append(expand);
    }
    const next = [...$("chat-messages").children].find(n=>Number(n.dataset.id)>m.id);
    $("chat-messages").insertBefore(el, next || null);
  }
  $("chat-older").hidden = !chatHasEarlier;
  if (!$("assistant-panel").hidden) {
    markChatSeen();
    if (nearEnd || chatBusy) box.scrollTop = box.scrollHeight;
  } else {
    const unread = list.filter((m) => m.id > chatSeen && m.role === "assistant").length;
    $("assistant-unread").hidden = unread === 0;
    $("assistant-unread").textContent = unread > 9 ? "9+" : String(unread);
  }
}
async function loadChat(older = false) {
  if (chatLoading) return;
  chatLoading = true;
  try {
    const query = older && chatFirst ? "?before=" + chatFirst : chatLast ? "?after=" + chatLast : "";
    const response = await fetch("/api/assistant" + query, {signal:AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error("对话暂时无法同步，稍后会自动重试。");
    const data = await response.json();
    const oldHeight = $("chat-body").scrollHeight;
    const previousLast = chatLast;
    for (const message of data.messages || []) {
      chatMessages.set(message.id, message);
      chatFirst = Math.min(chatFirst || message.id, message.id);
      chatLast = Math.max(chatLast, message.id);
    }
    if (older || !previousLast) chatHasEarlier = !!data.has_earlier;
    renderChat();
    if (older) $("chat-body").scrollTop += $("chat-body").scrollHeight - oldHeight;
  } catch (error) {
    if (!$("assistant-panel").hidden) {
      $("chat-error").hidden = false;
      $("chat-error").textContent = error.message;
    }
  } finally {
    chatLoading = false;
    updateChatControls();
  }
}
async function sendChat(question, expand = false) {
  question = (question || "").trim();
  if (!question || question.length > 500 || chatBusy || !fresh()) return;
  chatBusy = true;
  $("chat-error").hidden = true;
  $("chat-busy").hidden = false;
  $("chat-input").value = "";
  updateChatControls();
  const requestId = crypto.randomUUID();
  try {
    const response = await fetch("/api/assistant", {method:"POST",
      headers:{"Content-Type":"application/json", "X-Control-Token":token},
      body:JSON.stringify({message:question, request_id:requestId, expand}),
      signal:AbortSignal.timeout(35000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "回答暂未完成，请稍后再试。");
    if (data.status !== "done") throw new Error("这次回答尚未完成，稍后可查看对话记录。");
    await loadChat();
  } catch(error) {
    $("chat-error").hidden = false;
    $("chat-error").textContent = error.name === "TimeoutError" ?
      "回答仍可能在处理中，请稍后查看记录；不会自动重复请求。" : error.message;
    await loadChat();
  } finally {
    chatBusy = false;
    $("chat-busy").hidden = true;
    updateChatControls();
    $("chat-input").focus();
  }
}
$("assistant-open").onclick = () => setChatOpen(true);
$("assistant-close").onclick = () => setChatOpen(false);
$("chat-backdrop").onclick = () => setChatOpen(false);
$("chat-older").onclick = () => loadChat(true);
$("chat-form").onsubmit = (e) => { e.preventDefault(); sendChat($("chat-input").value); };
$("chat-input").oninput = updateChatControls;
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault(); sendChat($("chat-input").value);
  }
});
document.querySelectorAll("[data-question]").forEach((b) => { b.onclick = () => sendChat(b.dataset.question); });
document.addEventListener("keydown", (e) => {
  if ($("assistant-panel").hidden) return;
  if (e.key === "Escape" && !$("confirm").open) { setChatOpen(false); return; }
  if (e.key === "Tab" && window.innerWidth <= 800) {
    const nodes = [...$("assistant-panel").querySelectorAll("button:not(:disabled),textarea")].filter(n=>n.getClientRects().length);
    const first = nodes[0], last = nodes.at(-1);
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
window.addEventListener("resize", () => {
  if (!$("assistant-panel").hidden) {
    const modal = window.innerWidth <= 800;
    $("assistant-panel").setAttribute("aria-modal",String(modal));
    $("chat-backdrop").hidden = !modal;
    document.body.style.overflow = modal ? "hidden" : "";
  }
});
loadChat();
setInterval(loadChat,15000);
