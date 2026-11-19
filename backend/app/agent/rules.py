"""제약 판단·점수 계산 (F3008). LLM 을 쓰지 않는 순수 함수만 둔다 — 재현 가능하고 단위 테스트가 가능하다.

eval(후보 하나의 이동시간 조회 결과) 형식
  {"id","name","x","y","times":{참여자 idx: 분}, "transfers":{idx: 수},
   "status": "ok"|"no_route"|"error"|"pruned", "over": {"idx","min"} (pruned 일 때)}
"""
from __future__ import annotations

from app.utils.geo import haversine_m

CAT_ALL = ["음식점", "카페", "문화시설", "관광명소"]
OUTDOOR_CAT = "관광명소"  # 실내외 구분 데이터가 없어 비 올 때 일괄 제외 (설계 결정 B2)
PRIOS = {"travel": "이동 부담 적게", "things": "할 거 많게", "food": "음식점 많게"}


def pick_candidates(stations: list[dict], points: list[dict], k: int) -> list[dict]:
    """참여자 직선거리 합이 작은 상위 k개 (API 호출 없음)."""
    def total(s: dict) -> float:
        return sum(haversine_m(s["x"], s["y"], p["x"], p["y"]) for p in points)
    return sorted(stations, key=total)[:k]


def participant_order(station: dict, points: list[dict]) -> list[int]:
    """가장 먼 참여자부터 조회해야 상한 초과 시 나머지 호출을 건너뛸 수 있다 (호출량 절감)."""
    return sorted(range(len(points)),
                  key=lambda i: -haversine_m(station["x"], station["y"], points[i]["x"], points[i]["y"]))


def apply_constraints(evals: list[dict], n: int, t_max: int, g_max: int, names: list[str]
                      ) -> tuple[list[dict], list[dict]]:
    """제약 필터. 통과 후보(통계 포함)와 제외 사유 목록을 돌려준다."""
    passed, excluded = [], []
    for e in evals:
        st = e["status"]
        if st == "no_route":
            excluded.append({"name": e["name"], "reason": "경로를 찾지 못해 판단하지 않음 (NO_ROUTE)"})
        elif st == "error":
            excluded.append({"name": e["name"], "reason": "이동시간 조회에 실패해 판정 불가"})
        elif st == "pruned":
            o = e["over"]
            excluded.append({"name": e["name"],
                             "reason": f"{names[o['idx']]}의 이동시간 {o['min']}분이 상한 {t_max}분 초과"})
        else:
            ts = [e["times"][i] for i in range(n)]
            mx, mn = max(ts), min(ts)
            if mx > t_max:
                excluded.append({"name": e["name"], "reason": f"최대 이동시간 {mx}분이 상한 {t_max}분 초과"})
            elif mx - mn > g_max:
                excluded.append({"name": e["name"], "reason": f"이동시간 편차 {mx - mn}분이 상한 {g_max}분 초과"})
            else:
                passed.append({**e, "max_t": mx, "min_t": mn, "gap": mx - mn, "sum_t": sum(ts)})
    return passed, excluded


def is_replannable(evals: list[dict], candidate_count: int) -> bool:
    """재계획은 '제약 때문에' 충족 후보가 없을 때만 의미가 있다.
    후보 역이 아예 없거나, 판단이 이뤄진(ok/pruned/no_route) 후보가 있을 때. 전부 조회 실패(error)면 재계획하지 않는다."""
    if candidate_count == 0:
        return True
    return any(e["status"] in ("ok", "pruned", "no_route") for e in evals)


def near_miss(evals: list[dict], n: int, t_max: int, g_max: int) -> dict | None:
    """상한 도달 시 안내할 '가장 근접했던 후보'와 초과 폭."""
    best = None
    for e in evals:
        if e["status"] == "ok":
            ts = [e["times"][i] for i in range(n)]
            excess = max(max(ts) - t_max, (max(ts) - min(ts)) - g_max, 0)
            detail = f"최대 {max(ts)}분 / 편차 {max(ts) - min(ts)}분"
        elif e["status"] == "pruned":
            excess = e["over"]["min"] - t_max
            detail = f"최소 {e['over']['min']}분 이상"
        else:
            continue
        if best is None or excess < best["excess"]:
            best = {"name": e["name"], "excess": excess, "detail": detail}
    return best


def spot_targets(prio: str, passed: list[dict], top: int = 3) -> list[dict]:
    """주변 장소(find_spot)를 조회할 후보. 이동 부담 우선이면 순위와 **같은 정렬 기준**(이동시간 합 → 최대 이동시간)의
    상위 top 곳만, 그 밖의 우선순위는 점수 계산에 필요하므로 전부. 기준이 다르면 동점일 때 1위가 조회 대상에서 빠진다."""
    if prio == "travel":
        return sorted(passed, key=lambda c: (c["sum_t"], c["max_t"]))[:top]
    return list(passed)


def spot_categories(prio: str, rainy: bool) -> list[str]:
    cats = CAT_ALL if prio != "food" else ["음식점"]
    return [c for c in cats if not (rainy and c == OUTDOOR_CAT)]


def spot_score_cats(prio: str, rainy: bool) -> list[str]:
    """점수에 합산할 카테고리."""
    if prio == "food":
        return ["음식점"]
    if prio == "things":
        return [c for c in CAT_ALL if not (rainy and c == OUTDOOR_CAT)]
    return []


def score(prio: str, cand: dict, spots: dict[str, dict | None], rainy: bool) -> float | None:
    """높을수록 좋다. 확인하지 못한 값이 필요하면 None(판정 불가)."""
    if prio == "travel":
        return float(-cand["sum_t"])
    total = 0
    for cat in spot_score_cats(prio, rainy):
        info = spots.get(cat)
        if info is None or info.get("total") is None:
            return None
        total += info["total"]
    return float(total)


def rank(prio: str, passed: list[dict], spots: dict[str, dict[str, dict | None]], rainy: bool, top: int = 3
         ) -> tuple[list[dict], list[dict]]:
    scored, unknown = [], []
    for c in passed:
        s = score(prio, c, spots.get(c["id"], {}), rainy)
        if s is None:
            unknown.append({"name": c["name"], "reason": "주변 장소 건수를 확인하지 못해 판정 불가"})
        else:
            scored.append({**c, "score": s})
    scored.sort(key=lambda c: (-c["score"], c["sum_t"], c["max_t"]))
    return scored[:top], unknown
