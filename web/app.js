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
  plans: {title: "AI 决策", subtitle: "OneShotEngine 的决策与订单结果。", crumb: "AI 决策"},
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
const MODE = {ONE_SHOT_DECISION: "AI 交易决策", ONE_SHOT_RESEARCH: "盘前研究", RESEARCH_REFRESH: "更新研究档案"};
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
    order_recovery: "订单状态恢复", one_shot_decision: "OneShot AI 决策",
    one_shot_result: "OneShot 订单结果", one_shot_research: "OneShot 盘前研究",
    one_shot_research_request: "OneShot 深入研究", one_shot_error: "OneShot 输出错误",
    premarket_plan: "历史盘前计划", universe_scan: "历史股票扫描",
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
    detail = "历史盘前计划记录，仅供回顾。";
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
let researchState = {query: "", sector: "", status: "", detail: null, scroll: 0};
let researchDetailData = null;
let robotEvents = new Map();
let robotNewIds = new Set();
let robotExpanded = new Set();
let robotCursor = 0;
let robotFilter = "all";
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
    return {label: "运行中", tone: "ok", heading: "正在监测交易机会", copy: "OneShotEngine 正在分析账户与市场；安全校验通过后立即提交限价单。"};
  }
  if (s.phase === "PREMARKET") {
    return {label: "盘前准备", tone: "ok", heading: "正在准备今日研究", copy: "AI 正在结合账户、研究档案和最新行情准备研究。"};
  }
  if (s.phase === "NO_SESSION") {
    return {label: "运行中", tone: "idle", heading: "今天休市", copy: "没有新的交易安排，等待下一个交易日。"};
  }
  return {label: "运行中", tone: "idle", heading: "今天的交易窗口已结束", copy: "不再发起新的本地订单；已有委托可能继续成交。"};
}

function renderBrief(s) {
  const brief = s.brief || {};
  const alias = {BUY: "买入", SELL: "卖出", HOLD: "持有", WAIT: "观察", NO_TRADE: "不交易",
    OPEN_LONG: "做多", CLOSE_LONG: "平多", OPEN_SHORT: "做空", COVER_SHORT: "平空"};
  const focus = (brief.focus || []).slice(0, 4);
  setHTML("brief-focus", focus.length ? focus.map((item) =>
    '<div class="brief-focus-item"><b>' + esc(item.symbol) + '</b><span>' +
    esc(alias[item.decision] || item.decision || "等待判断") + ' · ' + esc(item.analysis || "等待 AI 分析") +
    '</span><em>' + esc(item.status || "研究关注") +
    '</em></div>').join("") : '<p class="empty">今天暂时没有研究关注标的。</p>');
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

function renderDecisions(s) {
  const rows = (s.decisions || []).slice(0, 12);
  setText("plan-count", rows.length + " 条决策");
  setHTML("plan-list", rows.map((row) => {
    const d = row.payload || {};
    const order = d.order_intent || {};
    return '<article class="plan-card"><div class="plan-head"><span class="plan-symbol">' +
      esc(order.symbol || d.action || "AI 决策") + '</span><span class="plan-tag active">' +
      esc(d.action || "—") + '</span></div><p class="plan-thesis">' + esc(d.reason || "未记录理由") +
      '</p><div class="plan-meta"><span>' + esc(shortDate(row.ts) + " " + time(row.ts)) +
      '</span><span>' + esc(order.qty ? order.qty + " 股 · 限价 " + order.limit_price : "无新订单") +
      '</span></div></article>';
  }).join("") || '<div class="empty">尚无 OneShot 决策。</div>');
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
  const decisions = data.decisions || [];
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
    '<section class="research-section"><h4>相关 AI 决策</h4>' +
    (decisions.length ? decisions.slice(0, 5).map((row) => {
      const d = row.payload || {};
      return '<div class="research-evidence"><b>' + esc(shortDate(row.ts) + ' · ' + (d.action || '—')) +
        '</b><small>' + esc((d.reason || '').slice(0, 220)) + '</small></div>';
    }).join("") : '<p>该股票暂时没有 AI 决策。</p>') + '</section>' +
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

function robotTaskText(s) {
  if (s.freeze_reason || s.safety_reason) return "安全冻结，等待人工核对；不会提交新订单。";
  if (!s.running) return "等待本地 supervisor 在交易日启动。";
  if (s.paused) return "交易已暂停；已有券商委托仍可能成交。";
  if (s.phase === "PREMARKET") return "正在进行盘前研究。";
  if (s.phase === "ACTIVE") return "OneShotEngine 正在等待下一次 AI 决策；挂单由券商管理。";
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

    robotServerTime = data.server_time || robotServerTime;
    robotError = null;

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
  renderDecisions(s);
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
      const previousToken = token;
      render(next);
      if (token && token !== previousToken) void sendHeartbeat();
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

// The browser is the liveness authority for new risk.  This heartbeat never
// clears a manual pause; it only proves that the page is still open.
async function sendHeartbeat() {
  if (!token) return;
  try {
    await fetch("/api/heartbeat", {
      method: "POST", headers: {"X-Control-Token": token},
      signal: AbortSignal.timeout(5000)
    });
  } catch { /* the server-side 30s stale gate will stop new risk */ }
}
function sendHeartbeatClose() {
  if (!token) return;
  // Unlike sendBeacon, keepalive fetch can carry the control header on pagehide.
  void fetch("/api/heartbeat/close", {
    method: "POST", headers: {"X-Control-Token": token}, keepalive: true
  }).catch(() => { /* server stale timeout is the fallback */ });
}
setInterval(sendHeartbeat, 10000);

function updateEtClock() {
  const time = new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false
  }).format(new Date());
  const node = document.getElementById("et-clock-time");
  if (node) node.textContent = time;
}
updateEtClock();
setInterval(updateEtClock, 1000);
document.addEventListener("visibilitychange", () => {
  // A background tab is still an open page; keep the heartbeat alive.
  if (!document.hidden) sendHeartbeat();
});
window.addEventListener("pagehide", sendHeartbeatClose);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) void sendHeartbeat();
});
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
