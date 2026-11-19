"""메인 Agent 실행 흐름 확인 (F3005~F3011) + 이벤트·로깅 (F3012~F3013)."""
from __future__ import annotations

import json

from app.agent import rules
from conftest import future_date, meet


def run(c, msg="요청", sid="sess-0001-aaaa", choice=None):
    payload = c.agent.run(sid, msg, choice)
    events = c.log_store.get_session(sid) or []
    return payload, events


def tool_reqs(events, tool=None):
    return [e for e in events if e["type"] == "tool_req" and (tool is None or e["tool"] == tool)]


# ---------------------------------------------------------------- 그래프 구조
def test_graph_has_required_nodes(make_container):
    c = make_container()
    nodes = set(c.agent.graph.get_graph().nodes)
    assert {"analyze", "plan", "execute", "judge", "respond"} <= nodes and "replan" in nodes


# ---------------------------------------------------------------- 한 번에 충족 (시나리오 1)
def test_one_shot_success(make_container):
    c = make_container(meet())
    p, ev = run(c)
    assert p["type"] == "answer" and p["status"] == "ok"
    r = p["result"]
    assert len(r["recommendations"]) == 3 and r["replans"] == 0
    top = r["recommendations"][0]
    assert len(top["times"]) == 2 and top["max_t"] >= top["min_t"] and top["gap"] == top["max_t"] - top["min_t"]
    assert any("조회 시점" in n for n in r["notes"])  # 이동시간 기준 표기
    # 호출 순서: geo_loc → get_wx → find_stn → get_route → find_spot
    order = []
    for e in tool_reqs(ev):
        if e["tool"] not in order:
            order.append(e["tool"])
    assert order == ["geo_loc", "get_wx", "find_stn", "get_route", "find_spot"]
    assert r["plan_draft"]["participants"] == ["잠실", "건대"] and r["plan_draft"]["end_time"] is None


def test_events_cover_steps_tools_decision_and_run_end(make_container):
    c = make_container(meet())
    _, ev = run(c)
    types = {e["type"] for e in ev}
    assert {"step_start", "step_end", "tool_req", "tool_res", "llm_decision", "run_end"} <= types
    steps = [e["step"] for e in ev if e["type"] == "step_start"]
    assert steps == ["analyze", "plan", "execute", "judge", "respond"]
    dec = next(e for e in ev if e["type"] == "llm_decision")
    assert dec["kind"] == "intent" and dec["value"] == "new_meet" and dec["reason"]
    assert [e["seq"] for e in ev] == sorted(e["seq"] for e in ev)
    res = next(e for e in ev if e["type"] == "tool_res")
    assert {"tool", "ok", "code", "ms", "count"} <= set(res) and "data" not in res  # 응답 원문은 남기지 않는다


def test_prio_travel_calls_find_spot_only_for_top3(make_container):
    c = make_container(meet(prio="travel"))
    _, ev = run(c)
    assert len(tool_reqs(ev, "find_spot")) == 3 * 4  # 상위 3곳 × 카테고리 4종


def test_prio_food_single_category(make_container):
    c = make_container(meet(prio="food"))
    p, ev = run(c)
    cats = {e["args"]["cat"] for e in tool_reqs(ev, "find_spot")}
    assert cats == {"음식점"} and p["status"] == "ok"


def test_rain_excludes_outdoor_category(make_container, world):
    world.rain = 80
    c = make_container(meet())
    p, ev = run(c)
    cats = {e["args"]["cat"] for e in tool_reqs(ev, "find_spot")}
    assert "관광명소" not in cats and p["result"]["weather"]["rainy"] is True
    assert any("실내" in n for n in p["result"]["notes"])


# ---------------------------------------------------------------- 되묻기 / Tool 불필요
def test_missing_conditions_asks_before_any_tool(make_container):
    c = make_container(meet(date=None, start=None))
    p, ev = run(c)
    assert p["type"] == "ask" and p["ask"]["kind"] == "missing"
    assert "날짜" in p["ask"]["message"] and "시작 시각" in p["ask"]["message"]
    assert tool_reqs(ev) == []  # 값을 지어내지 않고 Tool 호출 전에 되묻는다
    steps = [e["step"] for e in ev if e["type"] == "step_start"]
    assert steps == ["analyze", "plan", "respond"]


