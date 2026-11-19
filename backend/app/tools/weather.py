"""날씨 Tool get_wx — Open-Meteo (키 불필요, 비상업 무료, CC BY 4.0 출처 표기)."""
from __future__ import annotations

from datetime import date as Date

from app.config import settings
from app.tools import http
from app.tools.base import ToolError, ToolSpec, ok, register
from app.utils.geo import now_kst

URL = "https://api.open-meteo.com/v1/forecast"


def _get_wx(args: dict) -> dict:
    try:
        target = Date.fromisoformat(str(args["date"]))
        x, y = float(args["x"]), float(args["y"])
    except (ValueError, TypeError):
        raise ToolError("BAD_INPUT", "date(YYYY-MM-DD) 또는 좌표 형식이 올바르지 않습니다.") from None
    today = now_kst().date()
    if target < today:
        raise ToolError("BAD_INPUT", "과거 날짜의 예보는 조회할 수 없습니다.")
    if (target - today).days > settings().wx_max_days:
        raise ToolError("OUT_OF_RANGE", f"예보 범위(오늘 + {settings().wx_max_days}일)를 벗어난 날짜입니다.")
    data = http.request_json(URL, params={
        "latitude": y, "longitude": x, "timezone": "Asia/Seoul",
        "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
        "start_date": target.isoformat(), "end_date": target.isoformat()})
    daily = (data or {}).get("daily") or {}
    try:
        rain = daily["precipitation_probability_max"][0]
        tmax, tmin = daily["temperature_2m_max"][0], daily["temperature_2m_min"][0]
    except (KeyError, IndexError, TypeError):
        raise ToolError("API_ERROR", "날씨 응답 구조가 예상과 다릅니다.") from None
    if rain is None:
        raise ToolError("NO_RESULT", "해당 날짜의 강수확률이 제공되지 않습니다.")
    return ok({"rain_pct": rain, "tmax": tmax, "tmin": tmin},
              basis=f"{target.isoformat()} 일별 예보 (좌표 기준)",
              notes=["Weather data by Open-Meteo.com (CC BY 4.0)"])


register(ToolSpec(
    name="get_wx",
    description="약속 날짜의 강수확률로 실내·야외 장소를 가를 때 쓴다. 날짜가 예보 범위 밖이면 OUT_OF_RANGE.",
    input_schema={"type": "object",
                  "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                 "date": {"type": "string", "description": "YYYY-MM-DD"}},
                  "required": ["x", "y", "date"]},
    fn=_get_wx, required=("x", "y", "date")))
