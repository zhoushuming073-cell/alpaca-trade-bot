const $ = (id) => document.getElementById(id);
let state = null;
let token = null;
let busy = false;

const esc = (x) => String(x ?? "—").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const money = (x) => x == null ? "—" : Number(x).toLocaleString("en-US", {
  minimumFractionDigits: 2, maximumFractionDigits: 2
});
const percent = (x) => x == null ? "—" : (Number(x) * 100).toFixed(2) + "%";
const date = (x) => x ? new Date(x).toLocaleString("zh-CN", {
  timeZone: "Asia/Shanghai", hour12: false
}) : "—";
const et = (x) => x ? new Date(x).toLocaleString("en-US", {
  timeZone: "America/New_York", hour12: false
}) : "—";
const pill = (text, cls) => `<span class="pill ${cls || ""}">${esc(text)}</span>`;

function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;
}

const LABELS = {
  operator: "人工操作",
  error: "异常",
  safety_alert: "安全告警",
  order: "订单更新",
  order_not_sent: "订单未发送",
  order_imported: "导入券商订单",
  order_recovery: "崩溃恢复",
  premarket_plan: "盘前计划",
  universe_scan: "全池扫描",
  intraday_recheck: "盘中复核",
  plan: "计划状态",
  plan_invalidated: "计划失效",
  intent_rejected: "意图拒绝",
  config_pending: "配置待重启",
  watchdog_loop: "看门狗异常",
  research_profile: "研究档案",
  source_call: "研究来源调用",
  source_fetch_error: "来源抓取失败",
  research_news: "研究新闻",
  premarket_data: "盘前数据",
  quote_errors: "报价异常",
  recheck_suppressed: "复核抑制",
  startup_reconciliation: "启动对账",
  after_close_reconcile: "收盘后对账",
  ai_call: "AI 调用",
  input: "AI 输入",
};
const STATES = {
  RESERVED: "本地预留", VALIDATING: "本地校验", READY_TO_DISPATCH: "待发送",
  DISPATCH_INTENT_RECORDED: "已记录发送意图", SUBMISSION_OUTCOME_UNKNOWN: "发送结果未知",
  ACCEPTED: "券商已接受", PARTIALLY_FILLED: "部分成交", FILLED: "完全成交",
  CANCELED: "已取消", EXPIRED: "已过期", REJECTED: "已拒绝",
  RECONCILIATION_REQUIRED: "待人工对账", NOT_SENT: "确认未发送",
  BROKER_OPEN: "券商开放中", BROKER_CLOSED: "券商历史订单",
};
const STOP = {
  NONE_BY_AI: "AI 选择不设", REQUESTED: "已请求", VERIFIED: "已确认",
  FAILED: "建立失败", UNKNOWN: "未知",
};

