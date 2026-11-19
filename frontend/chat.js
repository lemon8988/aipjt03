// 대화 화면 (F3018, F3019)
document.getElementById("nav").replaceWith(nav("index.html"));
showHealth(document.getElementById("banners"));

const $messages = document.getElementById("messages");
const $input = document.getElementById("input");
const $send = document.getElementById("send");
const $trace = document.getElementById("trace");

let sessionId = null;
try { sessionId = sessionStorage.getItem("session_id"); } catch (_) { /* 저장소 사용 불가 */ }

function scrollDown() { $messages.scrollTop = $messages.scrollHeight; }
function bubble(cls, ...kids) { const b = el("div", { class: `msg ${cls}` }, ...kids); $messages.append(b); scrollDown(); return b; }

bubble("bot", "안녕하세요! 참여자들의 위치(2~4곳)와 날짜·시작 시각을 알려 주시면, 이동 부담이 적은 만남 역과 주변 장소를 추천해 드려요.");

// ---------------------------------------------------------------- 전송
async function send(message, choice, label) {
  if (!message && !choice) return;
  bubble("user", label || message);
  setBusy(true);
  const typing = bubble("bot typing", "처리 중이에요… (위치 해석 → 후보 역 → 이동시간 → 판단)");
  try {
    const body = { message: message || "" };
    if (sessionId) body.session_id = sessionId;
    if (choice) body.choice = choice;
    const data = await api("/api/chat", { method: "POST", body });
    sessionId = data.session_id;
    try { sessionStorage.setItem("session_id", sessionId); } catch (_) { /* 무시 */ }
    typing.remove();
    renderBot(data);
    renderTrace(data.timeline || []);
    if (data.evaluation) $trace.append(evaluationBox(data.evaluation, false));
  } catch (e) {
    typing.remove();
    bubble("bot error", friendlyError(e));
  } finally {
    setBusy(false);
  }
}

function setBusy(b) { $send.disabled = b; $input.disabled = b; if (!b) $input.focus(); }

document.getElementById("form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const m = $input.value.trim();
  if (!m) return;
  $input.value = "";
  send(m);
});

// ---------------------------------------------------------------- 답변 렌더링
function renderBot(d) {
  const b = bubble(d.type === "error" ? "bot error" : "bot");
  b.append(d.answer || "");
  if (d.ask && d.ask.choices && d.ask.choices.length) {
    const box = el("div", { class: "choices" });
    for (const c of d.ask.choices) {
      box.append(el("button", { class: "ghost", text: c.label, onclick: () => {
        box.querySelectorAll("button").forEach((x) => (x.disabled = true));
        send("", { kind: kindOf(d.ask.kind), value: c.value, name: d.ask.name || null }, c.label);
      } }));
    }
    b.append(box);
  }
  if (d.result) b.append(renderResult(d.result));
  scrollDown();
}

function kindOf(askKind) { return { priority: "priority", disambiguate: "disambiguate", relax: "patch" }[askKind]; }

function renderResult(r) {
  const box = el("div", { class: "result" });
  const c = r.criteria;
  box.append(el("p", { class: "meta", text: `${r.date_note} · 우선순위: ${c.prio_label} · 최대 이동시간 ${c.t_max}분 / 편차 ${c.g_max}분` }));
  const w = r.weather;
  if (w && w.rain_pct !== undefined) box.append(el("p", { class: "meta", text: `날씨: 강수확률 ${w.rain_pct}% · ${w.tmin}~${w.tmax}°C` + (w.rainy ? " (실내 위주)" : "") }));
  if (r.near_miss) box.append(el("p", { class: "meta", text: `가장 근접했던 후보: ${r.near_miss.name} (${r.near_miss.detail}, 초과 ${r.near_miss.excess}분)` }));
  for (const rec of r.recommendations) box.append(renderCard(rec, r));
  if (r.excluded.length) {
    box.append(el("details", { class: "excluded" }, el("summary", { text: `제외된 후보 ${r.excluded.length}곳` }),
      el("ul", {}, ...r.excluded.map((x) => el("li", { text: `${x.name}: ${x.reason}` })))));
  }
  if (r.notes.length) box.append(el("ul", { class: "notes" }, ...r.notes.map((n) => el("li", { text: n }))));
  return box;
}

function scoreLabel(prio, rec) {
  if (prio === "travel") return `이동시간 합 ${rec.sum_t}분`;
  if (prio === "food") return `음식점 ${rec.score}건`;
  return `주변 장소 ${rec.score}건`;
}

