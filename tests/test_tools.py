"""Tool 확인 (F3001~F3004)."""
from __future__ import annotations

import pytest

from app.tools import call_tool, list_specs
from conftest import doc, future_date


def test_registry_lists_name_description_schema():
    specs = {s["name"]: s for s in list_specs()}
    assert set(specs) == {"geo_loc", "find_stn", "get_route", "find_spot", "get_wx"}
    for s in specs.values():
        assert s["kind"] == "external" and s["description"] and s["input_schema"]["type"] == "object"
        assert "fn" not in s  # 구현은 노출하지 않는다


@pytest.mark.parametrize("name,args", [
    ("geo_loc", {}), ("find_stn", {"x": 1}), ("find_spot", {"x": 1, "y": 1}),
    ("get_route", {"from": {"x": 1, "y": 1}}), ("get_wx", {"x": 1, "y": 1}), ("nope", {}),
])
def test_bad_input_returns_failure_format_never_raises(name, args):
    r = call_tool(name, args)
    assert r["ok"] is False and r["error"]["code"] == "BAD_INPUT"
    assert set(r["error"]) == {"code", "message", "detail"}


def test_missing_kakao_key_is_NO_KEY(monkeypatch, world):
    monkeypatch.setenv("KAKAO_REST_KEY", "")
    for name, args in [("geo_loc", {"q": "잠실"}), ("find_stn", {"x": 127.0, "y": 37.5}),
                       ("find_spot", {"x": 127.0, "y": 37.5, "cat": "카페"})]:
        r = call_tool(name, args)
        assert r["ok"] is False and r["error"]["code"] == "NO_KEY"
    assert world.calls == []  # 키가 없으면 외부 호출 자체를 하지 않는다


def test_unexpected_exception_is_converted(monkeypatch):
    from app.tools import http

    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(http, "request_json", boom)
    r = call_tool("geo_loc", {"q": "잠실"})
    assert r["ok"] is False and r["error"]["code"] == "API_ERROR"
    assert "secret" not in str(r)  # 예외 메시지(상세)를 그대로 내보내지 않는다


def test_geo_loc_station_first_and_merges_lines(world):
    r = call_tool("geo_loc", {"q": "잠실"})
    assert r["ok"] and len(r["data"]["items"]) == 1
    item = r["data"]["items"][0]
    assert item["name"] == "잠실역" and set(item) == {"id", "name", "addr", "x", "y"}


def test_geo_loc_prefix_match_single_station(world):
    r = call_tool("geo_loc", {"q": "건대"})  # '건대입구역' 으로 해석
    assert r["ok"] and r["data"]["items"][0]["name"] == "건대입구역"


def test_geo_loc_ambiguous_same_name_far_apart(world):
    world.keyword["중앙역"] = [doc("c1", "중앙역 4호선", 126.93, 37.31), doc("c2", "중앙역 1호선", 129.07, 35.10)]
    r = call_tool("geo_loc", {"q": "중앙"})
    assert r["ok"] is False and r["error"]["code"] == "AMBIGUOUS"
    assert len(r["error"]["detail"]["candidates"]) == 2


def test_geo_loc_no_result(world):
    r = call_tool("geo_loc", {"q": "없는곳"})
    assert r["ok"] is False and r["error"]["code"] == "NO_RESULT"


def test_find_stn_dedup_radius_and_meta(world):
    r = call_tool("find_stn", {"x": 127.085, "y": 37.527, "radius_m": 3000})
    assert r["ok"]
    names = [s["name"] for s in r["data"]["stations"]]
    assert "석촌역" in names and all(n.endswith("역") and " " not in n for n in names)
    assert set(r["data"]["stations"][0]) == {"id", "name", "x", "y", "dist_m"}
    assert "3000" in r["meta"]["basis"]


def test_find_stn_no_result_when_radius_too_small(world):
    r = call_tool("find_stn", {"x": 127.085, "y": 37.527, "radius_m": 100})
    assert r["ok"] is False and r["error"]["code"] == "NO_RESULT"


def test_find_spot_total_and_items(world):
    r = call_tool("find_spot", {"x": 127.1, "y": 37.5, "cat": "음식점", "size": 3})
    assert r["ok"] and len(r["data"]["items"]) == 3 and r["data"]["total"] >= 5
    assert set(r["data"]["items"][0]) == {"id", "name", "cat", "addr", "url", "dist_m"}


def test_find_spot_bad_category(world):
    assert call_tool("find_spot", {"x": 1, "y": 1, "cat": "병원"})["error"]["code"] == "BAD_INPUT"


def test_get_wx_ok_and_attribution(world):
    r = call_tool("get_wx", {"x": 127.0, "y": 37.5, "date": future_date(3)})
    assert r["ok"] and r["data"]["rain_pct"] == 10
    assert any("Open-Meteo" in n for n in r["meta"]["notes"])


def test_get_wx_out_of_range_and_past(world):
    assert call_tool("get_wx", {"x": 1, "y": 1, "date": future_date(40)})["error"]["code"] == "OUT_OF_RANGE"
    assert call_tool("get_wx", {"x": 1, "y": 1, "date": future_date(-2)})["error"]["code"] == "BAD_INPUT"
    assert call_tool("get_wx", {"x": 1, "y": 1, "date": "내일"})["error"]["code"] == "BAD_INPUT"


def test_get_wx_null_probability_is_no_result(world, monkeypatch):
    from app.tools import http
    monkeypatch.setattr(http, "request_json", lambda *a, **k: {"daily": {
        "precipitation_probability_max": [None], "temperature_2m_max": [1], "temperature_2m_min": [0]}})
    assert call_tool("get_wx", {"x": 1, "y": 1, "date": future_date(1)})["error"]["code"] == "NO_RESULT"