function render(s) {
  state = s;
  token = s.control_token;
  $("process").textContent = s.running ? "机器人进程运行中" : "机器人未运行";
  $("process").className = "pill " + (s.running ? "ok" : "warn");
  $("phase").textContent = s.phase;
  $("phase").className = "pill " + (s.phase === "ACTIVE" ? "ok" : s.phase === "FROZEN" ? "warn" : "");
  $("paused").textContent = s.paused ? "下单已暂停" : "下单已启用";
  $("paused").className = "pill " + (s.paused ? "warn" : "ok");
  $("now-et").textContent = et(s.now_et) + " ET / 北京 " + date(s.now);
  $("refresh").textContent = "更新于 " + date(s.now) + " · 每 5 秒刷新";
  $("equity").textContent = money(s.account?.equity);
  $("cash").textContent = "现金 " + money(s.account?.cash);
  $("buying-power").textContent = "购买力 " + money(s.account?.buying_power);
  $("available").textContent = "非杠杆可用 " + money(
    s.account?.available_non_margin_cash ?? s.account?.non_margin_cash ?? s.account?.cash
  );
  $("pnl").textContent = money(s.daily_pnl);
  $("return").textContent = percent(s.return);
  $("drawdown").textContent = percent(s.drawdown);
  $("account-time").textContent = date(s.account?.ts);
  $("source").textContent = s.account_source + " · " + (
    s.account_age_seconds == null ? "尚无数据" :
    s.account_age_seconds < 60 ? Math.floor(s.account_age_seconds) + " 秒前的快照" :
    Math.floor(s.account_age_seconds / 60) + " 分钟前的快照"
  );
  $("config").textContent = "配置 " + (s.active_config_hash || "—") +
    (s.config_pending_restart ? " · 磁盘配置待安全重启" : " · 已生效");
  $("watchdog").textContent = "看门狗 " + (
    s.health?.watchdog?.alive && !s.health?.watchdog?.stale ? "正常" : "异常/无心跳"
  );
  $("watchdog").className = "pill " + (
    s.health?.watchdog?.alive && !s.health?.watchdog?.stale ? "ok" : "warn"
  );
  $("broker-stop").textContent = "BROKER_STOP " + s.broker_stop;
  $("broker-stop").className = "pill " + (s.broker_stop === "VERIFIED" ? "ok" : "warn");

  const alerts = [];
  if (s.freeze_reason) alerts.push("冻结新风险：" + s.freeze_reason);
  if (s.safety_reason) alerts.push("安全锁：" + s.safety_reason);
  if (s.config_pending_restart) alerts.push("配置文件已变化，必须安全重启后才会生效。");
  if (s.running && s.health?.watchdog && (!s.health.watchdog.alive || s.health.watchdog.stale)) {
    alerts.push("订单看门狗没有有效心跳；不能依赖自动撤单/对账。");
  }
  if (s.running && s.health?.worker && (!s.health.worker.alive || s.health.worker.stale)) {
    alerts.push("策略 worker 心跳超时；进程可能在运行但策略未推进。");
  }
  if (s.account_age_seconds > 120) alerts.push("账户快照超过 2 分钟，金额可能不是实时余额。");
  if (s.account_stale && s.account_source !== "ALPACA PAPER 不可用") {
    alerts.push("当前账户数据来自最近一次成功快照，不是实时 Alpaca 响应。");
  }
  if (s.account_error) alerts.push("Alpaca 账户同步异常：" + s.account_error);
  if (s.recent_orders_error) alerts.push("最近历史订单同步失败：" + s.recent_orders_error);
  if (!s.account) alerts.push("没有可用的 Alpaca Paper 账户数据；页面不会用模拟数据填充。");
  const stateMismatches = (s.orders || []).filter((o) => o.state_mismatch);
  if (stateMismatches.length) {
    alerts.push("有 " + stateMismatches.length + " 笔本地账本与券商事实状态不一致；以券商事实为准，需对账。");
  }
  if (s.broker_stop !== "VERIFIED") alerts.push("BROKER_STOP 未验证；AI 选择该策略的订单会被整体拒绝，不会降级为无止损。");
  $("notice").textContent = alerts.join(" ");
  $("notice").style.display = alerts.length ? "block" : "none";

  const positions = (s.account?.positions && Object.entries(s.account.positions)) || [];
  const positionRows = positions.map(([symbol, value]) => {
    const detail = (s.account.position_details || {})[symbol] || {};
    return [
      symbol, s.account.quantities?.[symbol] ?? "—", money(value),
      money(detail.avg_entry_price), money(detail.unrealized_pl),
      STOP[(detail.stop_status)] || detail.stop_status || "—",
    ];
  });
  $("position-table").innerHTML = table(
    ["标的", "股数", "市值 · USD", "均价", "浮盈亏", "止损状态"],
    positionRows, "当前没有记录到持仓"
  );

  const orderRows = (s.orders || []).map((o) => [
    o.day, o.symbol, o.side || o.action, o.qty ?? o.quantity,
    o.filled_qty ?? "—", o.limit_price ?? "—", o.time_in_force || "—",
    o.status || "—", o.state_mismatch ? "券商事实优先" :
      (STATES[o.state] || o.state || o.status),
    STATES[o.local_state] || o.local_state || "—",
    STOP[o.stop_status] || o.stop_status || "—",
  ]);
  $("order-table").innerHTML = table(
    ["日期", "标的", "方向", "委托", "已成交", "限价", "TIF", "券商状态", "状态", "本地账本", "止损"],
    orderRows, "尚无订单记录"
  );
  $("pending").textContent = (s.unresolved || []).length + " 笔未决订单";

  const planRows = (s.plans || []).map((p) => {
    const c = p.candidate || {};
    const intent = c.order_intent || {};
    const triggers = (c.triggers || []).length;
    return [
      p.plan_id, p.status, date(p.valid_until),
      c.trade_decision || "—", intent.qty ?? "—", intent.limit_price ?? "—",
      intent.stop_policy || "—", triggers,
    ];
  });
  $("plan-table").innerHTML = table(
    ["计划", "状态", "有效期", "决策", "股数", "限价", "止损", "触发数"],
    planRows, "今日没有生效计划"
  );

  const aiRows = (s.ai_calls || []).map((a) => [
    date(a.ts), a.mode, a.model, a.prompt_tokens ?? "—",
    a.completion_tokens ?? "—", a.latency_ms + " ms", a.outcome,
  ]);
  $("ai-table").innerHTML = table(
    ["时间", "模式", "模型", "输入 tokens", "输出 tokens", "延迟", "结果"],
    aiRows, "尚无 AI 调用记录"
  );

  const researchRows = (s.research || []).map((r) => [
    r.symbol, r.status, r.source_status || "—", r.evidence_count ?? 0,
    "v" + r.version, date(r.verified_at), date(r.expires_at),
  ]);
  $("research-table").innerHTML = table(
    ["标的", "档案状态", "来源状态", "证据数", "版本", "最近核验", "过期时间"],
    researchRows, "尚无研究档案（研究记忆未初始化）"
  );

  $("events").innerHTML = (s.events || []).map((e) => `
    <div class="event"><time>${esc(date(e.ts))}</time>
      <details><summary>${esc(LABELS[e.kind] || e.kind)}</summary>
        <pre>${esc(JSON.stringify(e.payload, null, 2))}</pre>
      </details>
    </div>`).join("") || '<div class="empty">暂无运行事件</div>';

  $("symbols").innerHTML = (s.symbols || []).filter((x) =>
    x.includes(($("search").value || "").toUpperCase())
  ).map((x) => `<span>${esc(x)}</span>`).join("") || "<p>无匹配标的</p>";
  $("symbol-count").textContent = "· " + (s.symbols || []).length;
  $("version").textContent = "策略版本：" + (s.strategy_version || "—");
}

