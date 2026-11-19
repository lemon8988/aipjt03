"""카카오 로컬 API 래퍼: geo_loc, find_stn, find_spot (F3002, F3004).

주의: 응답 필드는 카카오 로컬 공개 문서 기준이며, scripts/check_apis.py 로 실제 응답과 대조해야 한다.
"""
from __future__ import annotations

import re

from app.config import settings
from app.tools import http
from app.tools.base import ToolError, ToolSpec, fail, ok, register
from app.utils.geo import haversine_m

BASE = "https://dapi.kakao.com"
CATEGORY_CODES = {"음식점": "FD6", "카페": "CE7", "문화시설": "CT1", "관광명소": "AT4"}
_STATION_RE = re.compile(r"^(.+?역)(?:\s.*)?$")


def _get(path: str, params: dict) -> dict:
    key = settings().kakao_key
    if not key:
        raise ToolError("NO_KEY", "KAKAO_REST_KEY 가 설정되지 않았습니다.")
    data = http.request_json(BASE + path, params=params, headers={"Authorization": f"KakaoAK {key}"})
    if not isinstance(data, dict) or "documents" not in data:
        raise ToolError("API_ERROR", "카카오 응답 구조가 예상과 다릅니다.")
    return data


def station_key(name: str) -> str:
    """'잠실역 2호선' → '잠실역' (같은 역의 노선별 중복을 합치기 위함)."""
    m = _STATION_RE.match(name.strip())
    return m.group(1) if m else name.strip()


def _addr(d: dict) -> str:
    return d.get("road_address_name") or d.get("address_name") or ""


def _group_stations(docs: list[dict]) -> list[dict]:
    """같은 역명 + 1.5km 이내는 한 역으로 합친다. 멀리 떨어진 동명 역은 별개로 둔다."""
    groups: list[dict] = []
    for d in docs:
        key, x, y = station_key(d["place_name"]), float(d["x"]), float(d["y"])
        for g in groups:
            if g["name"] == key and haversine_m(x, y, g["x"], g["y"]) <= 1500:
                break
        else:
            groups.append({"id": d["id"], "name": key, "addr": _addr(d), "x": x, "y": y,
                           "_dist": d.get("distance")})
    return groups


def _place(d: dict) -> dict:
    return {"id": d["id"], "name": d["place_name"], "addr": _addr(d),
            "x": float(d["x"]), "y": float(d["y"])}


def _geo_loc(args: dict) -> dict:
    q = str(args["q"]).strip()
    size = max(1, min(int(args.get("size") or 5), 15))
    if not q:
        raise ToolError("BAD_INPUT", "위치 이름이 비어 있습니다.")

    # 1) 지하철역 우선: "잠실" → "잠실역" 으로 역 검색
    want = q if q.endswith("역") else q + "역"
    docs = _get("/v2/local/search/keyword.json",
                {"query": want, "category_group_code": "SW8", "size": 15})["documents"]
    groups = _group_stations(docs)
    exact = [g for g in groups if g["name"] == want]
    pick = exact or [g for g in groups if g["name"].startswith(q)]
    if len(pick) == 1:
        g = pick[0]
        item = {k: g[k] for k in ("id", "name", "addr", "x", "y")}
        return ok({"items": [item]}, basis="카카오 로컬 키워드 검색(지하철역 우선)")
    if len(pick) > 1:
        cands = [{k: g[k] for k in ("id", "name", "addr", "x", "y")} for g in pick[:size]]
        return fail("AMBIGUOUS", f"'{q}' 에 해당하는 위치가 여러 곳입니다.", {"candidates": cands})

    # 2) 일반 키워드 검색
    docs = _get("/v2/local/search/keyword.json", {"query": q, "size": 15})["documents"]
    if not docs:
        raise ToolError("NO_RESULT", f"'{q}' 위치를 찾지 못했습니다.")
    items = [_place(d) for d in docs]
    exact_items = [i for i in items if i["name"].strip() == q]
    if len(exact_items) == 1 or len(items) == 1:
        return ok({"items": [(exact_items or items)[0]]}, basis="카카오 로컬 키워드 검색")
    return fail("AMBIGUOUS", f"'{q}' 에 해당하는 위치가 여러 곳입니다.", {"candidates": items[:size]})