def test_single_location_asks(make_container):
    p, _ = run(make_container(meet(locs=["잠실"])))
    assert p["ask"]["kind"] == "missing" and "위치" in p["ask"]["message"]


def test_priority_asked_with_buttons_then_choice_runs(make_container):
    c = make_container(meet(prio=None))
    p, _ = run(c)
    assert p["ask"]["kind"] == "priority" and {x["value"] for x in p["ask"]["choices"]} == {"travel", "things", "food"}
    p2, ev = run(c, "", choice={"kind": "priority", "value": "food"})
    assert p2["status"] == "ok" and p2["result"]["criteria"]["prio"] == "food"
    assert c.agent.llm.analyze_calls == 1  # 선택 버튼 처리에는 LLM 을 쓰지 않는다


def test_chat_intent_skips_plan(make_container):
    c = make_container({"intent": "chat", "reason": "인사"})
    p, ev = run(c, "안녕")
    assert p["answer"] and p["status"] == "chat"
    assert [e["step"] for e in ev if e["type"] == "step_start"] == ["analyze", "respond"]
    assert tool_reqs(ev) == []


def test_ambiguous_location_asks_then_choice_resolves(make_container, world):
    from conftest import doc
    world.keyword["중앙역"] = [doc("c1", "중앙역 4호선", 127.05, 37.52), doc("c2", "중앙역 1호선", 129.07, 35.10)]
    c = make_container(meet(locs=["중앙", "건대"]))
    p, ev = run(c)
    assert p["ask"]["kind"] == "disambiguate" and len(p["ask"]["choices"]) == 2
    assert p["ask"]["name"] == "중앙" and p["status"] == "ask"
    first = p["ask"]["choices"][0]["value"]
    p2, _ = run(c, "", choice={"kind": "disambiguate", "name": "중앙", "value": first})
    assert p2["status"] in ("ok", "limit")  # 재계획이 아니라 되묻기였고, 선택 후 진행된다


# ---------------------------------------------------------------- 재계획 (시나리오 2, 3)
def test_replan_widens_radius_and_reuses_results(make_container, world, monkeypatch):
    monkeypatch.setenv("T_MAX", "30")
    near = world.stations[2]  # 가까운 역을 반경 4500 에서만 보이게 한다
    near["distance"] = "4500"
    world.stations = world.far(6, 2000) + [near]
    c = make_container(meet(prio="travel"))
    p, ev = run(c)
    assert p["status"] == "ok" and p["result"]["replans"] == 1
    rp = [e for e in ev if e["type"] == "replan"]
    assert len(rp) == 1 and rp[0]["n"] == 1 and "3000" in rp[0]["change"] and "5000" in rp[0]["change"]
    # 재계획 후 이미 조회한 경로는 다시 부르지 않는다
    reqs = [json.dumps(e["args"], sort_keys=True) for e in tool_reqs(ev, "get_route")]
    assert len(reqs) == len(set(reqs))
    assert any(e["type"] == "cache_hit" for e in ev)
    assert p["result"]["recommendations"][0]["station"] == "어린이대공원역"


def test_replan_limit_stops_with_reason_and_asks_to_relax(make_container, world, monkeypatch):
    monkeypatch.setenv("T_MAX", "20")
    world.stations = [{**d, "distance": "2000"} for d in world.far(14)]
    c = make_container(meet(prio="travel"))
    p, ev = run(c)
    assert p["status"] == "limit"
    assert len([e for e in ev if e["type"] == "replan"]) == 3  # 상한 3회
    assert p["result"]["near_miss"]["excess"] > 0
    assert p["ask"]["kind"] == "relax" and p["ask"]["choices"][0]["value"] == {"t_max": 50}
    # 완화 선택 후 후속 요청
    p2, _ = run(c, "", choice={"kind": "patch", "value": {"t_max": 120}})
    assert p2["status"] == "ok"


