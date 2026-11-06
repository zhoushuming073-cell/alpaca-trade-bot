const $ = (id) => document.getElementById(id);
let state = null;
let token = null;
let busy = false;
let online = false;
let refreshing = null;
let lastUpdate = 0;
let pendingAction = null;
let toastTimer;

const PAGE_META = {
  overview: {title: "交易概览", subtitle: "账户、持仓与 AI 的最新进展。", crumb: "账户概览"},
  positions: {title: "持仓与订单", subtitle: "Alpaca Paper 实际持仓、委托与成交状态。", crumb: "持仓与订单"},
  plans: {title: "AI 计划", subtitle: "今日 AI 判断、触发条件与复核记录。", crumb: "AI 计划"},
  agents: {title: "智能体", subtitle: "研究员、反方分析师与基金经理的独立研究记录；研究完成不等于下单。", crumb: "智能体"},
  research: {title: "研究档案", subtitle: "10 个板块 · 48 只固定池标的的来源、证据与核验时间。", crumb: "研究档案"},
  activity: {title: "运行记录", subtitle: "机器人、订单同步和系统事件。", crumb: "运行记录"},
  robot: {title: "机器人状态", subtitle: "实时观察 AI 指令、Python 执行与 Alpaca 回报。", crumb: "机器人状态"},
  watchlist: {title: "关注股票", subtitle: "10 个板块 · 48 只固定股票池；研究、扫描和下单均受此约束。", crumb: "关注股票"},
};
const PAGE_ALIASES = {system: "activity"};
let activePage = "overview";