def _find_stn(args: dict) -> dict:
    x, y = float(args["x"]), float(args["y"])
    radius = max(100, min(int(args.get("radius_m") or 3000), 20000))
    size = max(1, min(int(args.get("size") or 15), 30))
    docs: list[dict] = []
    for page in (1, 2):  # 노선별 중복 때문에 역 수가 모자라면 한 페이지 더
        data = _get("/v2/local/search/category.json",
                    {"category_group_code": "SW8", "x": x, "y": y, "radius": radius,
                     "sort": "distance", "size": 15, "page": page})
        docs += data["documents"]
        if data.get("meta", {}).get("is_end", True) or len(_group_stations(docs)) >= size:
            break
    groups = _group_stations(docs)
    if not groups:
        raise ToolError("NO_RESULT", f"반경 {radius}m 안에 지하철역이 없습니다.")
    stations = []
    for g in groups[:size]:
        dist = float(g["_dist"]) if g.get("_dist") not in (None, "") else haversine_m(x, y, g["x"], g["y"])
        stations.append({"id": g["id"], "name": g["name"], "x": g["x"], "y": g["y"], "dist_m": int(dist)})
    notes = []
    if radius != int(args.get("radius_m") or 3000):
        notes.append(f"요청 반경 {args.get('radius_m')}m 를 {radius}m 로 조정했습니다.")
    return ok({"stations": stations}, basis=f"중심 좌표 반경 {radius}m", notes=notes)


def _find_spot(args: dict) -> dict:
    cat = args.get("cat")
    if cat not in CATEGORY_CODES:
        raise ToolError("BAD_INPUT", f"cat 은 {', '.join(CATEGORY_CODES)} 중 하나여야 합니다.")
    x, y = float(args["x"]), float(args["y"])
    radius = max(50, min(int(args.get("radius_m") or 500), 20000))
    size = max(1, min(int(args.get("size") or 5), 15))
    params = {"category_group_code": CATEGORY_CODES[cat], "x": x, "y": y, "radius": radius,
              "sort": "distance", "size": size}
    q = (args.get("q") or "").strip()
    if q:
        data = _get("/v2/local/search/keyword.json", {**params, "query": q})
    else:
        data = _get("/v2/local/search/category.json", params)
    docs = data["documents"]
    if not docs:
        raise ToolError("NO_RESULT", f"반경 {radius}m 안에 '{cat}' 결과가 없습니다.")
    meta = data.get("meta", {})
    items = [{"id": d["id"], "name": d["place_name"], "cat": cat, "addr": _addr(d),
              "url": d.get("place_url", ""), "dist_m": int(float(d.get("distance") or 0))} for d in docs]
    total = int(meta.get("total_count", len(items)))
    return ok({"items": items, "total": total, "pageable": int(meta.get("pageable_count", len(items)))},
              basis=f"반경 {radius}m 내 {cat}")


_XY = {"x": {"type": "number", "description": "경도"}, "y": {"type": "number", "description": "위도"}}

register(ToolSpec(
    name="geo_loc",
    description="사용자가 말한 위치(역명·동네·건물)를 좌표로 바꿔야 할 때 쓴다. 같은 이름이 여러 곳이면 AMBIGUOUS 로 후보를 돌려준다.",
    input_schema={"type": "object", "properties": {"q": {"type": "string", "description": "위치 이름"},
                                                   "size": {"type": "integer", "default": 5}},
                  "required": ["q"]},
    fn=_geo_loc, required=("q",)))

register(ToolSpec(
    name="find_stn",
    description="참여자 좌표의 중심 주변에서 만남 후보 지하철역이 필요할 때 쓴다.",
    input_schema={"type": "object", "properties": {**_XY, "radius_m": {"type": "integer", "default": 3000},
                                                   "size": {"type": "integer", "default": 15}},
                  "required": ["x", "y"]},
    fn=_find_stn, required=("x", "y")))

register(ToolSpec(
    name="find_spot",
    description="후보 역 주변의 음식점·카페·문화시설·관광명소를 찾거나 반경 내 개수가 필요할 때 쓴다. 어떤 영화·상영 정보 같은 것은 알려주지 않는다.",
    input_schema={"type": "object", "properties": {**_XY, "radius_m": {"type": "integer", "default": 500},
                                                   "cat": {"type": "string", "enum": list(CATEGORY_CODES)},
                                                   "q": {"type": "string"}, "size": {"type": "integer", "default": 5}},
                  "required": ["x", "y", "cat"]},
    fn=_find_spot, required=("x", "y", "cat")))