function renderCard(rec, r) {
  const card = el("div", { class: "card" });
  card.append(el("h3", {}, el("span", { class: "rank", text: `${rec.rank}.` }), rec.station));
  card.append(el("span", { class: "chip", text: scoreLabel(r.criteria.prio, rec) }),
              el("span", { class: "chip", text: `최대 ${rec.max_t}분 · 편차 ${rec.gap}분` }));
  card.append(el("table", { class: "t" },
    el("tr", {}, el("th", { text: "출발" }), el("th", { text: "이동시간" }), el("th", { text: "환승" })),
    ...rec.times.map((t) => el("tr", {}, el("td", { text: t.name }), el("td", { text: t.walk ? `${t.min}분 (도보 추정)` : `${t.min}분` }), el("td", { text: `${t.transfers}회` })))));
  const spotNames = [];
  const spots = el("div", { class: "spots" });
  for (const [cat, info] of Object.entries(rec.spots)) {
    spots.append(el("h4", { text: `${cat} · 반경 500m 안 ${info.total}건` }));
    const ul = el("ul");
    for (const it of info.items) {
      spotNames.push(it.name);
      const href = safeUrl(it.url);
      ul.append(el("li", {}, href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: it.name }) : it.name, ` (${it.dist_m}m)`));
    }
    spots.append(ul);
  }
  card.append(spots);

  const sel = el("select", {}, el("option", { value: "", text: "장소 선택 안 함" }), ...spotNames.map((n) => el("option", { value: n, text: n })));
  const msg = el("span", { class: "save-msg" });
  const btn = el("button", { text: "이 역으로 계획 저장", onclick: async () => {
    btn.disabled = true;
    try {
      const body = { ...r.plan_draft, station: rec.station, spot: sel.value || null };
      const res = await api("/api/plans", { method: "POST", body });
      if (res.conflicts.length) {
        msg.className = "save-msg warn";
        msg.textContent = `저장했어요. 단, 같은 날 시간이 겹치는 계획이 있어요: ` + res.conflicts.map((c) => `#${c.id}(${c.start_time}${c.end_time ? "~" + c.end_time : ""} ${c.station})`).join(", ");
      } else {
        msg.className = "save-msg ok";
        msg.textContent = `저장했어요 (#${res.plan.id}). 계획 관리에서 확인할 수 있어요.`;
      }
    } catch (e) {
      msg.className = "save-msg warn";
      msg.textContent = friendlyError(e);
      btn.disabled = false;
    }
  } });
  card.append(el("div", { class: "savebox" }, sel, btn, msg));
  return card;
}

// ---------------------------------------------------------------- 실행 과정 (단계 · Tool 호출 · 재계획)
const STEP_LABEL = { analyze: "① 분석", plan: "② 계획", execute: "③ 실행", judge: "④ 판단", replan: "↻ 재계획 준비", respond: "⑤ 응답" };

function renderTrace(timeline) {
  $trace.replaceChildren();
  $trace.classList.remove("meta");
  if (!timeline.length) { $trace.textContent = "표시할 실행 내역이 없어요."; return; }
  let cur = null;
  const finish = () => {
    if (!cur) return;
    const box = el("div", { class: "step" });
    box.append(el("b", { text: STEP_LABEL[cur.step] || cur.step }), el("span", { class: "ms", text: cur.ms !== null ? `${cur.ms}ms` : "" }));
    for (const line of cur.info) box.append(el("div", { class: "info", text: line }));
    const tools = Object.entries(cur.tools);
    if (tools.length) {
      const list = el("div");
      for (const [name, t] of tools) {
        const fails = Object.entries(t.codes).map(([c, n]) => `${c === "NO_RESULT" ? "결과 없음" : c}×${n}`).join(" ");
        list.append(el("div", {}, `🔧 ${name} ×${t.n} · 성공 ${t.ok}`, fails ? el("span", { class: "fail", text: ` · ${fails}` }) : "", ` · ${t.ms}ms`));
      }
      if (cur.cache) list.append(el("div", { class: "info", text: `♻ 이미 확보한 결과 재사용 ${cur.cache}건 (재호출 없음)` }));
      box.append(list);
    } else if (cur.cache) {
      box.append(el("div", { class: "info", text: `♻ 결과 재사용 ${cur.cache}건` }));
    }
    $trace.append(box);
    cur = null;
  };
  for (const e of timeline) {
    if (e.type === "step_start") { finish(); cur = { step: e.step, tools: {}, cache: 0, info: [], ms: null }; }
    else if (e.type === "tool_res" && cur) {
      const t = (cur.tools[e.tool] ||= { n: 0, ok: 0, codes: {}, ms: 0 });
      t.n++; t.ms += e.ms || 0;
      if (e.ok) t.ok++; else t.codes[e.code] = (t.codes[e.code] || 0) + 1;
    } else if (e.type === "cache_hit" && cur) cur.cache++;
    else if (e.type === "llm_decision" && cur) cur.info.push(`LLM 판단(${e.kind}): ${e.value} — ${e.reason}`);
    else if (e.type === "step_end" && cur) {
      cur.ms = e.ms;
      if (e.plan) cur.info.push(`계획: ${e.plan.join(" → ")}`);
      if (e.candidates !== undefined) cur.info.push(`후보 ${e.candidates}곳 중 제약 충족 ${e.passed}곳 · Tool 호출 ${e.tool_calls}회`);
      if (e.decision === "replan") cur.info.push("충족 후보 0개 → 재계획");
      if (e.status === "limit") cur.info.push("재계획 상한 도달");
      if (e.ask) cur.info.push(`되묻기: ${e.ask}`);
      if (e.error) cur.info.push(`오류: ${e.error}`);
    } else if (e.type === "replan") {
      finish();
      $trace.append(el("div", { class: "replan", text: `↻ 재계획 ${e.n}회 — ${e.change}` }));
    } else if (e.type === "run_end") {
      finish();
      $trace.append(el("div", { class: "meta", text: `완료: ${e.status} · 총 ${e.total_ms}ms` }));
    }
  }
  finish();
}
