"""백엔드 API·계획 저장소 확인 (F3015~F3017, F3014)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store.plans import overlaps
from conftest import meet

PLAN = {"date": "2026-12-05", "start_time": "14:00", "end_time": "17:00", "participants": ["잠실", "건대"],
        "purpose": "데이트", "priority": "things", "station": "강변역", "spot": "카페 A"}


@pytest.fixture
def client(make_container):
    c = make_container(meet(), meet(prio="food"))
    return TestClient(app), c


def test_overlap_boundaries():
    p = {"date": "d", "start_time": "10:00", "end_time": "12:00"}
    assert overlaps(p, {"date": "d", "start_time": "11:59", "end_time": "13:00"})
    assert not overlaps(p, {"date": "d", "start_time": "12:00", "end_time": "13:00"})  # 맞닿음은 겹침 아님
    assert not overlaps(p, {"date": "x", "start_time": "10:00", "end_time": "12:00"})
    # 종료 시각이 없으면 시작 + 3시간 으로 겹침만 판정한다
    assert overlaps({"date": "d", "start_time": "10:00", "end_time": None}, {"date": "d", "start_time": "12:30", "end_time": None})
    assert not overlaps({"date": "d", "start_time": "10:00", "end_time": None}, {"date": "d", "start_time": "13:00", "end_time": None})


def test_plan_crud_and_conflict_notice(client):
    cl, _ = client
    r = cl.post("/api/plans", json=PLAN)
    assert r.status_code == 201
    first = r.json()
    assert first["conflicts"] == [] and first["plan"]["created_at"] and first["plan"]["id"] == 1

    r = cl.post("/api/plans", json={**PLAN, "start_time": "16:00", "end_time": "18:00", "station": "건대입구역"})
    assert r.status_code == 201  # 겹쳐도 저장은 막지 않는다
    assert [x["id"] for x in r.json()["conflicts"]] == [1]
    second_id = r.json()["plan"]["id"]

    assert len(cl.get("/api/plans").json()) == 2
    assert cl.get(f"/api/plans/{second_id}").json()["station"] == "건대입구역"

    r = cl.put(f"/api/plans/{second_id}", json={"start_time": "18:00", "end_time": "20:00", "spot": "식당 B"})
    assert r.status_code == 200 and r.json()["conflicts"] == [] and r.json()["plan"]["spot"] == "식당 B"
    assert cl.get(f"/api/plans/{second_id}").json()["start_time"] == "18:00"  # 수정 반영

    assert cl.delete(f"/api/plans/{second_id}").status_code == 204
    assert [p["id"] for p in cl.get("/api/plans").json()] == [1]
    assert cl.get(f"/api/plans/{second_id}").status_code == 404 and cl.delete(f"/api/plans/{second_id}").status_code == 404


def test_plan_validation(client):
    cl, _ = client
    assert cl.post("/api/plans", json={**PLAN, "date": "내일"}).status_code == 422
    assert cl.post("/api/plans", json={**PLAN, "end_time": "13:00"}).status_code == 422
    assert cl.post("/api/plans", json={**PLAN, "end_time": None}).status_code == 201  # 종료 시각은 비워 둘 수 있다
    pid = cl.get("/api/plans").json()[0]["id"]
    assert cl.put(f"/api/plans/{pid}", json={"end_time": "09:00"}).status_code == 422


def test_plan_stores_no_coordinates_or_urls(client):
    cl, _ = client
    cl.post("/api/plans", json={**PLAN, "x": 127.1, "url": "http://x"})
    assert set(cl.get("/api/plans/1").json()) == {"id", "date", "start_time", "end_time", "participants", "purpose",
                                                  "priority", "station", "spot", "created_at", "updated_at"}


def test_chat_endpoint_returns_answer_and_timeline(client):
    cl, _ = client
    r = cl.post("/api/chat", json={"message": "잠실이랑 건대"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "answer" and body["session_id"] and body["result"]["recommendations"]
    kinds = {e["type"] for e in body["timeline"]}
    assert {"step_start", "tool_req", "tool_res", "llm_decision"} <= kinds
    assert "api_key" not in r.text.lower() and "test-kakao" not in r.text
    r2 = cl.post("/api/chat", json={"session_id": body["session_id"], "message": "다른 요청"})
    assert r2.json()["session_id"] == body["session_id"]


def test_chat_validation(client):
    cl, _ = client
    assert cl.post("/api/chat", json={"message": "  "}).status_code == 422
    assert cl.post("/api/chat", json={"message": "a" * 501}).status_code == 422
    assert cl.post("/api/chat", json={"session_id": "../x", "message": "a"}).status_code == 422


def test_logs_api_list_detail_delete_and_no_update(client):
    cl, _ = client
    sid = cl.post("/api/chat", json={"message": "요청"}).json()["session_id"]
    lst = cl.get("/api/logs").json()
    assert [s["session_id"] for s in lst] == [sid] and lst[0]["runs"] == 1
    detail = cl.get(f"/api/logs/{sid}").json()
    assert detail["events"][0]["type"] == "step_start" and detail["events"][-1]["type"] == "run_end"
    # 수정 API 없음
    for method in ("put", "patch", "post"):
        assert getattr(cl, method)(f"/api/logs/{sid}", json={}).status_code == 405
    assert cl.delete(f"/api/logs/{sid}").status_code == 204
    assert cl.get(f"/api/logs/{sid}").status_code == 404
    assert cl.get("/api/logs/..%2Fetc").status_code in (404, 422)


def test_health_reports_presence_not_values(client, monkeypatch):
    cl, _ = client
    body = cl.get("/api/health").json()
    assert body["kakao_key"] is True and body["route_provider"] == "mock" and "test-kakao" not in str(body)
    monkeypatch.setenv("KAKAO_REST_KEY", "")
    assert cl.get("/api/health").json()["kakao_key"] is False


def test_unhandled_error_hides_details(client, monkeypatch):
    cl, c = client
    monkeypatch.setattr(c.plans, "list", lambda: (_ for _ in ()).throw(RuntimeError("db password=hunter2")))
    r = TestClient(app, raise_server_exceptions=False).get("/api/plans")
    assert r.status_code == 500 and "hunter2" not in r.text and r.json()["error_code"] == "INTERNAL"