async function refresh() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error();
    render(await response.json());
  } catch (error) {
    $("process").textContent = "网页服务连接失败";
    $("process").className = "pill warn";
    $("refresh").textContent = "更新失败，正在显示上次数据";
  }
}

async function control(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const response = await fetch("/api/control/" + action, {
      method: "POST", headers: { "X-Control-Token": token }
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error);
    const messages = {
      start: "已请求启动（保留暂停状态）",
      resume: "已恢复本地交易",
      pause: "已暂停新订单",
      restart: "已安全重启（保持暂停）",
      stop: "已停止机器人进程",
    };
    toast(messages[action]);
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  }
}

function toast(text) {
  $("toast").textContent = text;
  $("toast").style.display = "block";
  setTimeout(() => { $("toast").style.display = "none"; }, 7000);
}

["start", "pause", "stop", "restart"].forEach((action) => {
  $(action).onclick = () => control(action);
});
$("resume").onclick = () => $("confirm").showModal();
$("cancel-confirm").onclick = () => $("confirm").close();
$("confirm-resume").onclick = () => { $("confirm").close(); control("resume"); };
$("search").oninput = () => {
  if (state) {
    $("symbols").innerHTML = state.symbols.filter((x) =>
      x.includes(($("search").value || "").toUpperCase())
    ).map((x) => `<span>${esc(x)}</span>`).join("") || "<p>无匹配标的</p>";
  }
};

refresh();
setInterval(refresh, 5000);