def test_get_route_mock_is_labeled_not_real(world):
    r = call_tool("get_route", {"from": {"x": 127.10, "y": 37.51}, "to": {"x": 127.07, "y": 37.54}})
    assert r["ok"] and isinstance(r["data"]["min"], int)
    assert "모의" in r["meta"]["basis"]


def _kakao_route(monkeypatch, payload, captured=None):
    from app.tools import http
    monkeypatch.setenv("ROUTE_PROVIDER", "kakao")

    def fake(url, *, params=None, headers=None):
        if captured is not None:
            captured.update(url=url, params=params, headers=headers)
        return payload
    monkeypatch.setattr(http, "request_json", fake)


FAR = {"from": {"x": 127.1001, "y": 37.5133}, "to": {"x": 127.0703, "y": 37.5404}}  # 직선 약 4km
NEAR = {"from": {"x": 127.1001, "y": 37.5133}, "to": {"x": 127.1012, "y": 37.5133}}  # 직선 약 100m


def _route(total_time, transfers=0, kind="BUS"):
    return {"properties": {"type": kind, "totalTime": total_time, "totalDistance": 5000, "transfers": transfers,
                           "fare": {"value": 1550}}, "steps": []}


def test_get_route_kakao_picks_shortest_and_converts_seconds_to_minutes(monkeypatch):
    # 실제 응답 구조(확인함): totalTime 은 초 단위, 경로 여러 개
    sent = {}
    _kakao_route(monkeypatch, {"status": "OK", "properties": {"total": 3}, "routes": [
        _route(1253), _route(741, 0, "SUBWAY"), _route(1535, 1)]}, sent)
    r = call_tool("get_route", FAR)
    assert r["ok"] and r["data"]["min"] == 12 and r["data"]["transfers"] == 0 and r["data"]["type"] == "SUBWAY"
    assert r["data"]["mode"] == "transit" and "조회 시점" in r["meta"]["basis"]
    assert set(sent["params"]) == {"start_x", "start_y", "end_x", "end_y"}  # 실제 요구 파라미터명
    assert sent["headers"]["Authorization"].startswith("KakaoAK ")


def test_get_route_kakao_no_results_far_is_NO_ROUTE(monkeypatch):
    for status in ("NO_RESULTS", "STARTNODES_NULL", "ENDNODES_NULL"):
        _kakao_route(monkeypatch, {"status": status, "properties": {"total": 0}, "routes": []})
        r = call_tool("get_route", FAR)
        assert r["ok"] is False and r["error"]["code"] == "NO_ROUTE"


def test_get_route_kakao_no_results_very_near_is_flagged_walk_estimate(monkeypatch):
    _kakao_route(monkeypatch, {"status": "NO_RESULTS", "properties": {"total": 0}, "routes": []})
    r = call_tool("get_route", NEAR)
    assert r["ok"] and r["data"]["mode"] == "walk_estimate" and r["data"]["min"] >= 1
    assert "도보 추정" in r["meta"]["basis"] and r["meta"]["notes"]  # 추정임을 밝힌다


def test_get_route_kakao_equal_points_is_zero_minutes(monkeypatch):
    _kakao_route(monkeypatch, {"status": "EQUAL_POINTS", "properties": {"total": 0}, "routes": []})
    r = call_tool("get_route", NEAR)
    assert r["ok"] and r["data"]["min"] == 0 and r["data"]["mode"] == "same_place"


def test_get_route_kakao_unexpected_shapes_fail_safely(monkeypatch):
    for payload in ({"status": "OK", "routes": []}, {"status": "WEIRD"}, {"nope": 1}, [1, 2]):
        _kakao_route(monkeypatch, payload)
        r = call_tool("get_route", FAR)
        assert r["ok"] is False and r["error"]["code"] == "API_ERROR"


def test_get_route_kakao_without_key_makes_no_call(monkeypatch):
    called = []
    _kakao_route(monkeypatch, {"status": "OK", "routes": [_route(600)]}, called)
    monkeypatch.setenv("KAKAO_REST_KEY", "")
    assert call_tool("get_route", FAR)["error"]["code"] == "NO_KEY" and called == []


def test_fail_injection_every_nth(monkeypatch, world):
    monkeypatch.setenv("FAIL_INJECT", "get_route:API_ERROR:2")
    args = {"from": {"x": 127.10, "y": 37.51}, "to": {"x": 127.07, "y": 37.54}}
    codes = [None if call_tool("get_route", args)["ok"] else "fail" for _ in range(4)]
    assert codes == [None, "fail", None, "fail"]


def test_kakao_quota_400_is_rate_limit_not_bad_input(monkeypatch):
    """카카오는 호출 한도 초과를 HTTP 400 으로 준다 → RATE_LIMIT(치명, 즉시 중단·재시도 안내)로 분류."""
    import pytest
    from app.tools import http
    from app.tools.base import ToolError

    class R:
        def __init__(self, status, text):
            self.status_code, self.text = status, text

    body = '{"errorType":"BadRequest","message":"API limit has been exceeded.","code":-10,"msg":"API limit has been exceeded."}'
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: R(400, body))
    with pytest.raises(ToolError) as e:
        http.request_json("https://dapi.kakao.com/v2/routing/publictraffic", params={"x": 1})
    assert e.value.code == "RATE_LIMIT"

    monkeypatch.setattr(http.requests, "get", lambda *a, **k: R(400, '{"message":"invalid parameter"}'))
    with pytest.raises(ToolError) as e:
        http.request_json("https://dapi.kakao.com/v2/routing/publictraffic")
    assert e.value.code == "BAD_INPUT"
