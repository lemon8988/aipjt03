// 실행 로그 화면 (F3014): 세션 목록 · 상세(실행 순서) · 삭제. 수정 기능 없음.
document.getElementById("nav").replaceWith(nav("logs.html"));
showHealth(document.getElementById("banners"));

const $sessions = document.getElementById("sessions");
const $detail = document.getElementById("detail");
const $notice = document.getElementById("notice");
let selected = null;
document.getElementById("reload").addEventListener("click", load);

function fmt(ts) { return fmtLocal(ts, true); }

async function load() {
  try {
    const list = await api("/api/logs");
    if (!list.length) { $sessions.replaceChildren(el("div", { class: "empty", text: "저장된 실행 로그가 없어요." })); return; }
    $sessions.replaceChildren(...list.map((s) => el("div", { class: "list-item" + (s.session_id === selected ? " active" : ""), onclick: () => open(s.session_id) },
      el("div", {}, el("b", { text: s.session_id.slice(0, 12) + "… " }), verdictBadge(s.latest_verdict),
        s.anomaly_runs ? el("span", { class: "meta", text: ` 이상 ${s.anomaly_runs}회` }) : ""),
      el("small", { text: `${fmtLocal(s.last_at)} · 실행 ${s.runs}회 · 재계획 ${s.replans}회` }),
      el("small", { text: `마지막 결과: ${s.last_status || "-"} · 이벤트 ${s.event_count}건` }))));
  } catch (e) { $sessions.replaceChildren(el("div", { class: "empty", text: friendlyError(e) })); }
}

function describe(e) {
  switch (e.type) {
    case "step_start": return `▶ 단계 시작: ${e.step}`;
    case "step_end": return `■ 단계 종료: ${e.step} (${e.ms}ms)` + (e.intent ? ` · 의도 ${e.intent} — ${e.reason}` : "") + (e.plan ? ` · 계획 ${e.plan.join(" → ")}` : "");
    case "tool_req": return `→ Tool 호출: ${e.tool} ${JSON.stringify(e.args)}`;
    case "tool_res": return `← Tool 응답: ${e.tool} ${e.ok ? "성공" : e.code === "NO_RESULT" ? "결과 없음" : "실패(" + e.code + ")"} · ${e.ms}ms` + (e.count !== null && e.count !== undefined ? ` · ${e.count}건` : "");
    case "cache_hit": return `♻ 결과 재사용: ${e.tool}`;
    case "llm_decision": return `🧠 LLM 판단(${e.kind}): ${e.value} — ${e.reason}`;
    case "replan": return `↻ 재계획 ${e.n}회 (${e.trigger}): ${e.change}`;
    case "run_end": return `✔ 요청 종료: ${e.status} · 총 ${e.total_ms}ms`;
    default: return e.type;
  }
}

let pollTimer = null;

async function open(sid, attempt = 0) {
  selected = sid;
  clearTimeout(pollTimer);
  try {
    const d = await api(`/api/logs/${sid}`);
    const evs = (await api(`/api/logs/${sid}/evaluations`)).evaluations;
    // 진단은 백그라운드에서 끝나므로 진행 중이면 잠시 뒤 다시 읽는다 (최대 5회)
    if (evs.some((e) => e.diagnosis && e.diagnosis.status === "pending") && attempt < 5) pollTimer = setTimeout(() => open(sid, attempt + 1), 3000);
    const evalCards = evs.length
      ? el("div", {}, el("h3", { text: "실행별 판정" }), ...evs.map((e) => el("div", { class: "evalcard" },
          el("div", { class: "meta", text: `실행 #${e.run_index} · Tool ${e.stats.tool_calls}회 · 재계획 ${e.stats.replans}회 · ${e.stats.total_ms ?? "-"}ms` }),
          evaluationBox(e, true))))
      : el("div", { class: "meta", text: "이 세션에는 저장된 판정이 없어요." });
    const rows = d.events.map((e) => el("tr", { class: e.type === "replan" ? "replan" : "" },
      el("td", { text: e.seq }), el("td", { text: fmt(e.ts).slice(11) }), el("td", { class: e.type === "tool_res" && !e.ok ? "fail" : "", text: describe(e) })));
    $detail.replaceChildren(
      el("div", { class: "toolbar" }, el("b", { text: `세션 ${sid}` }), el("button", { class: "danger", text: "이 로그 삭제", onclick: () => remove(sid) })),
      evalCards,
      el("h3", { text: "이벤트 로그 (수정 불가)" }),
      el("div", { class: "scroll-x" }, el("table", { class: "t" }, el("tr", {}, el("th", { text: "#" }), el("th", { text: "시각" }), el("th", { text: "이벤트" })), ...rows)));
    await load();
  } catch (e) { $detail.replaceChildren(el("div", { class: "empty", text: friendlyError(e) })); }
}

async function remove(sid) {
  if (!confirm("이 세션의 실행 로그를 삭제할까요?")) return;
  try {
    await api(`/api/logs/${sid}`, { method: "DELETE" });
    selected = null;
    $notice.replaceChildren(el("div", { class: "banner info", text: "삭제했어요." }));
    $detail.replaceChildren(el("div", { class: "empty", text: "왼쪽에서 세션을 선택하세요." }));
    await load();
  } catch (e) { $notice.replaceChildren(el("div", { class: "banner err", text: friendlyError(e) })); }
}

load();