function pageFromHash() {
  const raw = decodeURIComponent((location.hash || "").replace(/^#/, ""));
  const name = PAGE_ALIASES[raw] || raw;
  return PAGE_META[name] ? name : "overview";
}

function setPage(name, {scroll = true} = {}) {
  const page = PAGE_META[name] ? name : "overview";
  activePage = page;
  document.querySelectorAll(".page-view").forEach((view) => {
    const visible = view.dataset.page === page;
    view.classList.toggle("active", visible);
    view.hidden = !visible;
  });
  document.querySelectorAll("nav a[data-page]").forEach((link) => {
    const active = link.dataset.page === page;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const meta = PAGE_META[page];
  $("page-title").textContent = meta.title;
  $("page-subtitle").textContent = meta.subtitle;
  $("crumb-current").textContent = meta.crumb;
  document.title = meta.title + " · Paper 交易工作台";
  if (location.hash !== "#" + page) history.replaceState(null, "", "#" + page);
  setMobileSidebar(false);
  if (scroll) window.scrollTo({top: 0, behavior: "auto"});
  requestAnimationFrame(moveNavPill);
  if (page === "robot" && !robotEvents.size) loadRobotLog(true);
}

function setSidebar(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const button = $("sidebar-toggle");
  const icon = $("sidebar-toggle-icon");
  if (button) {
    button.setAttribute("aria-expanded", String(!collapsed));
    button.title = collapsed ? "展开侧边栏" : "收起侧边栏";
  }
  if (icon) icon.textContent = collapsed ? "›" : "‹";
  try { localStorage.setItem("paper-sidebar-collapsed", collapsed ? "1" : "0"); } catch {}
  requestAnimationFrame(moveNavPill);
}

function setMobileSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  $("sidebar-backdrop").hidden = !open;
  $("mobile-nav").setAttribute("aria-expanded", String(open));
}

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
  OPEN_LONG: "做多开仓", CLOSE_LONG: "平多", OPEN_SHORT: "做空开仓", COVER_SHORT: "买入平空",
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
  const query = $("search").value.trim().toLowerCase();
  const groups = groupsOf(state);
  const sections = groups.map((group) => {
    const matchesGroup = !query || String(group.name).toLowerCase().includes(query);
    const symbols = (group.symbols || []).filter((symbol) =>
      !query || matchesGroup || symbol.toLowerCase().includes(query));
    if (!symbols.length) return "";
    return '<section class="symbol-group">' +
      '<div class="symbol-group-head"><span class="symbol-group-no">' +
      String(group.id).padStart(2, "0") + '</span><b>' + esc(group.name) +
      '</b><span class="symbol-group-count">' + groupCount(group) + ' 只</span></div>' +
      '<div class="symbols">' + symbols.map((symbol) => '<span>' + esc(symbol) + '</span>').join("") +
      '</div></section>';
  }).join("");
  $("symbols").innerHTML = sections || '<p class="empty">没有匹配的股票</p>';
}
function eventMessage(e) {
  const p = e.payload || {};
  const titles = {operator: "运行设置已更新", error: "运行异常", safety_alert: "交易保护已触发",
    order: "订单更新", order_not_sent: "订单未提交", order_imported: "订单同步",
    order_recovery: "订单状态恢复", premarket_plan: "盘前计划已生成", universe_scan: "股票扫描已完成",
    intraday_recheck: "AI 已复核计划", plan: "计划状态更新", plan_invalidated: "计划已失效",
    intent_rejected: "本次交易未通过检查", config_pending: "配置等待更新",
    research_profile: "研究档案更新", source_call: "研究来源请求", source_fetch_error: "资料读取失败",
    research_news: "研究资讯更新", premarket_data: "盘前行情已读取", quote_errors: "个别标的报价暂不可用",
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

const FALLBACK_GROUPS = [
  {id: 1, name: "算力基建", symbols: ["CRWV", "NBIS", "IREN", "APLD", "WULF", "CIFR", "HUT", "SMCI", "DELL"]},
  {id: 2, name: "存储芯片", symbols: ["MU", "SNDK", "WDC", "STX", "SKHY"]},
  {id: 3, name: "AI芯片", symbols: ["NVDA", "AMD", "AVGO", "TSM", "ALAB", "MRVL"]},
  {id: 4, name: "光通信", symbols: ["COHR", "LITE", "POET"]},
  {id: 5, name: "商业航天", symbols: ["RKLB", "LUNR", "ASTS", "SPCX"]},
  {id: 6, name: "量子计算", symbols: ["IONQ", "QBTS", "RGTI", "QUBT"]},
  {id: 7, name: "AI应用", symbols: ["SOUN", "PLTR", "GOOGL", "META", "AAPL", "RDDT"]},
  {id: 8, name: "数字金融", symbols: ["RIOT", "MARA", "CLSK", "COIN", "SOFI"]},
  {id: 9, name: "核能科技", symbols: ["OKLO", "SMR", "NNE"]},
  {id: 10, name: "创新医药", symbols: ["MRNA", "SMMT", "VKTX"]},
];
const groupsOf = (source) => Array.isArray(source?.groups) && source.groups.length
  ? source.groups : FALLBACK_GROUPS;
const groupLookup = (groups) => new Map(groups.flatMap((group) =>
  (group.symbols || []).map((symbol) => [symbol, group])));
const groupOf = (symbol, source) => groupLookup(groupsOf(source)).get(symbol) || null;
const groupText = (group) => group ? String(group.id).padStart(2, "0") + " · " + group.name : "未分组";
const groupCount = (group) => (group?.symbols || []).length;
const htmlCache = new Map();
let planRenderKey = "";
const planExpanded = new Set();
let researchState = {query: "", sector: "", status: "", detail: null, scroll: 0};
let researchDetailData = null;
let robotEvents = new Map();
let robotNewIds = new Set();
let robotExpanded = new Set();
let robotCursor = 0;
let robotFilter = "all";
let robotMonitor = [];
let robotServerTime = null;
let robotLoading = false;
let robotError = null;
let robotRenderKey = "";
const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const motionAllowed = () => !motionQuery.matches;

function withViewTransition(update) {
  if (!motionAllowed() || typeof document.startViewTransition !== "function") {
    update();
    return null;
  }
  return document.startViewTransition(update);
}

function setHTML(id, html) {
  if (htmlCache.get(id) === html) return false;
  htmlCache.set(id, html);
  $(id).innerHTML = html;
  return true;
}

function setText(id, value) {
  const element = $(id);
  if (!element || element.textContent === String(value)) return false;
  element.textContent = String(value);
  return true;
}

function setValue(id, value) {
  const element = $(id);
  if (!element) return;
  const next = String(value);
  if (element.textContent === next) return;
  element.textContent = next;
  element.classList.remove("flash");
  void element.offsetWidth;
  element.classList.add("flash");
  setTimeout(() => element.classList.remove("flash"), 700);
}

function statusSummary(s) {
  const worker = healthy(s.health?.worker);
  const watched = healthy(s.health?.watchdog);
  const locked = Boolean(s.freeze_reason || s.safety_reason || s.unresolved?.length || s.config_pending_restart);
  if (locked) {
    return {
      label: "异常", tone: "danger",
      heading: s.freeze_reason || s.safety_reason ? "交易受到保护" : "需要人工核对",
      copy: "存在冻结、未知订单或待重启配置；系统不会按正常交易状态处理。",
    };
  }
  if (!s.running) {
    return {label: "未运行", tone: "idle", heading: "机器人未运行", copy: "等待本地 supervisor 在交易日启动。"};
  }
  if (s.paused) {
    return {label: "已暂停", tone: "warn", heading: "交易已暂停", copy: "不会提交新订单；已有委托仍按券商规则处理。"};
  }
  if (!worker || !watched) {
    return {label: "异常", tone: "danger", heading: "运行状态待确认", copy: "worker 或订单同步尚未恢复健康，新下单保持冻结。"};
  }
  if (s.phase === "ACTIVE") {
    return {label: "运行中", tone: "ok", heading: "正在监测交易机会", copy: "AI 计划正在等待触发；只在条件满足时提交 Paper 订单。"};
  }
  if (s.phase === "PREMARKET") {
    return {label: "盘前准备", tone: "ok", heading: "正在准备今天的计划", copy: "AI 正在结合账户、研究档案和最新行情生成交易计划。"};
  }
  if (s.phase === "NO_SESSION") {
    return {label: "运行中", tone: "idle", heading: "今天休市", copy: "没有新的交易安排，等待下一个交易日。"};
  }
  return {label: "运行中", tone: "idle", heading: "今天的交易窗口已结束", copy: "不再发起新的本地订单；已有委托可能继续成交。"};
}

function planStatusInfo(plan, now) {
  const status = String(plan?.status || "").toUpperCase();
  const active = ["ACTIVE", "WATCHING", "TRIGGERED"].includes(status);
  if (active && plan.valid_until && new Date(plan.valid_until) <= now) {
    return {key: "expired", label: "已到期"};
  }
  if (active) return {key: "active", label: "正在进行"};
  if (status === "EXECUTED") return {key: "executed", label: "已执行"};
  if (status === "SUPERSEDED" || status === "INVALIDATED") return {key: "invalid", label: "已失效"};
  if (status === "NO_ORDER") return {key: "invalid", label: "未下单"};
  if (status === "NO_TRADE") return {key: "invalid", label: "不交易"};
  if (status === "EXPIRED") return {key: "expired", label: "已到期"};
  return {key: "invalid", label: status || "状态待确认"};
}

function conditionText(condition) {
  const names = {last: "价格", price: "价格", volume: "成交量", change_pct: "涨跌幅"};
  return `${names[condition.field] || condition.field} ${condition.op} ${condition.value}`;
}

function purposeText(purpose) {
  return {REQUEST_AI_RECHECK: "AI 复核", INVALIDATE_PLAN: "失效条件", EXECUTE_ORDER: "执行订单"}[purpose] || purpose;
}

function planDetailHtml(candidate, plan, statusLabel) {
  const triggers = candidate.triggers || [];
  const triggerHtml = triggers.length ? '<h4>触发条件</h4><ul>' + triggers.map((t) =>
    '<li><b>' + esc(purposeText(t.purpose)) + '</b>：' +
    (t.all_of || []).map(conditionText).map(esc).join(" 且 ") + '</li>').join("") + '</ul>' : "";
  const risks = (candidate.risk_flags || []).length ? '<h4>风险因素</h4><ul>' +
    candidate.risk_flags.map((r) => '<li>' + esc(r.replaceAll("_", " ")) + '</li>').join("") + '</ul>' : "";
  const intent = candidate.order_intent;
  const order = intent ? '<h4>订单意图</h4><p>' + esc(
    `${intent.action || intent.side || "BUY"} ${intent.qty || "—"} 股 · ${intent.order_type || "LIMIT"} · ` +
    `${intent.limit_price || "—"} · ${intent.time_in_force || "DAY"} · 止损 ${intent.stop_policy || "NONE"}`
  ) + '</p>' : "";
  return '<div class="plan-detail-inner">' +
    '<h4>完整分析</h4><p>' + esc(candidate.analysis || candidate.thesis || "档案中没有完整分析。") + '</p>' +
    triggerHtml + risks + order +
    '<h4>计划记录</h4><p>' + esc(`${statusLabel || plan.status} · 有效期 ${plan.valid_until || "—"}${plan.reason ? " · " + plan.reason : ""}`) + '</p>' +
    '</div>';
}

function renderBrief(s) {
  const brief = s.brief || {};
  const alias = {BUY: "买入", SELL: "卖出", HOLD: "持有", WAIT: "观察", BUY_IF_TRIGGER: "等待触发", NO_TRADE: "不交易",
    OPEN_LONG: "做多", CLOSE_LONG: "平多", OPEN_SHORT: "做空", COVER_SHORT: "平空"};
  const focus = (brief.focus || []).slice(0, 4);
  setHTML("brief-focus", focus.length ? focus.map((item) =>
    '<div class="brief-focus-item"><b>' + esc(item.symbol) + '</b><span>' +
    esc(alias[item.decision] || item.decision || "等待判断") + ' · ' + esc(item.analysis || "等待 AI 分析") +
    '</span><em>' + esc(planStatusInfo({status: item.status, valid_until: item.valid_until}, new Date(s.now || Date.now())).label) +
    '</em></div>').join("") : '<p class="empty">今天暂时没有有效计划。</p>');
  const positions = (brief.positions || []);
  setHTML("brief-positions", positions.length ? positions.map((item) =>
    '<div class="brief-position"><span><b>' + esc(item.symbol) + '</b> ' + esc(item.qty) + ' 股<br><small>' +
    esc(item.analysis || "等待评估") + '</small></span><strong>' + esc(alias[item.judgment] || item.judgment || "观察") +
    '</strong></div>').join("") : '<p class="empty">当前没有持仓。</p>');
  const events = (brief.events || []).slice(0, 4);
  setHTML("brief-events", events.length ? events.map((event) =>
    '<div class="brief-event"><b>' + esc(event.title || event.symbol || "研究事件") + '</b><small>' +
    esc([event.symbol, event.kind, event.summary].filter(Boolean).join(" · ")) + '</small></div>').join("") :
    '<p class="empty">暂无新的研究事件。</p>');
  setText("brief-next", brief.next || "等待系统状态。");
  setText("brief-asof", brief.as_of ? "更新于 " + time(brief.as_of) : "等待同步");
}

function renderPlans(s) {
  const now = new Date(s.now || Date.now());
  const rows = (s.plan_history && s.plan_history.length ? s.plan_history : s.plans || [])
    .filter((plan) => plan && plan.candidate);
  const dataKey = JSON.stringify(rows.map((plan) => [
    plan.plan_id, plan.status, plan.valid_until, plan.day,
    plan.candidate?.trade_decision, plan.candidate?.analysis, plan.candidate?.risk_flags,
    plan.candidate?.order_intent, plan.candidate?.triggers,
  ]));
  if (dataKey === planRenderKey) return;
  planRenderKey = dataKey;
  const cards = rows.map((plan) => {
    const candidate = plan.candidate || {};
    const state = planStatusInfo(plan, now);
    return {plan, candidate, state};
  }).sort((a, b) => {
    const rank = {active: 0, expired: 1, executed: 2, invalid: 3};
    const d = (rank[a.state.key] ?? 9) - (rank[b.state.key] ?? 9);
    return d || String(b.plan.created_at || "").localeCompare(String(a.plan.created_at || ""));
  });
  const active = cards.filter((item) => item.state.key === "active").length;
  setText("plan-count", `${active} 个进行中 · 共 ${cards.length} 个计划`);
  const html = cards.map(({plan, candidate, state}) => {
    const id = plan.plan_id || candidate.plan_id || "";
    const open = planExpanded.has(id);
    const decision = label(candidate.trade_decision, "等待判断");
    return '<article class="plan-card' + (open ? " open" : "") + '" data-plan-id="' + esc(id) +
      '" role="button" tabindex="0" aria-expanded="' + String(open) + '">' +
      '<div class="plan-head"><span class="plan-symbol">' + esc(candidate.symbol || "计划") +
      '</span><span class="plan-tag ' + state.key + '">' + esc(state.label) + '</span></div>' +
      '<div class="plan-decision">' + esc(decision) + (candidate.opportunity_grade ? ' · ' + esc(candidate.opportunity_grade) + ' 级机会' : '') + '</div>' +
      '<p class="plan-thesis">' + esc(candidate.analysis || candidate.thesis || "暂无分析摘要") + '</p>' +
      '<div class="plan-meta"><span>有效期 ' + esc(date(plan.valid_until)) + '</span>' +
      (candidate.risk_flags?.length ? '<span>' + esc(candidate.risk_flags.length) + ' 个风险标记</span>' : '') +
      '<span>' + esc(plan.day || "") + '</span></div>' +
      '<div class="plan-detail"><div>' + planDetailHtml(candidate, plan, state.label) + '</div></div></article>';
  }).join("");
  setHTML("plan-list", html || '<div class="empty">暂时没有交易计划。</div>');
}

function renderAgents(s) {
  const ma = s.multi_agent || {};
  const cfg = ma.config || {};
  const agents = cfg.agents || {};
  const budget = cfg.budget || {};
  const costKnown = Number(budget.cost_per_1m_input_usd || 0) > 0 ||
    Number(budget.cost_per_1m_output_usd || 0) > 0;
  const run = ma.latest_run;
  const researchOnly = run?.mode === "RESEARCH_ONLY";
  const reports = ma.reports || [];
  const banner = $("agent-banner");
  if (banner) {
    banner.hidden = !!ma.enabled;
    banner.textContent = researchOnly
      ? "当前为 RESEARCH_ONLY 影子研究：真实只读行情/账户 + DeepSeek，已禁止全部券商写请求；不会生成可执行计划。"
      : "多智能体当前未启用；以下为只读历史或等待状态。启用需要显式配置并安全重启，不会自动下单。";
  }
  const byRole = (role) => reports.filter((item) => item.role === role);
  const latestFor = (role) => byRole(role).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))[0];
  const roleRows = [
    ["researcher", "市场研究员", agents.researcher],
    ["skeptic", "反方分析师", agents.skeptic],
    ["portfolio_manager", "基金经理", agents.portfolio_manager],
    ["baseline", "单智能体基准", agents.baseline],
  ];
  const roleHtml = roleRows.map(([role, label, roleCfg]) => {
    const enabled = roleCfg?.enabled !== false && (cfg.enabled || researchOnly);
    const item = latestFor(role === "researcher" ? "researcher" : role);
    const failed = run?.status === "FAILED" && role !== "baseline" && !item;
    let status = "等待运行";
    if (!cfg.enabled && !researchOnly) status = "未启用";
    else if (!enabled) status = "已关闭";
    else if (failed) status = "失败";
    else if (item?.status === "OK") status = "已完成";
    else if (run?.status === "RUNNING") status = "运行中";
    const error = run?.status === "FAILED" ? String(run.error || "") : "";
    return '<article class="agent-role-card"><div class="agent-role-name">' + esc(label) + '</div>' +
      '<div class="agent-role-model">' + esc(roleCfg?.model || "未配置") + '</div>' +
      '<div class="agent-role-status"><span>' + esc(status) + '</span><span class="hint">' +
      esc(item?.created_at ? shortDate(item.created_at) + " " + time(item.created_at) : "—") + '</span></div>' +
      (error ? '<div class="agent-role-error">' + esc(error.slice(0, 180)) + '</div>' : '') +
      '</article>';
  }).join("");
  setHTML("agent-role-grid", roleHtml || '<div class="empty">暂无智能体配置</div>');

  setText("agent-run-status", run ? (run.status || "—") : "尚未运行");
  if (!run) {
    setHTML("agent-run", '<div class="empty">今天还没有多智能体研究记录。盘前研究完成前不会生成交易计划。</div>');
  } else {
    const usage = ma.usage || [];
    const total = usage.reduce((sum, item) => sum + Number(item.prompt_tokens || 0) + Number(item.completion_tokens || 0), 0);
    const cost = usage.reduce((sum, item) => sum + Number(item.cost_usd || 0), 0);
    const items = [
      ["运行编号", run.run_id], ["状态", run.status],
      ["运行模式", run.mode || "THREE_AGENT"],
      ["交易日", run.trading_day], ["配置版本", run.universe_version],
      ["研究 / 质疑", byRole("researcher").length + " / " + byRole("skeptic").length],
      ["预算内调用", usage.reduce((sum, item) => sum + Number(item.calls || 0), 0) + " 次"],
      ["Token", total], ["估算成本", costKnown ? "$" + cost.toFixed(4) : "费率未配置"],
      ["单智能体基准", run.baseline_status || "未运行"],
    ];
    setHTML("agent-run", items.map(([label, value]) =>
      '<p><span>' + esc(label) + '</span><small>' + esc(String(value ?? "—")) + '</small></p>').join(""));
  }

  const usageRows = (ma.usage || []).map((item) => [
    esc(item.role || "single_agent"),
    esc(String(item.calls ?? 0)),
    esc(String(item.prompt_tokens ?? 0)),
    esc(String(item.completion_tokens ?? 0)),
    costKnown ? "$" + Number(item.cost_usd || 0).toFixed(4) : "费率未配置",
    item.calls ? (Number(item.latency_ms || 0) / Number(item.calls) / 1000).toFixed(1) + " 秒" : "—",
    esc(String(item.cached_calls ?? 0)),
  ]);
  setHTML("agent-usage", table(["角色", "调用", "输入 Token", "输出 Token", "成本", "平均延迟", "缓存"], usageRows,
    "今天还没有 API 调用记录"));
  setText("agent-api-date", s.now_et ? "交易日 " + shortDate(s.now_et) : "");

  const researchRows = byRole("researcher").map((item) => {
    const report = item.parsed || {};
    const critique = byRole("skeptic").find((row) => row.symbol === item.symbol)?.parsed || {};
    return [
      esc(item.symbol), esc(report.direction || "—"), esc(report.research_conclusion || "—"),
      esc(String(report.technical_structure || "").slice(0, 150)),
      esc(critique.overall_assessment || "未审查"),
      esc(critique.critique_severity || "—"),
      esc(String((report.supporting_evidence_ids || []).length) + " / " + String((report.counterevidence_ids || []).length)),
    ];
  });
  setHTML("agent-research", table(["股票", "方向", "研究员结论", "技术结构摘要", "反方评估", "严重度", "支持 / 反证"], researchRows,
    "本轮没有深度研究报告"));

  const plan = ma.latest_decision;
  const baseline = ma.baseline;
  setText("agent-decision-mode", "三智能体最终计划");
  if (!plan) {
    setHTML("agent-decision", '<div class="empty">本轮尚未形成基金经理计划；没有计划就没有交易授权。</div>');
    setText("agent-decision-foot", "");
  } else {
    const rows = (plan.candidates || []).map((candidate) => {
      const intent = candidate.order_intent || {};
      return [
        esc(candidate.symbol), esc(label(candidate.trade_decision)),
        esc(intent.qty ?? "—"), esc(intent.limit_price ?? "—"),
        esc(intent.stop_policy === "BROKER_STOP" ? "券商止损" : "AI 选择不设"),
        esc(candidate.research_report_id || "—"),
        esc((candidate.counterevidence || []).slice(0, 2).join("；") || "—"),
      ];
    });
    setHTML("agent-decision", table(["股票", "决策", "股数", "限价", "止损", "研究编号", "关键反证"], rows,
      "基金经理选择不交易 / 观望"));
    setText("agent-decision-foot", "市场判断：" + (plan.market_decision || "—") +
      " · 计划完成不等于已下单；执行仍由 Python 触发与风控决定。" +
      (baseline ? " 单智能体影子基准：" + (baseline.market_decision || "—") + "（不下单）。" : ""));
  }
  const experiment = (ma.experiments || [])[0];
  if (!experiment) {
    setHTML("agent-experiment", '<div class="empty">还没有对照实验记录。</div>');
  } else {
    const basePlan = experiment.baseline_decision || {};
    const threePlan = experiment.three_agent_decision || {};
    setHTML("agent-experiment", table(
      ["组别", "市场判断", "候选", "Token", "延迟"],
      [
        ["单智能体基准", esc(basePlan.market_decision || "—"),
          esc(String((basePlan.candidates || []).length)), esc(String(experiment.baseline_tokens ?? "—")),
          experiment.baseline_latency_ms != null ? esc((Number(experiment.baseline_latency_ms) / 1000).toFixed(1) + " 秒") : "—"],
        ["三智能体", esc(threePlan.market_decision || "—"),
          esc(String((threePlan.candidates || []).length)), esc(String(experiment.three_agent_tokens ?? "—")),
          experiment.three_agent_latency_ms != null ? esc((Number(experiment.three_agent_latency_ms) / 1000).toFixed(1) + " 秒") : "—"],
      ], "没有对照记录"));
  }

  const safety = [
    ["研究", run ? (run.status === "COMPLETE" ? "已完成" : run.status === "FAILED" ? "失败/已放弃" : "进行中或未完成") : "未运行"],
    ["最终计划", plan ? (plan.market_decision === "NO_TRADE" ? "NO_TRADE" : "已形成，等待触发") : "无计划"],
    ["交易执行", researchOnly ? "RESEARCH_ONLY：禁止券商写请求" : ((s.unresolved || []).length ? "存在未知订单，已冻结" : (s.paused ? "已暂停" : "由 Python 监测/风控"))],
    ["订单事实", (s.orders || []).length ? "最近订单见持仓页" : "没有订单"],
    ["单智能体基准", baseline ? "影子记录，不下单" : (ma.enabled ? "未完成" : "未启用")],
    ["安全门", s.freeze_reason ? "冻结：" + s.freeze_reason : "无冻结"],
  ];
  setHTML("agent-safety", safety.map(([labelText, value]) =>
    '<p><b>' + esc(labelText) + '</b>' + esc(value) + '</p>').join(""));
}

function populateSectors(groups) {
  const box = $("research-sector-chips");
  if (!box) return;
  const signature = groups.map((group) => group.id + ":" + group.name).join("|");
  if (box.dataset.ready === signature) return;
  box.innerHTML = '<button class="filter-chip active" type="button" data-sector="">全部板块</button>' +
    groups.map((group) => '<button class="filter-chip" type="button" data-sector="' + esc(String(group.id)) + '">' +
      esc(String(group.id).padStart(2, "0") + " · " + group.name) + '</button>').join("");
  box.dataset.ready = signature;
}

function markSectorChip() {
  document.querySelectorAll("#research-sector-chips .filter-chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.sector === researchState.sector);
  });
}

function renderResearchList(s) {
  const all = s.research || [];
  const groups = groupsOf(s);
  const lookup = groupLookup(groups);
  populateSectors(groups);
  markSectorChip();
  const query = researchState.query.trim().toLowerCase();
  const filtered = all.filter((item) => {
    const group = lookup.get(item.symbol);
    if (researchState.sector && String(group?.id || "") !== researchState.sector) return false;
    if (researchState.status && item.status !== researchState.status) return false;
    if (!query) return true;
    const evidenceText = (item.latest_evidence || []).map((e) => e.title || "").join(" ");
    return `${item.symbol} ${groupText(group)} ${evidenceText}`.toLowerCase().includes(query);
  });
  setText("research-count", `${filtered.length}/${all.length} 只 · ${groups.length} 个板块`);
  setHTML("research-empty", filtered.length ? "" : "没有匹配的研究档案");
  $("research-empty").hidden = filtered.length > 0;
  const sections = groups.map((group) => {
    const rows = (group.symbols || [])
      .map((symbol) => filtered.find((item) => item.symbol === symbol))
      .filter(Boolean);
    if (!rows.length) return "";
    const showGroupTag = Boolean(researchState.sector);
    const cards = rows.map((item) => {
      const status = label(item.status, "待更新");
      return '<button class="research-card" type="button" data-symbol="' + esc(item.symbol) + '">' +
        '<div class="research-card-top"><span class="research-card-symbol">' + esc(item.symbol) +
        '</span><span class="plan-tag ' + (item.status === "OK" ? "active" : "invalid") + '">' + esc(status) + '</span></div>' +
        (showGroupTag ? '<div class="research-card-sector">' + esc(groupText(group)) + '</div>' : '') +
        '<div class="research-card-meta"><span>' + esc(item.evidence_count || 0) + ' 条证据</span><span>' +
        esc(shortDate(item.verified_at)) + '</span></div></button>';
    }).join("");
    return '<section class="research-group">' +
      '<div class="research-group-head"><span class="research-group-no">' +
      String(group.id).padStart(2, "0") + '</span><h3>' + esc(group.name) +
      '</h3><span class="research-group-count">' + rows.length + ' / ' + groupCount(group) +
      ' 只</span></div><div class="research-group-grid">' + cards + '</div></section>';
  }).join("");
  setHTML("research-grid", sections || '<div class="empty">暂无研究档案</div>');
  if (researchState.detail) {
    $("research-grid").hidden = true;
    $("research-detail").hidden = false;
  } else {
    $("research-grid").hidden = false;
    $("research-detail").hidden = true;
  }
}

function researchSection(title, rows, fallback) {
  const items = asList(rows).filter(Boolean);
  return '<section class="research-section"><h4>' + esc(title) + '</h4>' +
    (items.length ? '<ul>' + items.slice(0, 8).map((item) => '<li>' + esc(item) + '</li>').join("") + '</ul>' :
      '<p>' + esc(fallback || "当前档案没有这一项。") + '</p>') + '</section>';
}

function renderResearchDetail(data) {
  researchDetailData = data;
  const profile = data.profile || {};
  const group = groupOf(data.symbol, state || {});
  const sector = groupText(group);
  const evidence = (data.evidence || []);
  const news = evidence.filter((item) => ["news", "ir", "filing"].includes(item.kind));
  const shown = (news.length ? news : evidence).slice(0, 10);
  const riskFlags = (data.plans || []).flatMap((plan) => plan.candidate?.risk_flags || []);
  const planRows = data.plans || [];
  const detail = $("research-detail");
  detail.innerHTML = '<div class="research-detail-head"><div class="research-detail-title">' +
    '<h2 id="research-detail-symbol" style="view-transition-name:research-symbol">' + esc(data.symbol) + '</h2>' +
    '<span class="plan-tag ' + (data.status === "OK" ? "active" : "invalid") + '">' + esc(label(data.status)) + '</span></div>' +
    '<div class="research-detail-actions"><button id="research-back" class="research-back" type="button">← 返回档案</button></div></div>' +
    '<div class="research-note">板块分组是本地研究标签；业务、财务、新闻、风险等内容均来自当前数据库档案与已验证证据，没有为缺失字段编造内容。</div>' +
    '<div class="research-detail-grid">' +
    '<section class="research-section"><h4>业务与行业线索</h4><p><b>' + esc(sector) + '</b> · ' +
    esc(data.symbol) + '</p><p>研究档案没有单列“主营业务”字段；以下事实来自档案，可用于判断业务与行业。</p>' +
    (asList(profile.facts).length ? '<ul>' + asList(profile.facts).slice(0, 6).map((item) => '<li>' + esc(item) + '</li>').join("") + '</ul>' :
      '<p>当前档案没有业务事实。</p>') + '</section>' +
    researchSection("AI 解读", profile.interpretations, "当前档案没有额外解读。") +
    researchSection("催化与风险线索", [
      ...asList(profile.assumptions), ...asList(profile.unknowns),
      ...asList(profile.invalidation).map((item) => `失效条件：${conditionText(item)}`),
      ...riskFlags.map((item) => `风险标记：${String(item).replaceAll("_", " ")}`),
    ], "当前档案没有催化/风险线索。") +
    '<section class="research-section"><h4>最新新闻与证据</h4>' +
    (shown.length ? shown.map((item) => {
      const title = item.title || item.source_url || "来源";
      const meta = [item.published_at ? shortDate(item.published_at) : "", item.kind,
        item.verified ? "已验证" : "未验证"].filter(Boolean).join(" · ");
      return '<div class="research-evidence">' +
        '<a href="' + esc(item.source_url || "#") + '" target="_blank" rel="noopener noreferrer">' + esc(title) + '</a>' +
        '<small>' + esc(meta) + (item.summary ? " · " + esc(String(item.summary).slice(0, 220)) : "") + '</small></div>';
    }).join("") : '<p>当前档案没有可展示证据。</p>') + '</section>' +
    researchSection("研究档案信息", [
      `版本 ${data.version ?? "—"}`,
      data.verified_at ? `核验 ${date(data.verified_at)}` : "",
      data.expires_at ? `过期 ${date(data.expires_at)}` : "",
      `${evidence.length} 条证据`,
    ], "暂无档案信息。") +
    '<section class="research-section"><h4>相关交易计划</h4>' +
    (planRows.length ? planRows.slice(0, 5).map((plan) => {
      const candidate = plan.candidate || {};
      return '<div class="research-evidence"><b>' + esc(plan.day || "") + ' · ' + esc(label(candidate.trade_decision, plan.status)) +
        '</b><small>' + esc((candidate.analysis || "").slice(0, 220)) + '</small></div>';
    }).join("") : '<p>该股票暂时没有交易计划。</p>') + '</section>' +
    '</div>';
  detail.hidden = false;
  $("research-grid").hidden = true;
  $("research-empty").hidden = true;
  const back = $("research-back");
  if (back) back.onclick = closeResearch;
}

async function openResearch(symbol, card) {
  if (!symbol) return;
  researchState.detail = symbol;
  researchState.scroll = $("research-grid").scrollTop;
  if (card) card.style.viewTransitionName = "research-symbol";
  const update = () => {
    $("research-detail").hidden = false;
    $("research-detail").innerHTML = '<div class="research-detail-head"><div class="research-detail-title">' +
      '<h2 style="view-transition-name:research-symbol">' + esc(symbol) + '</h2></div></div>' +
      '<div class="research-note">正在读取真实研究档案…</div>';
    $("research-grid").hidden = true;
    $("research-empty").hidden = true;
  };
  const transition = withViewTransition(update);
  if (transition) transition.finished.finally(() => { if (card) card.style.viewTransitionName = ""; });
  try {
    const response = await fetch("/api/research/" + encodeURIComponent(symbol), {signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error("研究档案暂时无法读取");
    renderResearchDetail(await response.json());
  } catch (error) {
    $("research-detail").innerHTML = '<div class="research-detail-head"><div class="research-detail-title">' +
      '<h2>' + esc(symbol) + '</h2></div><div class="research-detail-actions"><button id="research-back" class="research-back" type="button">← 返回档案</button></div></div>' +
      '<div class="research-note">' + esc(error.message) + '</div>';
    const back = $("research-back");
    if (back) back.onclick = closeResearch;
  }
}

function closeResearch() {
  researchState.detail = null;
  const update = () => {
    $("research-detail").hidden = true;
    $("research-grid").hidden = false;
    $("research-empty").hidden = false;
  };
  withViewTransition(update);
  requestAnimationFrame(() => { $("research-grid").scrollTop = researchState.scroll; });
}

function moveNavPill() {
  const nav = document.querySelector("nav");
  const active = document.querySelector("nav a.active");
  const pill = $("nav-pill");
  if (!nav || !active || !pill) return;
  const navRect = nav.getBoundingClientRect();
  const activeRect = active.getBoundingClientRect();
  if (!activeRect.height) { pill.hidden = true; return; }
  pill.hidden = false;
  pill.style.height = activeRect.height + "px";
  pill.style.transform = "translateY(" + (activeRect.top - navRect.top) + "px)";
}

const asList = (value) => Array.isArray(value) ? value : value ? [value] : [];

function togglePlanCard(card) {
  if (!card) return;
  const id = card.dataset.planId || "";
  const open = !card.classList.contains("open");
  card.classList.toggle("open", open);
  card.setAttribute("aria-expanded", String(open));
  if (open) planExpanded.add(id);
  else planExpanded.delete(id);
}

function robotTaskText(s) {
  if (s.freeze_reason || s.safety_reason) return "安全冻结，等待人工核对；不会提交新订单。";
  if (!s.running) return "等待本地 supervisor 在交易日启动。";
  if (s.paused) return "交易已暂停；已有券商委托仍可能成交。";
  if (s.phase === "PREMARKET") return "正在准备今天的 AI 盘前计划。";
  if (s.phase === "ACTIVE") return robotMonitor.length
    ? "正在监测 " + robotMonitor.length + " 个有效价格条件。"
    : "正在监测 AI 触发条件，等待行情或复核请求。";
  if (s.phase === "NO_SESSION") return "今天是休市日，等待下个交易日。";
  return "交易窗口已结束，等待下个交易日。";
}

function renderRobotStatus(s) {
  if (!s) return;
  const status = statusSummary(s);
  const stateElement = $("robot-state");
  if (stateElement) {
    stateElement.textContent = status.label;
    stateElement.className = status.tone;
  }
  setText("robot-task", robotTaskText(s));
  setText("robot-live-copy", robotError ? "日志暂时不可用 · 保留最近记录" : "真实系统数据 · 每 3 秒增量更新");
}

function conditionLabel(condition) {
  return conditionText(condition || {});
}

function renderRobotMonitor() {
  const box = $("robot-monitor");
  if (!box) return;
  const items = robotMonitor || [];
  setText("robot-monitor-count", items.length ? items.length + " 个条件" : "");
  if (!items.length) {
    box.innerHTML = '<div class="robot-empty">当前没有有效监测条件。</div>';
    return;
  }
  box.innerHTML = items.slice(0, 12).map((item) => {
    const conditions = (item.triggers || []).map((trigger) =>
      '<div class="condition">' + esc(trigger.purpose || "TRIGGER") + " · " +
      esc((trigger.all_of || []).map(conditionLabel).join(" 且 ")) + '</div>').join("");
    return '<div class="robot-monitor-card"><div class="robot-monitor-card-top"><b>' + esc(item.symbol) +
      '</b><span class="plan-tag active">' + esc(label(item.decision, "监测中")) + '</span></div>' +
      '<p>有效期 ' + esc(date(item.valid_until)) + '</p>' + conditions + '</div>';
  }).join("");
}

function renderRobotLog() {
  const box = $("robot-timeline");
  if (!box) return;
  const categories = {all: "全部", ai: "AI 决策", execution: "交易执行", fill: "订单成交", system: "系统"};
  const rows = [...robotEvents.values()]
    .filter((event) => robotFilter === "all" || event.category === robotFilter)
    .sort((a, b) => b.id - a.id);
  setText("robot-log-count", rows.length + " 条");
  setText("robot-log-updated", robotServerTime ? "更新于 " + time(robotServerTime) : "等待同步");
  const renderKey = robotFilter + "|" + rows.map((event) => event.id + ":" + event.severity).join(",");
  if (renderKey === robotRenderKey) return;
  robotRenderKey = renderKey;
  if (robotError && !rows.length) {
    box.innerHTML = '<div class="robot-empty">日志暂时不可用：' + esc(robotError) + '</div>';
    return;
  }
  if (!rows.length) {
    box.innerHTML = '<div class="robot-empty">当前筛选下没有行为记录。</div>';
    return;
  }
  const oldHeight = box.scrollHeight;
  const nearTop = box.scrollTop < 55;
  box.innerHTML = rows.map((event) => {
    const open = robotExpanded.has(event.id);
    const sourceClass = String(event.source || "system").toLowerCase().replace(/[^a-z]/g, "");
    const detail = event.detail || {};
    const decisions = event.decision ? '<dt>AI 决策</dt><dd>' + esc(event.decision) + '</dd>' : "";
    const cid = event.cid ? '<dt>订单编号</dt><dd>' + esc(event.cid) + '</dd>' : "";
    const positionIntent = event.detail?.position_intent
      ? '<dt>持仓意图</dt><dd>' + esc(event.detail.position_intent) + '</dd>' : "";
    return '<article class="robot-event ' + esc(event.category) + ' ' + esc(event.severity) +
      (open ? " open" : "") + (robotNewIds.has(event.id) ? " new" : "") +
      '" data-event-id="' + event.id + '" role="button" tabindex="0" aria-expanded="' + String(open) + '">' +
      '<span class="robot-dot" aria-hidden="true"></span><div class="robot-event-main">' +
      '<div class="robot-event-meta"><time datetime="' + esc(event.ts) + '">' + esc(date(event.ts)) +
      '</time><span class="robot-source ' + esc(sourceClass) + '">' + esc(event.source) + '</span>' +
      '<span>' + esc(categories[event.category] || event.category) + '</span>' +
      (event.symbol ? '<span class="robot-event-symbol">' + esc(event.symbol) + '</span>' : '') + '</div>' +
      '<div class="robot-event-title">' + esc(event.title) + '</div>' +
      '<div class="robot-event-summary">' + esc(event.summary || "") + '</div>' +
      '<div class="robot-event-detail"><div><div class="robot-event-detail-inner"><dl>' +
      '<dt>来源</dt><dd>' + esc(event.source) + '</dd><dt>事件类型</dt><dd>' + esc(event.kind) + '</dd>' +
      decisions + cid + positionIntent + '</dl><pre>' + esc(JSON.stringify(detail, null, 2)) + '</pre></div></div></div>' +
      '</div></article>';
  }).join("");
  if (!nearTop) box.scrollTop += box.scrollHeight - oldHeight;
  robotNewIds.clear();
}

async function loadRobotLog(initial = false) {
  if (robotLoading) return;
  robotLoading = true;
  try {
    const after = initial ? 0 : robotCursor;
    const response = await fetch("/api/robot-events?after_id=" + after + "&limit=200", {
      signal: AbortSignal.timeout(12000),
    });
    if (!response.ok) throw new Error("状态服务未返回日志");
    const data = await response.json();
    const received = data.events || [];
    if (initial) robotEvents.clear();
    for (const event of received) {
      if (!robotEvents.has(event.id)) {
        robotEvents.set(event.id, event);
        if (!initial) robotNewIds.add(event.id);
      }
    }
    robotCursor = Math.max(robotCursor, Number(data.next_after || 0));
    robotMonitor = data.monitoring || [];
    robotServerTime = data.server_time || robotServerTime;
    robotError = null;
    renderRobotMonitor();
    renderRobotLog();
    renderRobotStatus(state);
  } catch (error) {
    robotError = error.name === "TimeoutError" ? "请求超时" : error.message;
    renderRobotStatus(state);
    renderRobotLog();
  } finally {
    robotLoading = false;
  }
}

function render(s) {
  state = s;
  token = s.control_token;
  const accountFresh = !!s.account && !s.account_stale && !s.account_error && s.account_age_seconds != null && s.account_age_seconds < 120;
  const worker = healthy(s.health?.worker);
  const watched = healthy(s.health?.watchdog);
  const lastAI = s.ai_calls?.[0];
  setText("today", new Date(s.now).toLocaleDateString("zh-CN", {
    timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long"
  }));
  setValue("equity", money(s.account?.equity));
  setText("cash", "账户现金 $" + money(s.account?.cash));
  setValue("pnl", s.daily_pnl == null ? "—" : (s.daily_pnl > 0 ? "+" : "") + money(s.daily_pnl));
  $("pnl").className = "value " + (s.daily_pnl > 0 ? "positive" : s.daily_pnl < 0 ? "negative" : "");
  setValue("return", percent(s.return));
  setValue("drawdown", percent(s.drawdown));
  const status = statusSummary(s);
  setText("process", status.label);
  $("process").className = "badge " + status.tone;
  setText("session-title", status.heading);
  setText("session-copy", status.copy);
  if (activePage === "overview") {
    setText("page-subtitle", accountFresh ? "账户、持仓与 AI 的最新进展。" : "账户信息等待更新，当前显示最近记录。");
  }
  ["research", "trade", "rest"].forEach((part) => $("stage-" + part).classList.remove("current"));
  if (s.phase === "PREMARKET") $("stage-research").classList.add("current");
  else if (s.phase === "ACTIVE") $("stage-trade").classList.add("current");
  else if (s.phase === "CLOSED" && s.session_reason !== "before_premarket") $("stage-rest").classList.add("current");
  $("broker-connection").innerHTML = dot(accountFresh) + (accountFresh ? "Alpaca · 已同步" : "Alpaca · 等待同步");
  setText("ai-connection", lastAI ? "AI · 最近分析 " + shortDate(lastAI.ts) + " " + time(lastAI.ts) +
    (lastAI.outcome === "ok" ? "" : " · 未完成") : "AI · 暂无分析记录");
  setText("refresh", "更新于 " + time(s.now) + " · 每 5 秒刷新");
  setText("account-time", s.account_asof ? "账户更新 " + time(s.account_asof) : "等待账户同步");
  renderBrief(s);

  const alerts = [];
  if (s.freeze_reason || s.safety_reason) alerts.push("安全冻结：系统不会提交新订单，需人工核对后恢复。");
  if (s.unresolved?.length) alerts.push("存在需要核对的订单，暂不能恢复自动交易。");
  if (s.config_pending_restart) alerts.push("配置已更新，请从「更多」安全重启机器人后再继续交易。");
  if (s.running && (!worker || !watched)) alerts.push("机器人或订单同步暂未确认健康，请查看运行详情。");
  if (!accountFresh) alerts.push(s.account ? "账户数据尚未更新，金额可能不是最新值。" : "暂时无法读取账户数据。");
  if (s.recent_orders_error) alerts.push("历史订单同步失败，当前记录可能不完整。");
  if ((s.orders || []).some((o) => o.state_mismatch)) alerts.push("部分订单本地记录与券商状态不一致，已优先显示券商事实。");
  setHTML("notice", esc(alerts.join(" ")) + (alerts.length ? ' <a href="#activity">查看详情</a>' : ""));
  $("notice").hidden = alerts.length === 0;

  const positions = Object.entries(s.account?.positions || {});
  setText("position-count", positions.length ? positions.length + " 只" : "");
  setHTML("position-table", table(["方向", "股票", "持有股数", "市值 / USD", "平均成本", "浮动盈亏", "止损"], positions.map(([symbol, value]) => {
    const d = s.account.position_details?.[symbol] || {};
    const short = Number(s.account.quantities?.[symbol] || 0) < 0;
    return [short ? "空" : "多", symbol, Math.abs(Number(s.account.quantities?.[symbol] || 0)), money(value),
      money(d.avg_entry_price), money(d.unrealized_pl), STOP[d.stop_status] || "待核实"];
  }), s.account ? "当前没有持仓" : "等待账户数据"));
  const shorting = s.shorting || {};
  setText("short-status", "做空：" + (shorting.configured ? "已配置" : "未启用") +
    " · Alpaca " + (shorting.account_enabled ? "允许" : "不允许") +
    " · 空头市值 $" + money(shorting.short_market_value) +
    " · 购买力 $" + money(shorting.buying_power) +
    (shorting.latest_check ? " · 最近检查 " + (shorting.latest_check.payload?.outcome || "") : ""));
  const orderRows = (s.orders || []).slice(0, 8).map((o) => [
    o.symbol, ({buy: "买入", sell: "卖出", BUY: "买入", SELL: "卖出"})[o.side || o.action] || "—",
    ({buy_to_open: "买入开多", sell_to_close: "卖出平多", sell_to_open: "卖空开仓", buy_to_close: "买入平空"})[o.position_intent] || o.position_intent || "—",
    (o.qty ?? o.quantity ?? "—") + " / " + (o.filled_qty ?? "—"), money(o.limit_price),
    label(o.status, label(o.state))
  ]);
  setHTML("order-table", table(["股票", "方向", "持仓意图", "股数 / 已成交", "限价", "状态"], orderRows, "暂时没有订单"));
  setText("pending", s.unresolved?.length ? s.unresolved.length + " 笔待核对" : "最近 8 笔");
  renderPlans(s);
  renderAgents(s);
  setHTML("ai-list", (s.ai_calls || []).slice(0, 6).map((a) =>
    '<div class="ai-row"><div><strong>' + esc(MODE[a.mode] || "AI 分析") + '</strong><small>' +
    esc(shortDate(a.ts) + " " + time(a.ts) + " · " + a.model) + '</small></div><div>' +
    badge(a.outcome === "ok" ? "已完成" : "未完成", a.outcome === "ok" ? "" : "warn") +
    '<small>' + (a.latency_ms != null ? esc((a.latency_ms / 1000).toFixed(1)) + " 秒" : "") + '</small></div></div>'
  ).join("") || '<div class="empty">还没有 AI 分析记录</div>');
  renderResearchList(s);
  renderRobotStatus(s);
  const seen = new Set();
  const events = [...(s.events || [])].reverse().filter((e) => !["input", "ai_raw"].includes(e.kind)).filter((e) => {
    const key = eventMessage(e).join("|");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 12);
  setHTML("events", events.map((e) => {
    const [title, detail] = eventMessage(e);
    return '<div class="event"><time datetime="' + esc(e.ts) + '">' + esc(shortDate(e.ts) + " " + time(e.ts)) +
      '</time><div><strong>' + esc(title) + '</strong><p>' + esc(detail) + '</p></div></div>';
  }).join("") || '<div class="empty">暂无运行记录</div>');
  setText("symbol-count", groupsOf(s).length + " 个板块 · " + (s.symbols || []).length + " 只");
  renderSymbols();
  setText("system-summary", alerts.length ? "有事项需要确认" : "连接与账户设置");
  setText("watchdog", watched ? "同步正常 · " + time(s.health.watchdog.last_success) : s.running ? "等待成功对账" : "机器人未运行");
  setText("broker-stop", s.broker_stop === "VERIFIED" ? "已验证" : "未验证 · 暂不可用");
  setText("available", "$" + money(s.account?.available_non_margin_cash ?? s.account?.non_margin_cash ?? s.account?.cash));
  setText("config", s.config_pending_restart ? "等待重启生效" : "已生效");
  setText("source", s.account_source || "暂无数据");
  setText("now-et", time(s.now, "America/New_York") + " ET");
  setText("version", "美股 · 模拟交易");
  setText("diagnostic-data", JSON.stringify({
    配置: s.active_config_hash, 冻结原因: s.freeze_reason || s.safety_reason || null,
    账户错误: s.account_error, 历史订单错误: s.recent_orders_error, 心跳: s.health,
    订单: (s.orders || []).map((o) => ({股票: o.symbol, 日期: o.day, 订单编号: o.cid, 券商状态: o.status,
      本地状态: o.local_state || o.state, 止损: o.stop_status})),
    事件: events, 模型调用: (s.ai_calls || []).map((a) => ({时间: a.ts, 类型: a.mode,
      模型: a.model, 输入用量: a.prompt_tokens, 输出用量: a.completion_tokens, 结果: a.outcome, 错误: a.error}))
  }, null, 2));
  updateControls();
  requestAnimationFrame(moveNavPill);
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
$("research-search").oninput = () => {
  researchState.query = $("research-search").value;
  renderResearchList(state || {});
};
$("research-sector-chips").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-sector]");
  if (!chip) return;
  researchState.sector = chip.dataset.sector || "";
  markSectorChip();
  renderResearchList(state || {});
});
$("research-status").onchange = () => {
  researchState.status = $("research-status").value;
  renderResearchList(state || {});
};
$("research-grid").addEventListener("click", (event) => {
  const card = event.target.closest(".research-card");
  if (card) openResearch(card.dataset.symbol, card);
});
$("plan-list").addEventListener("click", (event) => {
  const card = event.target.closest(".plan-card");
  if (card) togglePlanCard(card);
});
$("plan-list").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const card = event.target.closest(".plan-card");
  if (!card) return;
  event.preventDefault();
  togglePlanCard(card);
});
document.querySelectorAll("[data-robot-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    robotFilter = button.dataset.robotFilter || "all";
    document.querySelectorAll("[data-robot-filter]").forEach((item) =>
      item.classList.toggle("active", item === button));
    robotRenderKey = "";
    renderRobotLog();
  });
});
$("robot-timeline").addEventListener("click", (event) => {
  const item = event.target.closest(".robot-event");
  if (!item) return;
  const id = Number(item.dataset.eventId);
  const open = !item.classList.contains("open");
  item.classList.toggle("open", open);
  item.setAttribute("aria-expanded", String(open));
  if (open) robotExpanded.add(id);
  else robotExpanded.delete(id);
});
$("robot-timeline").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const item = event.target.closest(".robot-event");
  if (!item) return;
  event.preventDefault();
  item.click();
});
$("sidebar-toggle").onclick = () => setSidebar(!document.body.classList.contains("sidebar-collapsed"));
$("mobile-nav").onclick = () => setMobileSidebar(!document.body.classList.contains("sidebar-open"));
$("sidebar-close").onclick = () => setMobileSidebar(false);
$("sidebar-backdrop").onclick = () => setMobileSidebar(false);
document.querySelectorAll("nav a[data-page]").forEach((link) => {
  link.addEventListener("click", () => setMobileSidebar(false));
});
window.addEventListener("hashchange", () => setPage(pageFromHash(), {scroll: false}));
window.addEventListener("resize", () => {
  if (window.innerWidth > 800) setMobileSidebar(false);
  requestAnimationFrame(moveNavPill);
});
setSidebar((() => { try { return localStorage.getItem("paper-sidebar-collapsed") === "1"; } catch { return false; } })());
setPage(pageFromHash(), {scroll: false});
refresh();
setInterval(refresh, 5000);
setInterval(() => {
  if (activePage === "robot" && !document.hidden) loadRobotLog();
}, 3000);

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
  const button = $("assistant-open");
  const panel = $("assistant-panel");
  const update = () => {
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.hidden = open;
    const modal = open && window.innerWidth <= 800;
    panel.setAttribute("aria-modal", String(modal));
    $("chat-backdrop").hidden = !modal;
    document.body.style.overflow = modal ? "hidden" : "";
    if (open) {
      markChatSeen();
      loadChat();
      $("chat-body").scrollTop = $("chat-body").scrollHeight;
    }
  };
  button.style.viewTransitionName = "assistant-entry";
  panel.style.viewTransitionName = "assistant-entry";
  const transition = withViewTransition(update);
  if (transition) transition.finished.finally(() => {
    button.style.viewTransitionName = "";
    panel.style.viewTransitionName = "";
  });
  else {
    button.style.viewTransitionName = "";
    panel.style.viewTransitionName = "";
  }
  requestAnimationFrame(() => {
    if (open) $("chat-input").focus();
    else button.focus();
  });
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
    el.classList.add("new");
    setTimeout(() => el.classList.remove("new"), 700);
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
