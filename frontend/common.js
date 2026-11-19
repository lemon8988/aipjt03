// 공통: API 호출, DOM 헬퍼(innerHTML 미사용 → XSS 방지), 상태 배너
const API = window.API_BASE || "http://localhost:8000";

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch (_) { /* 본문 없음 */ }
  if (!res.ok) {
    const err = new Error("request failed");
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid !== null && kid !== undefined && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

function safeUrl(u) {
  try { const x = new URL(u); return ["http:", "https:"].includes(x.protocol) ? x.href : null; } catch (_) { return null; }
}

// 서버는 UTC ISO 시각을 주므로 화면에서는 브라우저의 로컬 시간(KST)으로 바꿔 보여 준다
function fmtLocal(iso, withSec = false) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "-";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}` + (withSec ? `:${p(d.getSeconds())}` : "");
}

const PRIO_LABEL = { travel: "이동 부담 적게", things: "할 거 많게", food: "음식점 많게" };

// 사용자에게는 상세 오류를 그대로 보여 주지 않는다 (NF3002)
function friendlyError(e) {
  if (e && e.status === 422) return "입력 형식을 확인해 주세요.";
  if (e && e.status === 404) return "대상을 찾을 수 없어요.";
  if (e && e.status) return "서버에서 문제가 발생했어요. 잠시 후 다시 시도해 주세요.";
  return "서버에 연결하지 못했어요. 백엔드가 실행 중인지 확인해 주세요.";
}

async function showHealth(box) {
  box.replaceChildren();
  try {
    const h = await api("/api/health");
    if (!h.kakao_key) box.append(el("div", { class: "banner warn", text: "카카오 API 키가 설정되지 않았어요. 프로젝트 루트 .env 의 KAKAO_REST_KEY 를 채우고 서버를 다시 시작하세요." }));
    if (!h.llm_key) box.append(el("div", { class: "banner warn", text: "LLM API 키가 설정되지 않았어요. .env 의 LLM_API_KEY 를 확인하세요." }));
    if (h.route_provider === "mock") box.append(el("div", { class: "banner info", text: "개발용 모의 이동시간을 사용 중이에요. 표시되는 이동시간은 실제 값이 아니에요." }));
    if (!h.logging_agent) box.append(el("div", { class: "banner info", text: "로깅 Agent 가 꺼져 있어 실행 로그가 저장되지 않아요." }));
  } catch (e) {
    box.append(el("div", { class: "banner warn", text: friendlyError(e) }));
  }
}

function nav(active) {
  return el("nav", { class: "nav" },
    ...[["index.html", "대화"], ["plans.html", "계획 관리"], ["logs.html", "실행 로그"]].map(([href, label]) =>
      el("a", { href, class: href === active ? "active" : "", text: label })));
}

// ---- 로깅 Agent 평가 (F3020)
const VERDICT_LABEL = { ok: "정상", anomaly: "이상", unknown: "판정 불가" };
const RULE_LABEL = { repeated_call: "반복 호출", excess_replan: "재계획 과다", unplanned_call: "계획 외 호출" };
const CHECK_LABEL = { ok: "이상 없음", warn: "경고", na: "판정 불가" };

function verdictBadge(v) {
  return el("span", { class: `badge ${v || "none"}`, text: VERDICT_LABEL[v] || "미평가" });
}

// showDiagnosis=false 이면 진단 영역은 안내 문구만 (대화 화면)
function evaluationBox(ev, showDiagnosis = true) {
  const box = el("div", { class: "evalbox" });
  box.append(el("div", {}, el("b", { text: "로깅 Agent 판정 " }), verdictBadge(ev.verdict),
    el("span", { class: "meta", text: ` · 규칙 기반(LLM 미사용) · 경고 ${ev.warning_count}건` })));
  const chips = el("div", { class: "checks" });
  for (const [rule, st] of Object.entries(ev.checks)) chips.append(el("span", { class: `chip ${st}`, text: `${RULE_LABEL[rule] || rule}: ${CHECK_LABEL[st] || st}` }));
  box.append(chips);
  if (ev.verdict === "unknown") box.append(el("div", { class: "meta", text: "근거가 되는 이벤트가 없어 판정할 수 없어요. ('이상 없음'이 아니에요)" }));
  if (ev.warnings.length) box.append(el("ul", { class: "warnlist" }, ...ev.warnings.map((w) => el("li", { text: w.message }))));
  const d = ev.diagnosis || { status: "skipped" };
  if (!showDiagnosis) {
    if (ev.verdict === "anomaly") box.append(el("div", { class: "meta", text: "원인 진단은 '실행 로그' 화면에서 확인할 수 있어요." }));
    return box;
  }
  const dbox = el("div", { class: "diag" }, el("b", { text: "LLM 원인 진단 " }), el("span", { class: "meta", text: "(판정과 별개 · 경고 수에 포함되지 않아요)" }));
  if (d.status === "done") dbox.append(el("p", { text: d.text }));
  else if (d.status === "pending") dbox.append(el("p", { class: "meta", text: "진단 중이에요… 잠시 후 새로고침하세요." }));
  else if (d.status === "failed") dbox.append(el("p", { class: "meta", text: "진단에 실패했어요. 판정과 실행 로그는 그대로 남아 있어요." }));
  else dbox.append(el("p", { class: "meta", text: ev.verdict === "anomaly" ? "진단하지 않았어요." : "이상으로 판정되지 않아 LLM 진단을 하지 않았어요." }));
  box.append(dbox);
  return box;
}