def test_max_replan_is_configurable(make_container, world, monkeypatch):
    monkeypatch.setenv("T_MAX", "20")
    monkeypatch.setenv("MAX_REPLAN", "1")
    world.stations = world.far(6)
    p, ev = run(make_container(meet()))
    assert p["status"] == "limit" and len([e for e in ev if e["type"] == "replan"]) == 1


def test_all_route_failures_do_not_replan(make_container, monkeypatch):
    """조회 실패는 '제약 불충족'이 아니므로 재계획하지 않는다."""
    monkeypatch.setenv("FAIL_INJECT", "get_route:API_ERROR")
    p, ev = run(make_container(meet()))
    assert p["status"] == "no_data" and not [e for e in ev if e["type"] == "replan"]
    assert all("판정 불가" in x["reason"] for x in p["result"]["excluded"])


# ---------------------------------------------------------------- Tool 실패 (시나리오 4)
def test_partial_route_failure_excludes_only_that_candidate(make_container, monkeypatch):
    monkeypatch.setenv("FAIL_INJECT", "get_route:NO_ROUTE:3")  # 후보당 2회 호출 → 3번째마다 실패하면 일부 후보만 탈락
    monkeypatch.setenv("ROUTE_WORKERS", "1")
    p, _ = run(make_container(meet(prio="travel")))
    assert p["status"] == "ok" and p["result"]["recommendations"]
    assert any("NO_ROUTE" in x["reason"] for x in p["result"]["excluded"])


