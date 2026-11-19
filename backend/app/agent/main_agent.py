"""메인 Agent: 분석 → 계획 → 실행 → 판단 → 응답 (+ 재계획 루프). F3005~F3011.

단계별 LLM 사용: 분석(사용) / 계획(미사용, 규칙) / 실행(미사용) / 판단(미사용, 계산식) / 응답(문장 생성에만 사용)
이벤트는 EventBus 로 발행만 한다. 로깅 Agent 가 연결돼 있는지 이 코드는 알지 못한다 (F3012).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent import rules
from app.agent.llm import USAGE_TEXT, LLMError
from app.agent.memory import SLOT_KEYS, MemoryStore, SessionMemory, cache_key, fresh_slots
from app.config import settings
from app.events.bus import EventBus
from app.tools import call_tool
from app.tools.base import fail
from app.utils.geo import WEEKDAYS_KO, centroid, now_kst

log = logging.getLogger("agent")

FATAL_CODES = {"NO_KEY", "RATE_LIMIT"}
PLAN_STEPS = ["geo_loc", "get_wx", "find_stn", "get_route", "find_spot"]
PRIO_CHOICES = [{"label": label, "value": key} for key, label in rules.PRIOS.items()]

ERROR_MESSAGES = {
    "NO_KEY": "카카오 API 키가 설정되지 않았거나 유효하지 않아요. backend 의 .env 에서 KAKAO_REST_KEY 와 서비스 활성화를 확인해 주세요.",
    "RATE_LIMIT": "외부 API 호출 한도에 도달했어요. 잠시 후 다시 시도해 주세요.",
    "UPSTREAM": "외부 API 오류로 위치·후보 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
    "LLM_NO_KEY": "LLM API 키가 설정되지 않았어요. .env 의 LLM_API_KEY 를 확인해 주세요.",
    "LLM_ERROR": "요청을 분석하지 못했어요. 잠시 후 다시 시도해 주세요.",
    "INTERNAL": "처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
}


@dataclass
class RunCtx:
    calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    fatal: dict | None = None


class AgentState(TypedDict, total=False):
    session_id: str
    message: str
    choice: dict | None
    memory: SessionMemory
    ctx: RunCtx
    intent: str
    reason: str
    slots: dict
    ask: dict | None
    fatal: dict | None
    plan: list
    res: dict
    evals: list
    passed: list
    excl: list
    ranked: list
    near: dict | None
    prev_eval: dict | None
    replan_n: int
    radius_m: int
    k: int
    decision: str
    status: str
    final: dict


def _count(res: dict) -> int | None:
    if not res.get("ok"):
        return None
    d = res["data"]
    for key in ("items", "stations"):
        if key in d:
            return len(d[key])
    return None


class MainAgent:
    def __init__(self, bus: EventBus, llm: Any, memories: MemoryStore,
                 eval_lookup: Callable[[str], dict | None] | None = None):
        self.bus, self.llm, self.memories = bus, llm, memories
        self.eval_lookup = eval_lookup  # F3020: 로깅 Agent 가 남긴 직전 실행 판정을 참조하는 함수
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ graph
    def _wrap(self, name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
        def node(state: AgentState) -> dict:
            sid = state["session_id"]
            # 검증용 주입(ANOMALY_INJECT=missing_events): 실행 시작 이벤트가 유실된 상황을 만든다 → 로깅 Agent 는 '판정 불가'
            if not (name == "analyze" and settings().anomaly_inject == "missing_events"):
                self.bus.publish(sid, "step_start", step=name)
            t0 = time.perf_counter()
            try:
                out = fn(state)
            except Exception:  # noqa: BLE001
                log.exception("node %s failed", name)
                out = {"fatal": {"code": "INTERNAL"}, "_end": {"error": "INTERNAL"}}
            extra = out.pop("_end", {})
            self.bus.publish(sid, "step_end", step=name, ms=int((time.perf_counter() - t0) * 1000), **extra)
            return out
        return node

    def _build_graph(self):
        g = StateGraph(AgentState)
        for name, fn in [("analyze", self.n_analyze), ("plan", self.n_plan), ("execute", self.n_execute),
                         ("judge", self.n_judge), ("replan", self.n_replan), ("respond", self.n_respond)]:
            g.add_node(name, self._wrap(name, fn))
        g.add_edge(START, "analyze")
        g.add_conditional_edges("analyze", lambda s: (
            "respond" if s.get("fatal") or s.get("intent") in ("chat", "save_plan") else "plan"),
            {"plan": "plan", "respond": "respond"})
        g.add_conditional_edges("plan", lambda s: "respond" if s.get("ask") or s.get("fatal") else "execute",
                                {"execute": "execute", "respond": "respond"})
        g.add_edge("execute", "judge")
        g.add_conditional_edges("judge", lambda s: s.get("decision", "respond"),
                                {"replan": "replan", "respond": "respond"})
        g.add_edge("replan", "plan")
        g.add_edge("respond", END)
        return g.compile()

    def graph_mermaid(self) -> str:
        """그래프 구조 출력 (검증 4.2: 분석 → 계획 → 실행 → 판단 → 응답)."""
        return self.graph.get_graph().draw_mermaid()

    # --------------------------------------------------------------- run
    def run(self, session_id: str, message: str, choice: dict | None = None) -> dict:
        t0 = time.perf_counter()
        mem = self.memories.get(session_id)
        s = settings()
        prev = None
        if self.eval_lookup:
            try:
                prev = self.eval_lookup(session_id)
            except Exception:  # noqa: BLE001 - 판정 조회 실패는 무시한다
                prev = None
        state: AgentState = {
            "session_id": session_id, "message": message, "choice": choice, "memory": mem,
            "ctx": RunCtx(), "slots": mem.slots, "prev_eval": prev, "ask": None, "fatal": None, "replan_n": 0,
            "radius_m": s.radius_steps[0], "k": s.k_steps[0], "excl": [], "evals": [], "passed": [],
        }
        try:
            out = self.graph.invoke(state, {"recursion_limit": 60})
            payload = out["final"]
            intent = out.get("intent")
        except Exception:  # noqa: BLE001
            log.exception("graph failed")
            payload = {"type": "error", "status": "error", "error_code": "INTERNAL",
                       "answer": ERROR_MESSAGES["INTERNAL"], "ask": None, "result": None}
            intent = None
        mem.turns += 1
        mem.pending = ({"kind": payload["ask"]["kind"], "intent": intent if intent in ("new_meet", "edit_cond") else "new_meet"}
                       if payload.get("type") == "ask" else None)
        if payload.get("status") == "ok" and payload.get("result"):
            mem.summary = {
                "top": [{"station": r["station"], "max_t": r["max_t"], "gap": r["gap"], "score": r["score"]}
                        for r in payload["result"]["recommendations"]],
                "excluded_count": len(payload["result"]["excluded"]),
            }
        self.bus.publish(session_id, "run_end", status=payload.get("status"),
                         total_ms=int((time.perf_counter() - t0) * 1000))
        return payload

    # --------------------------------------------------------- tool helper
    def _call(self, state: AgentState, tool: str, args: dict) -> dict:
        ctx, sid, mem = state["ctx"], state["session_id"], state["memory"]
        key = cache_key(tool, args)
        cached = mem.cache.get(key)
        if cached is not None:  # 재계획·후속 요청에서 이미 확보한 결과는 다시 부르지 않는다 (F3009)
            self.bus.publish(sid, "cache_hit", tool=tool)
            return cached
        with ctx.lock:
            if ctx.calls >= settings().call_budget:
                ctx.fatal = ctx.fatal or {"code": "RATE_LIMIT"}
                return fail("RATE_LIMIT", "요청당 호출 예산을 초과했습니다.")
            ctx.calls += 1
        self.bus.publish(sid, "tool_req", tool=tool, args=args)
        t0 = time.perf_counter()
        res = call_tool(tool, args)
        code = None if res["ok"] else res["error"]["code"]
        self.bus.publish(sid, "tool_res", tool=tool, ok=res["ok"], code=code,
                         ms=int((time.perf_counter() - t0) * 1000), count=_count(res))
        if res["ok"] or code in ("NO_RESULT", "NO_ROUTE"):  # 재시도해도 같은 결과인 것만 캐시
            mem.cache[key] = res
        if code in FATAL_CODES:
            with ctx.lock:
                ctx.fatal = ctx.fatal or {"code": code}
        return res

    # --------------------------------------------------------------- nodes
    def _apply_choice(self, mem: SessionMemory, choice: dict) -> str:
        kind, value = choice.get("kind"), choice.get("value")
        if kind == "priority" and value in rules.PRIOS:
            mem.slots["prio"] = value
        elif kind == "disambiguate" and isinstance(value, dict) and choice.get("name"):
            try:
                mem.geo[str(choice["name"])] = {"id": str(value.get("id", "")), "name": str(value["name"]),
                                                "addr": str(value.get("addr", "")),
                                                "x": float(value["x"]), "y": float(value["y"])}
            except (KeyError, TypeError, ValueError):
                pass
        elif kind == "patch" and isinstance(value, dict):
            for k in ("t_max", "g_max"):
                if isinstance(value.get(k), int) and 5 <= value[k] <= 360:
                    mem.slots[k] = value[k]
        return (mem.pending or {}).get("intent") or "new_meet"

    @staticmethod
    def _prev_eval_note(state: AgentState) -> dict:
        """직전 실행의 판정(verdict·경고 코드)을 참조했다는 사실을 단계 종료 이벤트에 남긴다."""
        pe = state.get("prev_eval")
        if not pe:
            return {}
        return {"prev_eval": pe["verdict"], "prev_warnings": [w["code"] for w in pe.get("warnings", [])]}

    def n_analyze(self, state: AgentState) -> dict:
        mem, msg = state["memory"], state["message"]
        if state.get("choice"):
            intent = self._apply_choice(mem, state["choice"])
            mem.pending = None
            return {"intent": intent, "reason": "사용자 선택 반영", "slots": mem.slots,
                    "_end": {"intent": intent, "reason": "사용자 선택 반영 (LLM 미사용)", **self._prev_eval_note(state)}}
        now = now_kst()
        try:
            a = self.llm.analyze(msg, mem.context(), now.strftime("%Y-%m-%d"), WEEKDAYS_KO[now.weekday()])
        except LLMError as e:
            code = "LLM_NO_KEY" if e.code == "NO_KEY" else "LLM_ERROR"
            return {"fatal": {"code": code}, "_end": {"error": code}}
        intent = a.intent
        if mem.pending and intent == "new_meet":  # 되묻기에 대한 답변은 조건 갱신으로 본다
            intent = "edit_cond"
        self.bus.publish(state["session_id"], "llm_decision", kind="intent", value=intent, reason=a.reason)
        if intent in ("new_meet", "edit_cond"):
            if intent == "new_meet":
                mem.slots = fresh_slots()
            for k in SLOT_KEYS:  # 사용자가 바꾼(말한) 값만 갱신한다
                v = getattr(a, k)
                if v is not None and v != []:
                    mem.slots[k] = v
        mem.pending = None
        return {"intent": intent, "reason": a.reason, "slots": mem.slots,
                "_end": {"intent": intent, "reason": a.reason, **self._prev_eval_note(state)}}

    def n_plan(self, state: AgentState) -> dict:
        slots, s = state["slots"], settings()
        missing: list[str] = []
        if not slots["date"]:
            missing.append("날짜")
        else:
            try:
                if Date.fromisoformat(slots["date"]) < now_kst().date():
                    slots["date"] = None
                    missing.append("날짜(지난 날짜는 사용할 수 없어요)")
            except ValueError:
                slots["date"] = None
                missing.append("날짜")
        if not slots["start"]:
            missing.append("시작 시각")
        if len(slots["locs"]) < 2:
            missing.append("참여자 위치(2곳 이상)")
        if len(slots["locs"]) > s.max_participants:
            ask = {"kind": "missing", "choices": [],
                   "message": f"참여자 위치는 최대 {s.max_participants}곳까지 지원해요. 줄여서 다시 알려 주세요."}
            return {"ask": ask, "_end": {"ask": "missing"}}
        if missing:
            ask = {"kind": "missing", "choices": [],
                   "message": f"{', '.join(missing)}을(를) 알려 주세요. 예) \"잠실이랑 건대, 토요일 오후 2시\""}
            return {"ask": ask, "_end": {"ask": "missing", "missing": missing}}
        if not slots["prio"]:
            ask = {"kind": "priority", "choices": PRIO_CHOICES,
                   "message": "어떤 점을 가장 중요하게 볼까요?"}
            return {"ask": ask, "_end": {"ask": "priority"}}
        plan = PLAN_STEPS
        if settings().anomaly_inject == "unplanned_call":  # 검증용 주입: 계획에서 get_wx 를 빼 두고 실행은 그대로 호출 → 계획 외 호출
            plan = [t for t in PLAN_STEPS if t != "get_wx"]
        return {"ask": None, "plan": plan, "_end": {"plan": plan}}

    # ---- 실행: Tool 호출 (경로 → 제약 필터 → 장소 건수)
    def n_execute(self, state: AgentState) -> dict:
        slots, mem, ctx, s = state["slots"], state["memory"], state["ctx"], settings()
        names = slots["locs"]
        t_max, g_max = slots["t_max"] or s.t_max, slots["g_max"] or s.g_max
        points: list[dict] = []
        for name in names:
            loc = mem.geo.get(name)
            if loc is None:
                r = self._call(state, "geo_loc", {"q": name})
                if r["ok"]:
                    loc = r["data"]["items"][0]
                    mem.geo[name] = loc
                else:
                    code = r["error"]["code"]
                    if code == "AMBIGUOUS":
                        cands = r["error"]["detail"]["candidates"]
                        return {"ask": {"kind": "disambiguate", "name": name,
                                        "message": f"'{name}' 위치가 여러 곳이에요. 어느 곳인가요?",
                                        "choices": [{"label": f"{c['name']} ({c['addr']})" if c["addr"] else c["name"],
                                                     "value": c} for c in cands]},
                                "_end": {"ask": "disambiguate"}}
                    if code in ("NO_RESULT", "BAD_INPUT"):
                        return {"ask": {"kind": "missing", "choices": [],
                                        "message": f"'{name}' 위치를 찾지 못했어요. 역 이름처럼 더 구체적으로 알려 주세요."},
                                "_end": {"ask": "missing"}}
                    ctx.fatal = ctx.fatal or {"code": "UPSTREAM"}
                    return {"_end": {"error": code}}
            points.append({**loc, "label": name})

        cx, cy = centroid(points)
        cx, cy = round(cx, 6), round(cy, 6)
        wxr = self._call(state, "get_wx", {"x": cx, "y": cy, "date": slots["date"]})
        if settings().anomaly_inject == "repeat_call":  # 검증용 주입: 같은 Tool 을 같은 입력으로 한 번 더 호출 (캐시를 우회)
            mem.cache.pop(cache_key("get_wx", {"x": cx, "y": cy, "date": slots["date"]}), None)
            wxr = self._call(state, "get_wx", {"x": cx, "y": cy, "date": slots["date"]})
        wx = wxr["data"] if wxr["ok"] else {"unavailable": wxr["error"]["code"], "message": wxr["error"]["message"]}

        sr = self._call(state, "find_stn", {"x": cx, "y": cy, "radius_m": state["radius_m"], "size": 15})
        if sr["ok"]:
            cands = rules.pick_candidates(sr["data"]["stations"], points, state["k"])
        elif sr["error"]["code"] == "NO_RESULT":
            cands = []
        else:
            if not ctx.fatal:
                ctx.fatal = {"code": "UPSTREAM"}
            return {"_end": {"error": sr["error"]["code"]}}

        def eval_one(st: dict) -> dict:
            e: dict = {"id": st["id"], "name": st["name"], "x": st["x"], "y": st["y"],
                       "times": {}, "transfers": {}, "status": "ok"}
            for i in rules.participant_order(st, points):  # 가장 먼 참여자부터
                r = self._call(state, "get_route", {"from": {"x": points[i]["x"], "y": points[i]["y"]},
                                                    "to": {"x": st["x"], "y": st["y"]}})
                if not r["ok"]:
                    e["status"] = "no_route" if r["error"]["code"] == "NO_ROUTE" else "error"
                    break
                m = r["data"]["min"]
                e["times"][i], e["transfers"][i] = m, r["data"].get("transfers", 0)
                if r["data"].get("mode") == "walk_estimate":  # 가까워 대중교통 경로가 없는 구간 → 도보 추정
                    e.setdefault("walk", []).append(i)
                if m > t_max:  # 상한 초과 → 나머지 참여자 호출 생략
                    e["status"], e["over"] = "pruned", {"idx": i, "min": m}
                    break
            return e

        with ThreadPoolExecutor(max_workers=s.route_workers) as ex:
            evals = list(ex.map(eval_one, cands))
        passed, excl = rules.apply_constraints(evals, len(points), t_max, g_max, names)

        rainy = bool(wxr["ok"] and wx["rain_pct"] >= s.rain_threshold)
        spots: dict[str, dict] = {}
        if passed and not ctx.fatal:
            prio = slots["prio"]
            targets = rules.spot_targets(prio, passed)
            jobs = [(c, cat) for c in targets for cat in rules.spot_categories(prio, rainy)]

            def spot_one(job: tuple[dict, str]):
                c, cat = job
                r = self._call(state, "find_spot", {"x": c["x"], "y": c["y"], "radius_m": 500, "cat": cat, "size": 3})
                if r["ok"]:
                    info: dict | None = {"total": r["data"]["total"], "items": r["data"]["items"]}
                elif r["error"]["code"] == "NO_RESULT":
                    info = {"total": 0, "items": []}
                else:
                    info = None
                return c["id"], cat, info

            with ThreadPoolExecutor(max_workers=s.route_workers) as ex:
                for cid, cat, info in ex.map(spot_one, jobs):
                    spots.setdefault(cid, {})[cat] = info

        res = {"points": points, "wx": wx, "wx_ok": wxr["ok"], "rainy": rainy, "spots": spots,
               "candidate_count": len(cands), "t_max": t_max, "g_max": g_max}
        return {"res": res, "evals": evals, "passed": passed, "excl": excl,
                "_end": {"candidates": len(cands), "passed": len(passed), "tool_calls": ctx.calls}}

    # ---- 판단: 계산식으로 점수·재계획 여부 결정 (LLM 미사용)
    def n_judge(self, state: AgentState) -> dict:
        ctx, s = state["ctx"], settings()
        if ctx.fatal:
            return {"fatal": ctx.fatal, "decision": "respond", "status": "error",
                    "_end": {"decision": "respond", "status": "error"}}
        if state.get("ask") or state.get("fatal"):
            return {"decision": "respond", "status": "ask" if state.get("ask") else "error",
                    "_end": {"decision": "respond"}}
        res, slots = state["res"], state["slots"]
        n = len(res["points"])
        passed, excl = state["passed"], list(state["excl"])
        if passed:
            ranked, unknown = rules.rank(slots["prio"], passed, res["spots"], res["rainy"])
            for r in ranked:
                r["spots"] = res["spots"].get(r["id"], {})
            if ranked:
                return {"ranked": ranked, "excl": excl + unknown, "decision": "respond", "status": "ok",
                        "_end": {"decision": "respond", "ranked": len(ranked)}}
            return {"excl": excl + unknown, "decision": "respond", "status": "no_data",
                    "_end": {"decision": "respond", "status": "no_data"}}
        if rules.is_replannable(state["evals"], res["candidate_count"]):
            if state["replan_n"] < s.max_replan:
                return {"decision": "replan", "status": "replan",
                        "_end": {"decision": "replan", "reason": "충족 후보 0개"}}
            near = rules.near_miss(state["evals"], n, res["t_max"], res["g_max"])
            return {"decision": "respond", "status": "limit", "near": near,
                    "_end": {"decision": "respond", "status": "limit"}}
        return {"decision": "respond", "status": "no_data", "_end": {"decision": "respond", "status": "no_data"}}

    def n_replan(self, state: AgentState) -> dict:
        s = settings()
        n = state["replan_n"] + 1
        i = min(n, len(s.radius_steps) - 1)
        old_r, old_k = state["radius_m"], state["k"]
        radius, k = s.radius_steps[i], s.k_steps[i]
        trigger = "no_station" if state["res"]["candidate_count"] == 0 else "constraint_unmet"
        self.bus.publish(state["session_id"], "replan", n=n, trigger=trigger,
                         change=f"반경 {old_r}m→{radius}m, 후보 수 {old_k}→{k}")
        return {"replan_n": n, "radius_m": radius, "k": k, "decision": "",
                "_end": {"n": n, "radius_m": radius, "k": k}}

    # ---- 응답
    def n_respond(self, state: AgentState) -> dict:
        mem, slots = state["memory"], state["slots"]
        fatal, ask, intent = state.get("fatal"), state.get("ask"), state.get("intent")
        if fatal:
            code = fatal["code"]
            final = {"type": "error", "status": "error", "error_code": code, "ask": None, "result": None,
                     "answer": ERROR_MESSAGES.get(code, ERROR_MESSAGES["INTERNAL"])}
        elif ask:
            final = {"type": "ask", "status": "ask", "error_code": None, "ask": ask, "result": None,
                     "answer": ask["message"]}
        elif intent == "chat":
            try:
                answer = self.llm.chat_reply(state["message"])
            except LLMError:
                answer = USAGE_TEXT
            final = {"type": "answer", "status": "chat", "error_code": None, "ask": None, "result": None,
                     "answer": answer}
        elif intent == "save_plan":
            answer = ("추천 카드의 '이 장소로 저장' 버튼을 눌러 계획을 저장해 주세요." if mem.summary
                      else "저장할 추천 결과가 아직 없어요. 먼저 약속 장소를 추천받아 보세요.")
            final = {"type": "answer", "status": "chat", "error_code": None, "ask": None, "result": None,
                     "answer": answer}
        else:
            final = self._respond_result(state)
        return {"final": final, "_end": {"response_type": final["type"], "status": final["status"]}}

    def _respond_result(self, state: AgentState) -> dict:
        slots, res, status = state["slots"], state["res"], state.get("status", "no_data")
        s = settings()
        names = [p["label"] for p in res["points"]]
        d = Date.fromisoformat(slots["date"])
        date_note = f"{d.isoformat()}({WEEKDAYS_KO[d.weekday()]})"
        defaults = [k for k in ("t_max", "g_max") if not slots[k]]
        criteria = {"t_max": res["t_max"], "g_max": res["g_max"], "prio": slots["prio"],
                    "prio_label": rules.PRIOS[slots["prio"]], "rain_threshold": s.rain_threshold,
                    "defaults_used": defaults}
        weather = ({"rain_pct": res["wx"]["rain_pct"], "tmax": res["wx"]["tmax"], "tmin": res["wx"]["tmin"],
                    "rainy": res["rainy"]} if res["wx_ok"] else {"unavailable": res["wx"].get("message", "")})
        notes = ["이동시간은 조회 시점 기준이며 약속 시각의 실제 운행과 다를 수 있어요."]
        if not res["wx_ok"]:
            notes.append(f"날씨 판단을 생략했어요: {res['wx'].get('message', '')}")
        elif res["rainy"]:
            notes.append(f"강수확률 {res['wx']['rain_pct']}%로 실내 위주로 추천해요. 야외 여부를 구분할 수 없어 '관광명소'를 제외했어요.")
        if slots["prio"] in ("things", "food"):
            notes.append("장소 수는 평점이 아니라 반경 500m 안의 검색 건수 기준이에요.")
        if defaults:
            notes.append(f"설정하지 않은 제약은 기본값을 썼어요 (최대 이동시간 {res['t_max']}분, 편차 {res['g_max']}분).")
        if state["replan_n"]:
            notes.append(f"충족 후보가 없어 검색 범위를 넓혀 {state['replan_n']}회 재계획했어요.")

        recs = []
        for i, r in enumerate(state.get("ranked", []), 1):
            recs.append({
                "rank": i, "station": r["name"], "x": r["x"], "y": r["y"],
                "times": [{"name": names[j], "min": r["times"][j], "transfers": r["transfers"].get(j, 0),
                           "walk": j in r.get("walk", [])} for j in range(len(names))],
                "max_t": r["max_t"], "min_t": r["min_t"], "gap": r["gap"], "sum_t": r["sum_t"],
                "score": r["score"],
                "spots": {cat: {"total": info["total"],
                                "items": [{k: it[k] for k in ("name", "addr", "url", "dist_m")} for it in info["items"]]}
                          for cat, info in r.get("spots", {}).items() if info is not None},
            })
        if any(t["walk"] for r in recs for t in r["times"]):
            notes.append("역과 아주 가까운 구간은 대중교통 경로가 없어 도보로 추정했어요 (직선거리×1.3, 분속 75m).")
        near = state.get("near")
        result = {
            "recommendations": recs, "excluded": state.get("excl", []), "criteria": criteria, "weather": weather,
            "date": slots["date"], "date_note": date_note, "notes": notes, "replans": state["replan_n"],
            "near_miss": near,
            "plan_draft": {"date": slots["date"], "start_time": slots["start"], "end_time": slots["end"],
                           "participants": names, "purpose": slots["purpose"], "priority": slots["prio"]},
        }
        ask = None
        no_station = status == "limit" and res["candidate_count"] == 0
        if no_station:  # 이동시간 상한이 원인이 아니라 중간 지점 주변에 역이 없는 경우 → 위치 조정을 권한다
            ask = {"kind": "relax_region", "choices": [],
                   "message": f"두 지역의 중간 지점 반경 {state['radius_m'] // 1000}km 안에 지하철역이 없어요. 서로 더 가까운 위치로 다시 알려 주시겠어요?"}
        elif status == "limit":
            ask = {"kind": "relax", "message": "조건을 조금 조정해 볼까요?", "choices": [
                {"label": f"최대 이동시간 {res['t_max'] + 30}분으로 완화", "value": {"t_max": res["t_max"] + 30}},
                {"label": f"편차 상한 {res['g_max'] + 15}분으로 완화", "value": {"g_max": res["g_max"] + 15}}]}
        facts = {"status": status, "no_station": no_station, "date_note": date_note, "criteria": criteria, "weather": weather,
                 "recommendations": [{"rank": r["rank"], "station": r["station"], "times": r["times"], "max_t": r["max_t"],
                                      "gap": r["gap"], "score": r["score"],
                                      "spot_counts": {c: v["total"] for c, v in r["spots"].items()}} for r in recs],
                 "excluded": result["excluded"][:6], "near_miss": near, "replans": state["replan_n"], "notes": notes}
        try:
            answer = self.llm.compose(facts)
        except LLMError:
            answer = self._template(facts)
        return {"type": "ask" if ask else "answer", "status": status, "error_code": None,
                "ask": ask, "result": result, "answer": answer}

    @staticmethod
    def _template(f: dict) -> str:
        """LLM 없이도 같은 사실을 전달하는 대체 문장."""
        c = f["criteria"]
        if f["status"] == "ok":
            lines = [f"{f['date_note']} 기준 추천이에요. (우선순위: {c['prio_label']})"]
            for r in f["recommendations"]:
                t = ", ".join(f"{x['name']} {x['min']}분" for x in r["times"])
                lines.append(f"{r['rank']}. {r['station']}: {t} (최대 {r['max_t']}분, 편차 {r['gap']}분)")
            return "\n".join(lines)
        if f["status"] == "limit" and f.get("no_station"):
            return f"재계획을 {f['replans']}회 했지만 두 지역 중간 지점 주변에 지하철역이 없어 후보를 찾지 못했어요. 서로 더 가까운 위치를 알려 주세요."
        if f["status"] == "limit":
            nm = f.get("near_miss")
            tail = f" 가장 근접한 후보는 {nm['name']}({nm['detail']}, 초과 {nm['excess']}분)였어요." if nm else ""
            return f"재계획을 {f['replans']}회까지 했지만 조건을 만족하는 후보가 없었어요.{tail}"
        return "이동시간 등 필요한 정보를 확인하지 못해 추천하지 못했어요. 잠시 후 다시 시도해 주세요."
