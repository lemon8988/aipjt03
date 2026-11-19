// 계획 관리 화면 (F3015, F3016): 목록 · 수정 · 삭제, 수정·삭제 결과는 목록에 바로 반영
document.getElementById("nav").replaceWith(nav("plans.html"));
showHealth(document.getElementById("banners"));

const $list = document.getElementById("list");
const $notice = document.getElementById("notice");
document.getElementById("reload").addEventListener("click", load);

function notice(cls, text) { $notice.replaceChildren(text ? el("div", { class: `banner ${cls}`, text }) : ""); }

async function load() {
  try {
    render(await api("/api/plans"));
  } catch (e) {
    $list.replaceChildren(el("div", { class: "empty", text: friendlyError(e) }));
  }
}

function render(plans, editingId = null) {
  if (!plans.length) { $list.replaceChildren(el("div", { class: "empty", text: "저장된 계획이 없어요. 대화 화면에서 추천받고 저장해 보세요." })); return; }
  const table = el("table", { class: "plan-table" },
    el("thead", {}, el("tr", {}, ...["#", "날짜", "시작", "종료", "만남 역", "선택 장소", "참여자", "목적 · 우선순위", "생성", ""].map((h) => el("th", { text: h })))),
    el("tbody", {}, ...plans.map((p) => (p.id === editingId ? editRow(p, plans) : viewRow(p, plans)))));
  $list.replaceChildren(table);
}

function viewRow(p, plans) {
  return el("tr", {},
    el("td", { text: p.id }), el("td", { text: p.date }), el("td", { text: p.start_time }),
    el("td", { text: p.end_time || "미정" }), el("td", { text: p.station }), el("td", { text: p.spot || "-" }),
    el("td", { text: p.participants.join(", ") }),
    el("td", { text: [p.purpose, PRIO_LABEL[p.priority]].filter(Boolean).join(" · ") || "-" }),
    el("td", { text: fmtLocal(p.created_at) }),
    el("td", {}, el("button", { class: "ghost", text: "수정", onclick: () => render(plans, p.id) }), " ",
      el("button", { class: "danger", text: "삭제", onclick: () => remove(p) })));
}

function editRow(p, plans) {
  const f = {
    date: el("input", { type: "date", value: p.date }),
    start: el("input", { type: "time", value: p.start_time }),
    end: el("input", { type: "time", value: p.end_time || "" }),
    station: el("input", { value: p.station, maxlength: 60 }),
    spot: el("input", { value: p.spot || "", maxlength: 100 }),
  };
  const save = async () => {
    try {
      const res = await api(`/api/plans/${p.id}`, { method: "PUT", body: {
        date: f.date.value, start_time: f.start.value, end_time: f.end.value || null,
        station: f.station.value.trim(), spot: f.spot.value.trim() || null } });
      notice(res.conflicts.length ? "warn" : "info",
        res.conflicts.length ? `수정했어요. 같은 날 시간이 겹치는 계획: ` + res.conflicts.map((c) => `#${c.id}(${c.start_time}${c.end_time ? "~" + c.end_time : ""} ${c.station})`).join(", ") : "수정했어요.");
      await load();
    } catch (e) { notice("err", e.status === 422 ? "입력값을 확인해 주세요. (종료 시각은 시작 시각보다 늦어야 해요)" : friendlyError(e)); }
  };
  return el("tr", {},
    el("td", { text: p.id }), el("td", {}, f.date), el("td", {}, f.start), el("td", {}, f.end),
    el("td", {}, f.station), el("td", {}, f.spot), el("td", { text: p.participants.join(", ") }),
    el("td", { text: [p.purpose, PRIO_LABEL[p.priority]].filter(Boolean).join(" · ") || "-" }),
    el("td", { text: "" }),
    el("td", {}, el("button", { text: "저장", onclick: save }), " ", el("button", { class: "ghost", text: "취소", onclick: () => render(plans) })));
}

async function remove(p) {
  if (!confirm(`계획 #${p.id} (${p.date} ${p.start_time} ${p.station}) 을(를) 삭제할까요?`)) return;
  try { await api(`/api/plans/${p.id}`, { method: "DELETE" }); notice("info", "삭제했어요."); await load(); }
  catch (e) { notice("err", friendlyError(e)); }
}

load();