def test_missing_kakao_key_is_error_without_replan(make_container, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_KEY", "")
    p, ev = run(make_container(meet()))
    assert p["type"] == "error" and p["error_code"] == "NO_KEY" and "KAKAO_REST_KEY" in p["answer"]
    assert not [e for e in ev if e["type"] == "replan"]


def test_weather_failure_is_skipped_not_fatal(make_container, monkeypatch):
    c = make_container(meet(date=future_date(40)))  # 예보 범위 밖
    p, _ = run(c)
    assert p["status"] == "ok" and "unavailable" in p["result"]["weather"]
    assert any("날씨 판단을 생략" in n for n in p["result"]["notes"])


def test_call_budget_stops_run(make_container, monkeypatch):
    monkeypatch.setenv("CALL_BUDGET", "3")
    p, _ = run(make_container(meet()))
    assert p["type"] == "error" and p["error_code"] == "RATE_LIMIT"


# ---------------------------------------------------------------- 대화 맥락 (시나리오 6)
def test_followup_changes_only_priority_and_reuses_tool_results(make_container):
    c = make_container(meet(prio="things"),
                       {"intent": "edit_cond", "reason": "우선순위만 변경", "prio": "food"})
    p1, ev1 = run(c, "처음")
    n_route_1 = len(tool_reqs(ev1, "get_route"))
    assert n_route_1 > 0
    p2, ev2 = run(c, "같은 조건에서 음식점 많은 쪽으로")
    second = [e for e in ev2 if e["seq"] > ev1[-1]["seq"]]
    assert p2["result"]["criteria"]["prio"] == "food"
    assert p2["result"]["plan_draft"]["participants"] == ["잠실", "건대"]  # 위치·날짜 유지
    assert p2["result"]["date"] == p1["result"]["date"]
    assert tool_reqs(second, "get_route") == [] and tool_reqs(second, "geo_loc") == []  # 이동시간 재호출 없음
    assert any(e["type"] == "cache_hit" for e in second)
    assert c.memories.get("sess-0001-aaaa").cache  # 정규화된 결과만 보관


def test_answer_to_missing_question_merges_into_existing_slots(make_container):
    c = make_container(meet(date=None, start=None),
                       {"intent": "new_meet", "reason": "답변", "date": future_date(), "start": "15:00"})
    p1, _ = run(c, "잠실이랑 건대에서 만날래")
    assert p1["ask"]["kind"] == "missing"
    p2, _ = run(c, "토요일 3시")
    assert p2["status"] == "ok" and p2["result"]["plan_draft"]["start_time"] == "15:00"


def test_llm_failure_is_reported_without_crash(make_container):
    c = make_container()  # 스크립트가 비어 있어 analyze 가 예외
    c.agent.llm.analyze = lambda *a, **k: (_ for _ in ()).throw(__import__("app.agent.llm", fromlist=["x"]).LLMError("NO_KEY", "x"))
    p, _ = run(c)
    assert p["type"] == "error" and p["error_code"] == "LLM_NO_KEY"


def test_compose_failure_falls_back_to_template(make_container):
    from app.agent.llm import LLMError
    c = make_container(meet())
    c.agent.llm.compose = lambda facts: (_ for _ in ()).throw(LLMError("LLM_ERROR", "x"))
    p, _ = run(c)
    assert p["status"] == "ok" and "추천" in p["answer"] and p["result"]["recommendations"]


# ---------------------------------------------------------------- 로깅 Agent 분리 (F3012, F3013)
def test_main_agent_runs_without_logging_agent(make_container):
    c = make_container(meet(), logging_enabled=False)
    p = c.agent.run("sess-nolog-1", "요청")
    assert p["status"] == "ok" and c.log_store.get_session("sess-nolog-1") is None


def test_logging_failure_does_not_break_conversation(make_container, monkeypatch):
    c = make_container(meet())
    monkeypatch.setattr(c.logging_agent, "dir", c.logging_agent.dir / "\0bad")  # 저장 실패 유도
    assert c.agent.run("sess-0001-aaaa", "요청")["status"] == "ok"


def test_logs_are_isolated_per_session_and_in_order(make_container):
    c = make_container(meet(), meet(locs=["잠실", "건대"]))
    c.agent.run("sess-0001-aaaa", "a")
    c.agent.run("sess-0002-bbbb", "b")
    a, b = c.log_store.get_session("sess-0001-aaaa"), c.log_store.get_session("sess-0002-bbbb")
    assert {e["session_id"] for e in a} == {"sess-0001-aaaa"} and {e["session_id"] for e in b} == {"sess-0002-bbbb"}
    sessions = {s["session_id"]: s for s in c.log_store.list_sessions()}
    assert sessions["sess-0001-aaaa"]["runs"] == 1 and sessions["sess-0001-aaaa"]["last_status"] == "ok"
    assert c.log_store.delete_session("sess-0001-aaaa") and c.log_store.get_session("sess-0001-aaaa") is None


def test_logging_agent_makes_no_llm_calls(make_container):
    c = make_container(meet())
    run(c)
    assert c.agent.llm.analyze_calls == 1 and c.agent.llm.compose_calls == 1  # 메인 Agent 의 호출뿐


def test_bus_works_with_no_subscribers_and_swallows_subscriber_errors():
    from app.events.bus import EventBus
    bus = EventBus()
    assert bus.publish("s1", "step_start", step="x")["seq"] == 1
    bus.subscribe(lambda e: 1 / 0)
    got = []
    bus.subscribe(got.append)
    assert bus.publish("s1", "step_end", step="x")["seq"] == 2 and len(got) == 1


# ---------------------------------------------------------------- 규칙 (제약 판단 경계값)
def _eval(name, times):
    return {"id": name, "name": name, "x": 0, "y": 0, "times": dict(enumerate(times)),
            "transfers": {}, "status": "ok"}


def test_constraint_boundaries_inclusive():
    evals = [_eval("경계", [90, 60]), _eval("초과", [91, 80]), _eval("편차", [80, 40]), _eval("편차경계", [70, 40])]
    passed, excl = rules.apply_constraints(evals, 2, 90, 30, ["a", "b"])
    assert [p["name"] for p in passed] == ["경계", "편차경계"]
    reasons = {x["name"]: x["reason"] for x in excl}
    assert "91분" in reasons["초과"] and "편차 40분" in reasons["편차"]


def test_rank_ties_broken_by_travel_sum_and_unknown_is_not_invented():
    a = {**_eval("A", [30, 30]), "sum_t": 60, "max_t": 30, "min_t": 30, "gap": 0}
    b = {**_eval("B", [20, 40]), "sum_t": 60, "max_t": 40, "min_t": 20, "gap": 20}
    c = {**_eval("C", [10, 10]), "sum_t": 20, "max_t": 10, "min_t": 10, "gap": 0}
    spots = {"A": {"음식점": {"total": 7}}, "B": {"음식점": {"total": 7}}, "C": {"음식점": None}}
    ranked, unknown = rules.rank("food", [a, b, c], spots, False)
    assert [r["name"] for r in ranked] == ["A", "B"] and unknown[0]["name"] == "C" and "판정 불가" in unknown[0]["reason"]
    assert [r["name"] for r in rules.rank("travel", [a, b, c], {}, False)[0]] == ["C", "A", "B"]


def test_limit_without_any_station_asks_for_closer_locations_not_looser_constraints(make_container, world):
    """서울-부산처럼 중간 지점에 역이 없으면 제약 완화가 아니라 위치 조정을 권한다 (실제 호출에서 발견)."""
    world.stations = []
    p, ev = run(make_container(meet(prio="travel")))
    assert p["status"] == "limit" and len([e for e in ev if e["type"] == "replan"]) == 3
    assert p["ask"]["kind"] == "relax_region" and p["ask"]["choices"] == []
    assert "지하철역이 없" in p["ask"]["message"] and p["result"]["recommendations"] == []


def test_rank_ties_prefer_smaller_max_travel_time():
    a = {**_eval("A", [9, 0]), "sum_t": 9, "max_t": 9, "min_t": 0, "gap": 9}
    b = {**_eval("B", [6, 3]), "sum_t": 9, "max_t": 6, "min_t": 3, "gap": 3}
    assert [r["name"] for r in rules.rank("travel", [a, b], {}, False)[0]] == ["B", "A"]


def test_today_date_invented_by_llm_without_mention_is_dropped():
    from app.agent.llm import Analysis, drop_unsupported_today
    a = Analysis(intent="edit_cond", date="2026-11-19", end="19:00")
    assert drop_unsupported_today(a, "잠실에서 7시 전에 끝나야 해", "2026-11-19").date is None
    assert drop_unsupported_today(Analysis(date="2026-11-19"), "오늘 저녁에 만나자", "2026-11-19").date == "2026-11-19"
    assert drop_unsupported_today(Analysis(date="2026-11-21"), "토요일에 만나자", "2026-11-19").date == "2026-11-21"


def test_spot_targets_use_the_same_order_as_ranking_so_the_top_pick_always_has_spots():
    """동점(이동시간 합 같음)에서 최대 이동시간이 작은 1위가 주변 장소 조회 대상에서 빠지던 버그 (실제 화면에서 발견)."""
    cands = [{**_eval(n, t), "sum_t": sum(t), "max_t": max(t), "min_t": min(t), "gap": max(t) - min(t)}
             for n, t in [("A", [40, 33]), ("B", [44, 29]), ("C", [42, 31]), ("D", [37, 36]), ("E", [70, 3])]]
    ranked, _ = rules.rank("travel", cands, {}, False)
    targets = rules.spot_targets("travel", cands)
    assert ranked[0]["name"] == "D"                       # 합이 같으면 최대 이동시간이 작은 쪽이 1위
    assert {r["name"] for r in ranked} <= {t["name"] for t in targets}   # 추천 3곳은 모두 조회 대상에 포함
    assert len(targets) == 3 and len(rules.spot_targets("food", cands)) == 5   # 그 밖의 우선순위는 전부


def test_every_recommendation_has_spots_in_travel_mode_with_ties(make_container, world, monkeypatch):
    c = make_container(meet(prio="travel"))
    p = c.agent.run("sess-tie-0001", "요청")
    assert p["status"] == "ok" and all(r["spots"] for r in p["result"]["recommendations"])
