"""기획 검증: 외부 API 3종과 LLM 을 한 번씩 호출해 응답 '구조'를 확인한다 (명세 2.7, 1일차 16시 기준).

사용:  python scripts/check_apis.py            # 카카오 로컬 + Open-Meteo + 카카오 대중교통
       python scripts/check_apis.py --llm      # LLM 연결 + Tool 선택까지 확인
       python scripts/check_apis.py --route-url URL --route-params "start_x=127.1&start_y=37.5&end_x=127.0&end_y=37.5"

카카오 응답 값은 약관상 저장 금지이므로, 결과 파일(docs/api_shapes/*.json)에는
필드 이름과 타입(숫자만 값)만 남기고 문자열 값은 저장하지 않는다. 키는 .env 에서만 읽고 출력하지 않는다.
이 결과로 tools/route.py 의 _parse_kakao 와 Tool 출력 스키마를 확정한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")  # cp949 콘솔에서 특수문자로 죽지 않게
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import requests  # noqa: E402

from app.config import settings  # noqa: E402
from app.utils.geo import now_kst  # noqa: E402

OUT = ROOT / "docs" / "api_shapes"
JAMSIL = (127.1001, 37.5133)
KONDAE = (127.0703, 37.5404)


def shape(v, depth=0):
    """값은 숨기고 구조만: 문자열→'str', 숫자→값, 리스트→[첫 원소 구조] + 길이."""
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return {"_list_len": len(v), "_item": shape(v[0], depth + 1) if v else None}
    if isinstance(v, str):
        return "str"
    return v


def safe_get(url, **kw):
    """네트워크 오류(DNS·SSL·타임아웃)는 예외 대신 안내 메시지로 보고한다."""
    try:
        return requests.get(url, **kw)
    except requests.RequestException as e:
        print(f"  연결 실패: {type(e).__name__}: 네트워크/방화벽/SSL 설정을 확인하세요.")
        return None


def report(name: str, resp: requests.Response | None, extra: dict | None = None):
    if resp is None:
        print(f"\n[FAIL] {name}  (요청을 보내지 못함)")
        return None
    ok = resp.status_code == 200
    print(f"\n[{'OK ' if ok else 'FAIL'}] {name}  HTTP {resp.status_code}")
    if ok:
        try:
            s = shape(resp.json())
        except ValueError:
            print("  JSON 이 아닌 응답"); return None
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}.json").write_text(json.dumps({"http": 200, "shape": s, **(extra or {})},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print("  구조:", json.dumps(s, ensure_ascii=False)[:900])
        return resp.json()
    print("  본문(앞 300자):", resp.text[:300].replace("\n", " "))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--route-url", default="https://dapi.kakao.com/v2/routing/publictraffic")
    ap.add_argument("--route-params", default=f"start_x={JAMSIL[0]}&start_y={JAMSIL[1]}&end_x={KONDAE[0]}&end_y={KONDAE[1]}")
    args = ap.parse_args()
    s = settings()
    print(f"KAKAO_REST_KEY 설정: {bool(s.kakao_key)} / LLM_API_KEY 설정: {bool(s.llm_key)}")
    h = {"Authorization": f"KakaoAK {s.kakao_key}"}
    kakao = "https://dapi.kakao.com"

    if s.kakao_key:
        d = report("kakao_keyword_SW8", safe_get(f"{kakao}/v2/local/search/keyword.json", headers=h, timeout=8,
                   params={"query": "잠실역", "category_group_code": "SW8", "size": 15}))
        if d:
            print("  → 같은 역이 노선별로 여러 건?:", len(d["documents"]), "건 /", len({x["place_name"].split()[0] for x in d["documents"]}), "개 역명")
        d = report("kakao_category_SW8", safe_get(f"{kakao}/v2/local/search/category.json", headers=h, timeout=8,
                   params={"category_group_code": "SW8", "x": JAMSIL[0], "y": JAMSIL[1], "radius": 3000, "sort": "distance", "size": 15}))
        if d:
            print("  → meta:", d.get("meta"))
        d = report("kakao_category_FD6", safe_get(f"{kakao}/v2/local/search/category.json", headers=h, timeout=8,
                   params={"category_group_code": "FD6", "x": JAMSIL[0], "y": JAMSIL[1], "radius": 500, "size": 3}))
        if d:
            m = d.get("meta", {})
            print(f"  → total_count={m.get('total_count')} pageable_count={m.get('pageable_count')}  (find_spot.total 에 어느 쪽을 쓸지 결정)")
        # 빈 결과 응답 형태 (실패 코드 NO_RESULT 근거)
        report("kakao_empty_result", safe_get(f"{kakao}/v2/local/search/keyword.json", headers=h, timeout=8,
               params={"query": "zzzzqqqq없는장소xx"}))
        # 대중교통 경로
        params = dict(parse_qsl(args.route_params))
        print(f"\n대중교통 경로 호출: {args.route_url}  params={params}")
        report("kakao_transit_route", safe_get(args.route_url, headers=h, params=params, timeout=10),
               {"request_params": list(params)})
        print("  ※ 실패하면 공식 문서(developers.kakao.com 카카오맵 REST API)에서 경로·파라미터·헤더를 확인해\n"
              "    --route-url / --route-params 로 다시 시도하세요. 앱에서 카카오맵 API 활성화가 필요합니다.")
    else:
        print("KAKAO_REST_KEY 가 없어 카카오 호출을 건너뜁니다. (.env 확인)")

    date = (now_kst().date()).isoformat()
    d = report("open_meteo_forecast", safe_get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
        "latitude": JAMSIL[1], "longitude": JAMSIL[0], "timezone": "Asia/Seoul",
        "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
        "start_date": date, "end_date": date}))
    if d:
        print("  → 오늘 강수확률 값:", d["daily"]["precipitation_probability_max"])

    if args.llm:
        print("\n[LLM] 연결·Tool 선택 확인")
        if not s.llm_key:
            print("  LLM_API_KEY 없음"); return
        from app.tools import openai_tools
        if s.llm_provider == "gemini":
            from app.agent.llm import LLMError, _gemini_text, gemini_generate, gemini_tool_declarations
            try:
                print("  1) 단순 호출:", _gemini_text(gemini_generate("", "한 단어로 인사해 주세요.", max_tokens=30)))
                data = gemini_generate("", "잠실이라는 장소의 좌표를 알고 싶어요.", max_tokens=200,
                                       tools=gemini_tool_declarations(openai_tools()))
                parts = data["candidates"][0]["content"]["parts"]
                calls = [(p_["functionCall"]["name"], p_["functionCall"].get("args")) for p_ in parts if "functionCall" in p_]
                print("  2) Tool 선택:", calls or "선택 없음 (설명/모델 확인 필요)")
            except (LLMError, KeyError, IndexError) as e:
                print(f"  LLM 호출 실패: {getattr(e, 'message', type(e).__name__)}")
            return
        from langchain_openai import ChatOpenAI
        m = ChatOpenAI(model=s.llm_model, api_key=s.llm_key, base_url=s.llm_base_url, temperature=0, timeout=40, max_retries=0)
        try:
            print("  1) 단순 호출:", m.invoke("한 단어로 인사해 주세요.").content[:60])
            r = m.bind_tools(openai_tools()).invoke("잠실이라는 장소의 좌표를 알고 싶어요.")
            print("  2) Tool 선택:", [(c["name"], c["args"]) for c in r.tool_calls] or "선택 없음 (설명/모델 확인 필요)")
        except Exception as e:  # noqa: BLE001
            print(f"  LLM 호출 실패: {type(e).__name__}: LLM_BASE_URL(엔드포인트 경로 제외), 키, 모델명, 네트워크를 확인하세요.")

if __name__ == "__main__":
    main()
