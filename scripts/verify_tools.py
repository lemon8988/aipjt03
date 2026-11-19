"""검증 4.1 Tool 확인: 등록 명세 출력 + Tool 마다 정상 호출·실패 호출을 실제 API 로 실행해 결과를 표로 저장한다.

실행: python scripts/verify_tools.py        → docs/verification/tool_checks.md
(.env 의 KAKAO_REST_KEY 필요. 실패 케이스 일부는 키를 일부러 비우거나 잘못된 입력으로 만든다)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ["ROUTE_PROVIDER"] = "kakao"
from app.tools import call_tool, list_specs  # noqa: E402
from app.utils.geo import now_kst  # noqa: E402

OUT = ROOT / "docs" / "verification" / "tool_checks.md"
rows: list[tuple[str, str, str, str]] = []   # (Tool, 케이스, 입력, 결과)


def short(r: dict) -> str:
    if r["ok"]:
        d = r["data"]
        if "items" in d and "total" in d:
            return f"ok · 반경 내 {d['total']}건, 예시 {d['items'][0]['name']}"
        if "items" in d:
            return "ok · " + ", ".join(f"{i['name']}({i['x']:.4f},{i['y']:.4f})" for i in d["items"])
        if "stations" in d:
            return f"ok · 역 {len(d['stations'])}곳: " + ", ".join(s["name"] for s in d["stations"][:5]) + " … | basis: " + r["meta"]["basis"]
        if "min" in d:
            return f"ok · {d['min']}분, 환승 {d['transfers']}회, mode={d.get('mode')} | basis: {r['meta']['basis']}"
        if "rain_pct" in d:
            return f"ok · 강수확률 {d['rain_pct']}%, {d['tmin']}~{d['tmax']}°C"
        return "ok"
    e = r["error"]
    extra = ""
    if e["code"] == "AMBIGUOUS":
        extra = " · 후보: " + ", ".join(f"{c['name']}({c['addr']})" for c in e["detail"]["candidates"])
    return f"**실패 {e['code']}** · {e['message']}{extra}"


def run(tool: str, case: str, args: dict, env: dict | None = None) -> dict:
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        r = call_tool(tool, args)          # 예외가 밖으로 나오면 여기서 스크립트가 죽는다 → 그 자체가 실패 증거
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    shown = {k: v for k, v in args.items()}
    rows.append((tool, case, f"`{json.dumps(shown, ensure_ascii=False)}`" + (f" (환경: {env})" if env else ""), short(r)))
    print(f"[{tool}] {case}: {short(r)[:150]}")
    return r


def main() -> None:
    d = lambda n: (now_kst().date() + timedelta(days=n)).isoformat()  # noqa: E731
    # ---- geo_loc
    g = run("geo_loc", "정상: 역 이름이 아닌 동네 표현", {"q": "잠실"})
    x, y = g["data"]["items"][0]["x"], g["data"]["items"][0]["y"]
    run("geo_loc", "정상: 앞부분만 입력한 역 이름 ('건대' → 건대입구역)", {"q": "건대"})
    run("geo_loc", "실패: 같은 이름이 여러 곳", {"q": "중앙"})
    run("geo_loc", "실패: 결과 없음", {"q": "zzqqxx없는곳abc"})
    run("geo_loc", "실패: 필수 인자 누락", {})
    run("geo_loc", "실패: API 키 없음", {"q": "잠실"}, {"KAKAO_REST_KEY": ""})
    # ---- find_stn
    s = run("find_stn", "정상: 반경을 주지 않음 → 기본값 3000m 적용", {"x": x, "y": y})
    run("find_stn", "실패: 반경 안에 역 없음 (외진 좌표)", {"x": 127.9, "y": 37.9, "radius_m": 500})
    run("find_stn", "실패: 필수 인자 누락", {"x": x})
    run("find_stn", "실패: API 키 없음", {"x": x, "y": y}, {"KAKAO_REST_KEY": ""})
    st = s["data"]["stations"][0]
    # ---- get_route (find_stn 이 돌려준 역 좌표를 다음 Tool 입력으로 전달 = 값 전달 확인)
    run("get_route", "정상: find_stn 이 반환한 역 좌표를 도착지로 사용", {"from": {"x": 127.0703, "y": 37.5404}, "to": {"x": st["x"], "y": st["y"]}})
    run("get_route", "실패: 경로 없음 (서울→부산)", {"from": {"x": 126.9707, "y": 37.5547}, "to": {"x": 129.0403, "y": 35.1150}})
    run("get_route", "정상: 출발지=도착지", {"from": {"x": x, "y": y}, "to": {"x": x, "y": y}})
    run("get_route", "실패: 좌표 누락", {"from": {"x": x}, "to": {"x": st["x"], "y": st["y"]}})
    run("get_route", "실패: API 키 없음", {"from": {"x": x, "y": y}, "to": {"x": st["x"], "y": st["y"]}}, {"KAKAO_REST_KEY": ""})
    # ---- find_spot
    run("find_spot", "정상: 음식점 (total 은 반경 내 전체 건수)", {"x": st["x"], "y": st["y"], "radius_m": 500, "cat": "음식점", "size": 3})
    run("find_spot", "실패: 결과 없음 (외진 좌표의 문화시설)", {"x": 127.9, "y": 37.9, "radius_m": 50, "cat": "문화시설"})
    run("find_spot", "실패: 잘못된 카테고리", {"x": x, "y": y, "cat": "병원"})
    run("find_spot", "실패: API 키 없음", {"x": x, "y": y, "cat": "카페"}, {"KAKAO_REST_KEY": ""})
    # ---- get_wx
    run("get_wx", "정상: 이틀 뒤 예보", {"x": x, "y": y, "date": d(2)})
    run("get_wx", "실패: 예보 범위 밖(40일 뒤)", {"x": x, "y": y, "date": d(40)})
    run("get_wx", "실패: 과거 날짜", {"x": x, "y": y, "date": d(-3)})
    run("get_wx", "실패: 날짜 형식 오류", {"x": x, "y": y, "date": "내일"})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Tool 확인 결과 (검증 4.1)", "",
             f"실행 시각: {now_kst().strftime('%Y-%m-%d %H:%M')} KST · 실제 외부 API 호출 · 모든 호출이 예외 없이 아래 형식으로 반환됨", "",
             "## 등록된 Tool 명세 (이름 · 종류 · 설명 · 입력 스키마)", ""]
    for sp in list_specs():
        lines += [f"### {sp['name']} ({sp['kind']})", f"- 설명: {sp['description']}",
                  f"- 입력 스키마: `{json.dumps(sp['input_schema'], ensure_ascii=False)}`", ""]
    lines += ["## 정상·실패 호출 결과", "", "| Tool | 케이스 | 입력 | 결과 |", "|---|---|---|---|"]
    lines += [f"| {t} | {c} | {i} | {r.replace('|', '/')} |" for t, c, i, r in rows]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n저장: {OUT}  ({len(rows)}건)")


if __name__ == "__main__":
    main()
