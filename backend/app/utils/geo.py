from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


def now_kst() -> datetime:
    return datetime.now(KST)


def haversine_m(x1: float, y1: float, x2: float, y2: float) -> float:
    """x=경도, y=위도 (카카오 좌표 규칙)."""
    r = 6371000.0
    p1, p2 = math.radians(y1), math.radians(y2)
    dp = p2 - p1
    dl = math.radians(x2 - x1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def centroid(points: list[dict]) -> tuple[float, float]:
    n = len(points)
    return sum(p["x"] for p in points) / n, sum(p["y"] for p in points) / n
