"""로깅 Agent 평가 기능 확인 (F3020, 명세 4.6)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.evaluation import EvalStore, RunEvaluator, evaluate_run, trim_sentences
from app.main import app
from conftest import FakeLLM, meet

PLAN = ["geo_loc", "get_wx", "find_stn", "get_route", "find_spot"]
SID = "sess-eval-0001"


def ev(type_, **kw):
    return {"session_id": SID, "seq": 0, "ts": "2026-11-20T00:00:00.000+00:00", "type": type_, **kw}


def run_events(calls=(), plan=PLAN, replans=0, start=True, plan_event=True):
    """합성 실행 이벤트. calls = [(tool, args), ...]"""
    out = []
    if start:
        out += [ev("step_start", step="analyze"), ev("step_end", step="analyze", intent="new_meet")]
    out += [ev("step_start", step="plan")]
    if plan_event:
        out += [ev("step_end", step="plan", plan=list(plan))]
    for tool, args in calls:
        out += [ev("tool_req", tool=tool, args=args), ev("tool_res", tool=tool, ok=True, code=None, ms=5, count=1)]
    for i in range(replans):
        out += [ev("replan", n=i + 1, trigger="constraint_unmet", change=f"반경 {3000 + i}m")]
    out += [ev("run_end", status="ok", total_ms=100)]
    return out


# ------------------------------------------------------------------ 규칙 판정 (LLM 미사용)
def test_normal_run_is_ok_with_all_checks_ok():
    r = evaluate_run(run_events([("geo_loc", {"q": "잠실"}), ("geo_loc", {"q": "건대"}), ("get_wx", {"x": 1, "y": 2})]))
    assert r["verdict"] == "ok" and r["warning_count"] == 0 and r["warnings"] == []
    assert r["checks"] == {"repeated_call": "ok", "excess_replan": "ok", "unplanned_call": "ok"}
    assert r["diagnosis"] == {"status": "skipped", "text": None}


def test_same_tool_same_input_repeated_is_flagged_even_if_key_order_differs():
    r = evaluate_run(run_events([("get_route", {"from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}}),
                                 ("get_route", {"to": {"y": 4, "x": 3}, "from": {"y": 2, "x": 1}}),
                                 ("get_route", {"from": {"x": 9, "y": 9}, "to": {"x": 3, "y": 4}})]))
    assert r["verdict"] == "anomaly" and r["checks"]["repeated_call"] == "warn"
    w = r["warnings"][0]
    assert w["code"] == "repeated_call" and w["evidence"]["tool"] == "get_route" and w["evidence"]["count"] == 2


def test_same_tool_with_different_inputs_is_not_repeated():
    r = evaluate_run(run_events([("geo_loc", {"q": "a"}), ("geo_loc", {"q": "b"}), ("geo_loc", {"q": "c"})]))
    assert r["checks"]["repeated_call"] == "ok"


@pytest.mark.parametrize("n,expected", [(0, "ok"), (2, "ok"), (3, "warn"), (4, "warn")])
def test_excess_replan_triggers_at_the_cap(n, expected):
    r = evaluate_run(run_events(replans=n))
    assert r["checks"]["excess_replan"] == expected
    if expected == "warn":
        assert r["warnings"][0]["evidence"]["replans"] == n and r["verdict"] == "anomaly"


def test_replan_threshold_follows_max_replan_setting(monkeypatch):
    monkeypatch.setenv("MAX_REPLAN", "1")
    assert evaluate_run(run_events(replans=1))["checks"]["excess_replan"] == "warn"


def test_tool_not_in_plan_is_flagged():
    r = evaluate_run(run_events([("geo_loc", {"q": "a"}), ("find_spot", {"x": 1}), ("secret_tool", {"a": 1})],
                                plan=["geo_loc", "find_spot"]))
    assert r["verdict"] == "anomaly" and r["checks"]["unplanned_call"] == "warn"
    w = next(w for w in r["warnings"] if w["code"] == "unplanned_call")
    assert w["evidence"]["tools"] == {"secret_tool": 1}


def test_tool_called_without_any_plan_is_flagged():
    r = evaluate_run(run_events([("geo_loc", {"q": "a"})], plan_event=False))
    assert r["checks"]["unplanned_call"] == "warn" and "계획 단계 없이" in r["warnings"][0]["message"]


def test_no_tool_calls_and_no_plan_is_fine_e_g_chat():
    events = [ev("step_start", step="analyze"), ev("step_end", step="analyze"), ev("step_start", step="respond"),
              ev("step_end", step="respond"), ev("run_end", status="chat", total_ms=3)]
    assert evaluate_run(events)["verdict"] == "ok"


def test_missing_evidence_is_unknown_not_ok():
    """실행 시작 이벤트를 놓쳤다면 '이상 없음'이 아니라 '판정 불가'."""
    r = evaluate_run(run_events([("geo_loc", {"q": "a"}), ("geo_loc", {"q": "a"})], start=False))
    assert r["verdict"] == "unknown" and r["basis"] is False and r["warnings"] == []
    assert set(r["checks"].values()) == {"na"}
    assert evaluate_run([])["verdict"] == "unknown"


# ------------------------------------------------------------------ 진단 (이상 실행에만, 판정과 분리)
def make_evaluator(tmp_path, llm):
    store = EvalStore(tmp_path / "evals")
    return store, RunEvaluator(store, llm.diagnose, background=False)


def test_diagnosis_only_for_anomaly_and_kept_separate(tmp_path):
    llm = FakeLLM()
    store, evaluator = make_evaluator(tmp_path, llm)
    evaluator.on_run_end(SID, run_events([("geo_loc", {"q": "a"})]))                      # 정상
    evaluator.on_run_end(SID, run_events([("geo_loc", {"q": "a"})] * 2))                  # 이상(반복 1건)
    evaluator.on_run_end(SID, run_events(start=False))                                    # 판정 불가
    ok, bad, unknown = store.list(SID)
    assert [ok["verdict"], bad["verdict"], unknown["verdict"]] == ["ok", "anomaly", "unknown"]
    assert len(llm.diagnose_calls) == 1  # 이상 판정 실행에만 LLM 호출
    assert ok["diagnosis"]["status"] == "skipped" and unknown["diagnosis"]["status"] == "skipped"
    assert bad["diagnosis"]["status"] == "done"
    # 진단은 판정과 구분되어 저장되고 경고 수에 섞이지 않는다
    assert bad["warning_count"] == 1 == len(bad["warnings"]) and "diagnosis" not in str(bad["warnings"])
    assert bad["diagnosis"]["text"] and "경고" not in bad["diagnosis"]["text"]
    # 세 문장 이내
    assert bad["diagnosis"]["text"].count("입니다") + bad["diagnosis"]["text"].count("하세요") <= 3
    assert "네 번째" not in bad["diagnosis"]["text"]


def test_diagnosis_input_contains_warnings_and_event_summary(tmp_path):
    llm = FakeLLM()
    _, evaluator = make_evaluator(tmp_path, llm)
    evaluator.on_run_end(SID, run_events([("geo_loc", {"q": "a"})] * 2))
    summary, warnings = llm.diagnose_calls[0]
    assert "tool_req geo_loc" in summary and "step_start analyze" in summary
    assert warnings[0]["code"] == "repeated_call"


def test_diagnosis_failure_keeps_judgment(tmp_path):
    llm = FakeLLM()
    llm.diagnose_error = RuntimeError("llm down")
    store, evaluator = make_evaluator(tmp_path, llm)
    rec = evaluator.on_run_end(SID, run_events([("geo_loc", {"q": "a"})] * 2))
    saved = store.latest(SID)
    assert saved["verdict"] == "anomaly" and saved["warning_count"] == 1       # 판정은 그대로
    assert saved["diagnosis"] == {"status": "failed", "text": None} and rec["run_index"] == 1


def test_trim_sentences():
    assert trim_sentences("하나. 둘. 셋. 넷. 다섯.") == "하나. 둘. 셋."
    assert trim_sentences("한 문장") == "한 문장"


def test_store_is_per_session_and_indexes_runs(tmp_path):
    store = EvalStore(tmp_path / "evals")
    store.append("sess-aaaa-0001", {"verdict": "ok", "diagnosis": {"status": "skipped"}})
    store.append("sess-aaaa-0001", {"verdict": "anomaly", "diagnosis": {"status": "skipped"}})
    store.append("sess-bbbb-0002", {"verdict": "ok", "diagnosis": {"status": "skipped"}})
    assert [r["run_index"] for r in store.list("sess-aaaa-0001")] == [1, 2]
    assert store.summary("sess-aaaa-0001") == {"latest_verdict": "anomaly", "anomaly_runs": 1, "evaluated_runs": 2}
    assert store.list("../x") == [] and store.latest("nope-nope-nope") is None
    store.delete("sess-aaaa-0001")
    assert store.list("sess-aaaa-0001") == [] and len(store.list("sess-bbbb-0002")) == 1


# ------------------------------------------------------------------ 실제 Agent 실행과의 연결
def test_normal_agent_run_is_ok_and_makes_no_diagnosis_llm_call(make_container):
    c = make_container(meet())
    c.agent.run(SID, "요청")
    e = c.evals.latest(SID)
    assert e["verdict"] == "ok" and e["warning_count"] == 0 and e["stats"]["tool_calls"] > 0
    assert c.agent.llm.diagnose_calls == []           # 정상 실행에는 LLM 을 호출하지 않는다
    assert e["diagnosis"]["status"] == "skipped"


def test_replan_limit_run_is_anomaly_and_diagnosed(make_container, world, monkeypatch):
    monkeypatch.setenv("T_MAX", "20")
    world.stations = world.far(14)
    c = make_container(meet(prio="travel"))
    c.agent.run(SID, "요청")
    e = c.evals.latest(SID)
    assert e["verdict"] == "anomaly" and [w["code"] for w in e["warnings"]] == ["excess_replan"]
    assert e["stats"]["replans"] == 3 and e["diagnosis"]["status"] == "done"
    assert len(c.agent.llm.diagnose_calls) == 1


def test_each_run_is_evaluated_separately_without_mixing_events(make_container, world, monkeypatch):
    c = make_container(meet(), {"intent": "edit_cond", "reason": "r", "prio": "food"})
    c.agent.run(SID, "첫 요청")
    c.agent.run(SID, "후속 요청")
    first, second = c.evals.list(SID)
    assert [first["run_index"], second["run_index"]] == [1, 2]
    assert second["verdict"] == "ok" and second["stats"]["tool_calls"] == 0  # 후속은 캐시 재사용 → 직전 실행의 호출이 섞이지 않음
    assert first["stats"]["tool_calls"] > 0


def test_main_agent_references_previous_judgment(make_container, world, monkeypatch):
    monkeypatch.setenv("T_MAX", "20")
    world.stations = world.far(14)
    c = make_container(meet(prio="travel"), {"intent": "chat", "reason": "인사"})
    c.agent.run(SID, "첫 요청")                         # 이상(재계획 과다)
    c.agent.run(SID, "안녕")
    ends = [e for e in c.log_store.get_session(SID) if e["type"] == "step_end" and e["step"] == "analyze"]
    assert "prev_eval" not in ends[0]                   # 첫 실행에는 직전 판정이 없다
    assert ends[1]["prev_eval"] == "anomaly" and ends[1]["prev_warnings"] == ["excess_replan"]


def test_unknown_when_logging_agent_missed_run_start(make_container):
    """로깅 Agent 가 실행 도중 연결돼 시작 이벤트를 놓친 경우 → 판정 불가."""
    c = make_container(logging_enabled=True)
    la = c.logging_agent
    la.on_event(ev("step_start", step="plan"))
    la.on_event(ev("tool_req", tool="geo_loc", args={"q": "a"}))
    la.on_event(ev("run_end", status="ok", total_ms=1))
    e = c.evals.latest(SID)
    assert e["verdict"] == "unknown" and c.agent.llm.diagnose_calls == []


def test_evaluation_failure_does_not_break_conversation(make_container, monkeypatch):
    c = make_container(meet())
    monkeypatch.setattr(c.logging_agent.evaluator, "on_run_end", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert c.agent.run(SID, "요청")["status"] == "ok"
    assert c.log_store.get_session(SID)[-1]["type"] == "run_end"   # 로그는 그대로 저장된다


def test_without_logging_agent_there_is_no_evaluation(make_container):
    c = make_container(meet(), logging_enabled=False)
    assert c.agent.run(SID, "요청")["status"] == "ok" and c.evals.latest(SID) is None


# ------------------------------------------------------------------ API
@pytest.fixture
def client(make_container):
    c = make_container(meet(), meet(prio="food"))
    return TestClient(app), c


def test_api_chat_returns_evaluation_and_logs_expose_it(client):
    cl, c = client
    body = cl.post("/api/chat", json={"message": "잠실이랑 건대"}).json()
    sid = body["session_id"]
    assert body["evaluation"]["verdict"] == "ok" and body["evaluation"]["run_index"] == 1
    lst = cl.get("/api/logs").json()
    assert lst[0]["latest_verdict"] == "ok" and lst[0]["anomaly_runs"] == 0 and lst[0]["evaluated_runs"] == 1
    evs = cl.get(f"/api/logs/{sid}/evaluations").json()["evaluations"]
    assert len(evs) == 1 and set(evs[0]["checks"]) == {"repeated_call", "excess_replan", "unplanned_call"}
    # 평가는 읽기 전용: 수정 경로 없음
    for method in ("put", "patch", "post", "delete"):
        assert cl.request(method.upper(), f"/api/logs/{sid}/evaluations").status_code == 405
    assert cl.get("/api/logs/nonexistent-session/evaluations").status_code == 404


def test_api_deleting_log_deletes_its_evaluations(client):
    cl, c = client
    sid = cl.post("/api/chat", json={"message": "x"}).json()["session_id"]
    assert c.evals.list(sid)
    assert cl.delete(f"/api/logs/{sid}").status_code == 204
    assert c.evals.list(sid) == []


def test_api_chat_evaluation_is_none_without_logging_agent(make_container):
    c = make_container(meet(), logging_enabled=False)
    body = TestClient(app).post("/api/chat", json={"message": "x"}).json()
    assert body["evaluation"] is None and body["status"] == "ok"


# ------------------------------------------------------------------ 검증용 이상 주입 (ANOMALY_INJECT): 이상 실행을 '각각 만들어' 식별되는지 확인
def test_inject_repeat_call_is_flagged_as_repeated_call(make_container, monkeypatch):
    monkeypatch.setenv("ANOMALY_INJECT", "repeat_call")
    c = make_container(meet())
    assert c.agent.run(SID, "요청")["status"] == "ok"
    e = c.evals.latest(SID)
    assert e["verdict"] == "anomaly" and [w["code"] for w in e["warnings"]] == ["repeated_call"]
    assert e["warnings"][0]["evidence"]["tool"] == "get_wx" and e["warnings"][0]["evidence"]["count"] == 2
    assert e["diagnosis"]["status"] == "done" and len(c.agent.llm.diagnose_calls) == 1


def test_inject_unplanned_call_is_flagged_as_unplanned_call(make_container, monkeypatch):
    monkeypatch.setenv("ANOMALY_INJECT", "unplanned_call")
    c = make_container(meet())
    c.agent.run(SID, "요청")
    e = c.evals.latest(SID)
    assert e["verdict"] == "anomaly" and [w["code"] for w in e["warnings"]] == ["unplanned_call"]
    assert e["warnings"][0]["evidence"]["tools"] == {"get_wx": 1} and "get_wx" not in e["warnings"][0]["evidence"]["plan"]


def test_inject_missing_events_is_unknown_not_ok_and_not_diagnosed(make_container, monkeypatch):
    monkeypatch.setenv("ANOMALY_INJECT", "missing_events")
    c = make_container(meet())
    assert c.agent.run(SID, "요청")["status"] == "ok"            # 메인 Agent 는 정상 동작
    e = c.evals.latest(SID)
    assert e["verdict"] == "unknown" and e["basis"] is False and e["warnings"] == []
    assert set(e["checks"].values()) == {"na"}
    assert e["diagnosis"]["status"] == "skipped" and c.agent.llm.diagnose_calls == []   # 판정 불가에는 LLM 미호출
    log = c.log_store.get_session(SID)
    assert not any(x["type"] == "step_start" and x["step"] == "analyze" for x in log)   # 시작 이벤트가 실제로 없음
