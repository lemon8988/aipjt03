"""이동시간 Tool get_route (F3003).

provider
- kakao: 카카오맵 대중교통 경로 조회 (GET /v2/routing/publictraffic, 실제 응답으로 확인함)
- mock : 개발·테스트용 모의 추정(직선거리 기반). 실제 이동시간이 아니며 meta.basis 에 그렇게 표기한다.

카카오 응답 (check_apis.py / 실제 호출로 확인한 구조)
  {"status": "OK"|"NO_RESULTS"|"EQUAL_POINTS"|"STARTNODES_NULL"|..., "properties": {...},
   "routes": [{"properties": {"type": "SUBWAY"|"BUS"|..., "totalTime": <초>, "totalDistance": <m>,
                              "transfers": <수>, "fare": {...}}, "steps": [...]}]}
- totalTime 은 초 단위 → 분으로 올림 없이 반올림한다. 경로가 여러 개면 가장 짧은 것을 쓴다.
- 서울-부산처럼 먼 구간과 바다·무정류장 지점은 NO_RESULTS/STARTNODES_NULL 로 온다 → NO_ROUTE.
- 직선 300m 안팎의 아주 가까운 구간은 경로가 NO_RESULTS 로 오는 경우가 있다(방향에 따라 다름).
  이때 후보 역이 제외되면 오히려 가장 좋은 후보를 잃으므로, 직선 500m 이하이면 도보 추정으로 대체하고 그 사실을 밝힌다.
"""
from __future__ import annotations

import math

from app.config import settings
from app.tools import http
from app.tools.base import ToolError, ToolSpec, ok, register
from app.utils.geo import haversine_m

KAKAO_ROUTE_URL = "https://dapi.kakao.com/v2/routing/publictraffic"
WALK_MAX_M = 500        # 이 이하의 가까운 구간만 경로 없음을 도보로 대체
WALK_M_PER_MIN = 75     # 시속 4.5km
WALK_DETOUR = 1.3       # 직선 → 도보 거리 보정


def _point(p, label: str) -> tuple[float, float]:
    try:
        return float(p["x"]), float(p["y"])
    except (KeyError, TypeError, ValueError):
        raise ToolError("BAD_INPUT", f"{label} 좌표(x, y)가 올바르지 않습니다.") from None


def _mock(a: tuple[float, float], b: tuple[float, float]) -> dict:
    km = haversine_m(a[0], a[1], b[0], b[1]) / 1000
    minutes = int(round(km * 1.3 / 28 * 60 + 8))
    return ok({"min": minutes, "transfers": int(km // 12), "mode": "mock"},
              basis="모의 추정(개발용, 실제 이동시간 아님)",
              notes=["ROUTE_PROVIDER=mock: 직선거리 기반 추정값입니다."])


def _walk_estimate(dist_m: float) -> dict:
    minutes = max(1, math.ceil(dist_m * WALK_DETOUR / WALK_M_PER_MIN))
    return ok({"min": minutes, "transfers": 0, "mode": "walk_estimate"},
              basis="도보 추정 (대중교통 경로 없음, 직선거리 기준)",
              notes=[f"직선 {int(dist_m)}m 구간이라 대중교통 경로가 없어 도보로 추정했습니다 (직선×{WALK_DETOUR}, 분속 {WALK_M_PER_MIN}m)."])


def _parse_kakao(payload: object, dist_m: float) -> dict:
    if not isinstance(payload, dict) or "status" not in payload:
        raise ToolError("API_ERROR", "카카오 대중교통 응답 구조가 예상과 다릅니다.")
    status = payload["status"]
    if status == "OK":
        times = []
        for r in payload.get("routes") or []:
            p = (r or {}).get("properties") or {}
            if isinstance(p.get("totalTime"), (int, float)):
                times.append((p["totalTime"], int(p.get("transfers") or 0), p.get("type")))
        if not times:
            raise ToolError("API_ERROR", "카카오 대중교통 응답에 소요시간이 없습니다.")
        sec, transfers, kind = min(times)  # 가장 짧은 경로
        return ok({"min": max(1, int(sec / 60 + 0.5)), "transfers": transfers, "mode": "transit",
                   "type": kind, "alternatives": len(times)},
                  basis="조회 시점 기준 대중교통 최단 경로 (약속 시각의 실제 운행과 다를 수 있음)")
    if status == "EQUAL_POINTS":
        return ok({"min": 0, "transfers": 0, "mode": "same_place"}, basis="출발지와 도착지가 같은 위치")
    # 경로 없음 계열: NO_RESULTS, STARTNODES_NULL, ENDNODES_NULL ...
    if status == "NO_RESULTS" or str(status).endswith("NODES_NULL"):
        if dist_m <= WALK_MAX_M:
            return _walk_estimate(dist_m)
        raise ToolError("NO_ROUTE", "두 지점 사이의 대중교통 경로를 찾지 못했습니다.", {"status": status})
    raise ToolError("API_ERROR", "카카오 대중교통이 알 수 없는 상태를 반환했습니다.", {"status": status})


def _get_route(args: dict) -> dict:
    a, b = _point(args.get("from"), "출발"), _point(args.get("to"), "도착")
    provider = settings().route_provider
    if provider == "mock":
        return _mock(a, b)
    if provider == "kakao":
        key = settings().kakao_key
        if not key:
            raise ToolError("NO_KEY", "KAKAO_REST_KEY 가 설정되지 않았습니다.")
        payload = http.request_json(
            KAKAO_ROUTE_URL,
            params={"start_x": a[0], "start_y": a[1], "end_x": b[0], "end_y": b[1]},
            headers={"Authorization": f"KakaoAK {key}"})
        return _parse_kakao(payload, haversine_m(a[0], a[1], b[0], b[1]))
    raise ToolError("BAD_INPUT", f"알 수 없는 ROUTE_PROVIDER: {provider}")


_PT = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
       "required": ["x", "y"]}

register(ToolSpec(
    name="get_route",
    description="참여자 위치에서 후보 역까지의 대중교통 이동시간(분)과 환승 수가 필요할 때 쓴다. 조회 시점 기준이며 약속 시각의 운행과 다를 수 있다.",
    input_schema={"type": "object", "properties": {"from": _PT, "to": _PT}, "required": ["from", "to"]},
    fn=_get_route, required=("from", "to")))
