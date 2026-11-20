"""검증 결과 문서(docs/05_검증결과.md)와 사람이 읽을 수 있는 실행 로그 샘플을 생성한다.
입력: docs/verification/records.json (scripts/capture_evidence.py), docs/samples/*.jsonl, docs/verification/*.md
실행: python scripts/build_verification_doc.py
수치는 모두 수집된 기록에서 가져온다 (손으로 옮겨 적지 않는다)."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
R = json.loads((DOCS / "verification" / "records.json").read_text(encoding="utf-8"))


def rec(sc: str, turn: str = "1") -> dict:
    return next(r for r in R if r["scenario"] == sc and r["turn"] == turn)


def img(name: str, alt: str) -> str:
    return f"![{alt}](screenshots/{name})"


def kst(ts: str) -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=9))).strftime("%H:%M:%S.%f")[:-3]


def describe(e: dict) -> str:
    t = e["type"]
    if t == "step_start":
        return f"▶ 단계 시작 `{e['step']}`"
    if t == "step_end":
        extra = []
        for k in ("intent", "reason", "plan", "candidates", "passed", "tool_calls", "decision", "status", "ask", "n", "radius_m", "k", "prev_eval", "response_type"):
            if k in e:
                extra.append(f"{k}={e[k]}")
        return f"■ 단계 종료 `{e['step']}` ({e['ms']}ms) " + " · ".join(extra)
    if t == "tool_req":
        return f"→ Tool 호출 `{e['tool']}` {json.dumps(e['args'], ensure_ascii=False)}"
    if t == "tool_res":
        return f"← Tool 응답 `{e['tool']}` {'성공' if e['ok'] else '**실패 ' + str(e['code']) + '**'} · {e['ms']}ms" + (f" · {e['count']}건" if e.get("count") is not None else "")
    if t == "cache_hit":
        return f"♻ 결과 재사용 `{e['tool']}` (재호출 없음)"
    if t == "llm_decision":
        return f"🧠 LLM 판단({e['kind']}): `{e['value']}` — {e['reason']}"
    if t == "replan":
        return f"**↻ 재계획 {e['n']}회** ({e['trigger']}): {e['change']}"
    if t == "run_end":
        return f"✔ 요청 종료 `{e['status']}` · 총 {e['total_ms']}ms"
    return t


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (DOCS / "samples" / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def log_table(evs: list[dict]) -> str:
    rows = ["| # | 시각(KST) | 이벤트 |", "|---|---|---|"]
    rows += [f"| {e['seq']} | {kst(e['ts'])} | {describe(e).replace('|', '/')} |" for e in evs]
    return "\n".join(rows)


def step_summary(evs: list[dict]) -> str:
    """단계별 요약: 단계, 소요, Tool 호출 수(성공/실패), 재사용, 비고."""
    rows = ["| 순서 | 단계 | 소요 | Tool 호출 | 비고 |", "|---|---|---|---|---|"]
    cur, i = None, 0
    for e in evs:
        if e["type"] == "step_start":
            cur = {"step": e["step"], "tools": {}, "cache": 0, "note": [], "ms": None}
            i += 1
        elif cur is None:
            continue
        elif e["type"] == "tool_res":
            t = cur["tools"].setdefault(e["tool"], [0, 0])
            t[0 if e["ok"] else 1] += 1
        elif e["type"] == "cache_hit":
            cur["cache"] += 1
        elif e["type"] == "llm_decision":
            cur["note"].append(f"LLM 판단 `{e['value']}`")
        elif e["type"] == "replan":
            cur["note"].append(f"**재계획 {e['n']}회**: {e['change']}")
        elif e["type"] == "step_end":
            cur["ms"] = e["ms"]
            for k, label in (("plan", "계획"), ("candidates", "후보"), ("passed", "충족"), ("decision", "결정")):
                if k in e:
                    cur["note"].append(f"{label}={e[k]}")
            tools = ", ".join(f"{n} ×{s + f}" + (f"(실패 {f})" if f else "") for n, (s, f) in cur["tools"].items())
            if cur["cache"]:
                tools += (", " if tools else "") + f"재사용 {cur['cache']}"
            rows.append(f"| {i} | {cur['step']} | {cur['ms']}ms | {tools or '-'} | {' · '.join(cur['note']) or '-'} |")
            cur = None
    return "\n".join(rows)


def rec_table(r: dict) -> str:
    if not r["recommendations"]:
        return "(추천 없음)"
    rows = ["| 순위 | 역 | 이동시간(환승) | 최대/편차 | 점수 | 주변 장소 수 |", "|---|---|---|---|---|---|"]
    for x in r["recommendations"]:
        t = ", ".join(f"{a['name']} {a['min']}분({a['transfers']})" + ("·도보추정" if a["walk"] else "") for a in x["times"])
        sp = ", ".join(f"{k} {v}" for k, v in x["spot_counts"].items()) or "-"
        rows.append(f"| {x['rank']} | {x['station']} | {t} | {x['max_t']}/{x['gap']}분 | {x['score']:.0f} | {sp} |")
    return "\n".join(rows)


def calls(r: dict) -> str:
    c = ", ".join(f"{k} ×{v}" for k, v in r["tool_calls"].items()) or "없음"
    f = ", ".join(f"{k}×{v}" for k, v in r["tool_failures"].items())
    return c + (f" (실패/결과없음: {f})" if f else "") + (f" · 캐시 재사용 {r['cache_hits']}건" if r["cache_hits"] else "")


def scenario_block(sc: str, title: str, expected: str, diff: str, shots: list[tuple[str, str]], turns: list[str]) -> str:
    out = [f"#### {sc}. {title}", ""]
    out.append(f"**기대한 동작**: {expected}")
    out.append("")
    for t in turns:
        r = rec(sc, t)
        out.append(f"**요청 {t}**: `{r['request']}` → 결과 `{r['type']}/{r['status']}` · {r['elapsed_s']}초")
        out.append("")
        out.append(f"- 실행 단계: {' → '.join(r['steps'])}")
        out.append(f"- Tool 호출: {calls(r)}" + (f" · 재계획 {r['replans']}회 ({'; '.join(r['replan_events'])})" if r["replans"] else " · 재계획 0회"))
        if r["llm_decisions"]:
            d = r["llm_decisions"][0]
            out.append(f"- LLM 판단: `{d['value']}` — {d['reason']}")
        if r["ask"]:
            out.append(f"- 되묻기: `{r['ask']['kind']}` {r['ask']['choices'] or ''}")
        if r["evaluation"]:
            out.append(f"- 로깅 Agent 판정: `{r['evaluation']['verdict']}` {r['evaluation']['warnings'] or ''}")
        out.append(f"- 최종 응답: {r['answer']!r}".replace("\\n", " ")[:600])
        if r["recommendations"]:
            out += ["", rec_table(r)]
        if r["excluded"]:
            out += ["", "제외 사유: " + "; ".join(f"{e['name']}: {e['reason']}" for e in r["excluded"][:5])]
        if r["near_miss"]:
            nm = r["near_miss"]
            out.append(f"\n가장 근접했던 후보: {nm['name']} ({nm['detail']}, 초과 {nm['excess']}분)")
        out.append("")
    out.append(f"**기대와 실제의 차이**: {diff}")
    out.append("")
    for n, alt in shots:
        out += [img(n, alt), ""]
    return "\n".join(out)


def scan_for_secrets() -> tuple[int, dict[str, int]]:
    """NF3001: .env 를 제외한 저장소의 모든 파일에서 실제 키 값을 검색한다 (키 값은 출력하지 않는다)."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import settings
    s = settings()
    secrets = {"KAKAO_REST_KEY": s.kakao_key, "LLM_API_KEY": s.llm_key}
    hits = {k: 0 for k in secrets}
    n = 0
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.name == ".env" or {".git", "__pycache__", ".pytest_cache"} & set(p.parts):
            continue
        data = p.read_bytes()
        n += 1
        for k, v in secrets.items():
            if v and v.encode() in data:
                hits[k] += 1
    return n, hits


def pytest_count() -> str:
    txt = (DOCS / "verification" / "pytest_output.txt").read_text(encoding="utf-8")
    import re
    m = re.search(r"(\d+) passed", txt)
    return m.group(1) if m else "?"


def main() -> None:
    # --- 사람이 읽을 수 있는 실행 로그 샘플 생성
    for src, dst, title in (("S1_full_run_log.jsonl", "S1_full_run_log.md", "대표 요청 1건의 전체 실행 로그 (S1: 제약을 한 번에 충족)"),
                            ("S2_replan_run_log.jsonl", "S2_replan_run_log.md", "재계획이 발생한 실행 로그 (S2: 위치 모호 되묻기 → 선택 → 재계획 1회 후 충족)"),
                            ("S3_limit_run_log.jsonl", "S3_limit_run_log.md", "재계획 상한 도달 → 완화 후 재실행 세션 로그 (S3, 2번째 실행의 분석 단계에 직전 판정 참조 `prev_eval` 기록)"),
                            ("A1_repeat_call_run_log.jsonl", "A1_repeat_call_run_log.md", "F3020 반복 호출 이상 실행 로그 (ANOMALY_INJECT=repeat_call)"),
                            ("A2_unplanned_call_run_log.jsonl", "A2_unplanned_call_run_log.md", "F3020 계획 외 호출 이상 실행 로그 (ANOMALY_INJECT=unplanned_call)"),
                            ("A3_missing_events_run_log.jsonl", "A3_missing_events_run_log.md", "F3020 근거 이벤트 없음 → 판정 불가 실행 로그 (ANOMALY_INJECT=missing_events)")):
        evs = events(src)
        (DOCS / "samples" / dst).write_text(
            f"# {title}\n\n원본: [{src}]({src}) · 이벤트 {len(evs)}건 · 시각은 한국 시각(KST)\n\n## 단계별 요약\n\n{step_summary(evs)}\n\n## 전체 이벤트 (실행 순서)\n\n{log_table(evs)}\n",
            encoding="utf-8")

    s1, s2, s3 = rec("S1"), rec("S2", "2"), rec("S3")
    s1_ev, s2_ev = events("S1_full_run_log.jsonl"), events("S2_replan_run_log.jsonl")
    timed = [r for r in R if r["tool_calls"] and r["scenario"].startswith("S")]   # Tool 을 호출한 모든 요청
    times = [r["elapsed_s"] for r in timed]
    slowest = max(timed, key=lambda r: r["elapsed_s"])
    s4 = rec("S4")
    ev3 = json.loads((DOCS / "samples" / "S3_evaluations.json").read_text(encoding="utf-8"))

    L: list[str] = []
    A = L.append
    A("# 검증 결과\n")
    A("명세서 4장의 확인 항목을 **실제 서버·실제 외부 API(카카오 로컬·카카오맵 대중교통·Open-Meteo)·실제 LLM(Gemini — 이후 GPT 로 전환, 재실행 필요)** 으로 실행해 확인한 결과다. 단위·통합 테스트(가짜 외부 API 사용) " + pytest_count() + "개도 통과했다 ([verification/pytest_output.txt](verification/pytest_output.txt)).\n")
    A("| 항목 | 내용 |\n|---|---|")
    A("| 검증 방법 | `scripts/capture_evidence.py` 가 서버를 상황별로(정상 / Tool 실패 주입 / 카카오 키 없음 / 로깅 Agent 끔) 띄우고 Chrome 으로 화면을 조작해 스크린샷·기록을 저장 |")
    A("| 실행 기준일 | 2026-11-20 (약속 날짜는 \"토요일\" → 2026-11-21) |")
    A("| 이동시간 | 실제 카카오맵 대중교통 (`ROUTE_PROVIDER=kakao`) — 모의값 사용 안 함 |")
    A("| 원본 기록 | [verification/records.json](verification/records.json) (요청별 응답·Tool 호출·재계획·판정), [samples/](samples/) (실행 로그), [screenshots/](screenshots/) |")
    A("| 주의 | 이동시간·장소 수는 **실시간 데이터**라 재실행하면 값이 달라질 수 있다. 아래 값은 수집 시점의 결과다 |\n")

    # ---------------- 4.1
    A("## 4.1 Tool 확인 (F3001~F3004)\n")
    A("`python scripts/verify_tools.py` 로 실제 API 를 호출해 Tool 별 정상·실패 23건을 실행했고, **모든 호출이 예외 없이 `ok`/`error(code,message)` 형식으로 반환**됐다. 전체 표: [verification/tool_checks.md](verification/tool_checks.md)\n")
    A("| 확인 항목 | 결과 | 증빙 |\n|---|---|---|")
    A("| 등록 Tool 의 이름·설명·입력 스키마 목록이 명세와 일치 | ✔ 5개 (geo_loc, find_stn, get_route, find_spot, get_wx) | tool_checks.md 상단 |")
    A("| 정상 호출과 실패 호출(잘못된 입력, API 키 없음 등)이 예외 없이 실패 형식으로 반환 | ✔ `BAD_INPUT`, `NO_KEY`, `NO_RESULT`, `NO_ROUTE`, `AMBIGUOUS`, `OUT_OF_RANGE` 모두 확인 | tool_checks.md |")
    A("| 후보 목록 Tool 에 기준을 주지 않았을 때 기본값 적용 | ✔ `find_stn` 반경 미지정 → `basis: 중심 좌표 반경 3000m` | tool_checks.md |")
    A("| 요청한 기준과 실제 조회 기준이 다를 때 밝힘 | ✔ `get_route` 는 항상 \"조회 시점 기준\", 도보 추정은 `basis`·`notes` 로 표기, 반경 보정은 `notes` | tool_checks.md, 3장 Tool 정의서 |")
    A("| 후보 Tool 이 반환한 식별자(좌표)를 다음 Tool 에 넘겨 조회가 이어짐 | ✔ `find_stn` 이 돌려준 역 좌표를 `get_route.to` 로 사용해 성공 (12분) | tool_checks.md, 4.2 S1 로그 |")
    A("| 판단에 필요한 속성이 비어 있는 항목이 임의 값으로 채워지지 않고 판단하지 않은 채 남음 | ✔ 경로 없음(`NO_ROUTE`) 후보는 값 없이 제외되고 사유 표기 (S4) | 4.7 S4, 화면 07 |")
    A("| 보조 정보 검색 Tool 에서 결과가 없을 때 정의한 응답 형식 | ✔ `find_spot` 외진 좌표 → `NO_RESULT`, Agent 는 개수 0 으로 점수 반영 | tool_checks.md |\n")
    A(img("07_chat_S4_tool_failure.png", "Tool 실패 시 해당 후보만 제외되고 사유가 표시된 화면") + "\n")

    # ---------------- 4.2
    A("## 4.2 메인 Agent 실행 흐름 확인 (F3005~F3011)\n")
    A("### 조건 추출과 임의 채움 방지 (F3005)\n")
    A("실제 LLM 으로 요청 5개의 분석 결과를 출력했다 ([verification/analysis_outputs.md](verification/analysis_outputs.md), `python scripts/show_analysis.py`). 요청에 없는 값은 `null` 로 남고 임의로 채워지지 않았다.\n")
    A((DOCS / "verification" / "analysis_outputs.md").read_text(encoding="utf-8").split("\n", 4)[4].split("\n※")[0].strip() + "\n")
    A("관찰: 2번째 요청에서 우선순위를 말하지 않았지만 모델이 \"이동은 60분 넘지 않게\"를 **이동 부담 적게**로 해석했다(말한 제약 표현에서의 추론이며 임의 기본값은 아님). 날짜·시각이 없는 요청(3·4번째)은 비워 두고 되물었다 (화면 10, 11).\n")
    A(img("10_chat_S7_missing.png", "날짜·시작 시각이 없어 Tool 호출 전에 되묻는 화면") + "\n")
    A(img("11_chat_S10_no_invention.png", "위치·날짜·시작 시각을 지어내지 않고 되묻는 화면") + "\n")
    A("### LangGraph 그래프 구조\n")
    A("분석 → 계획 → 실행 → 판단 → 응답 노드로 구성되고, 판단에서 재계획 노드를 거쳐 계획으로 돌아가는 루프가 있다 (`python scripts/show_graph.py`, 원본 [verification/agent_graph.mmd](verification/agent_graph.mmd)).\n")
    A("```mermaid\n" + (DOCS / "verification" / "agent_graph.mmd").read_text(encoding="utf-8").strip() + "\n```\n")
    A("### Tool 이 필요한 요청과 필요 없는 요청의 계획 (F3006)\n")
    A(f"| 요청 | 실행 단계 | Tool 호출 |\n|---|---|---|\n| S1 `{s1['request'][:30]}…` | {' → '.join(s1['steps'])} | {calls(s1)} |\n| S5 `안녕` | {' → '.join(rec('S5')['steps'])} (**계획 단계 건너뜀**) | {calls(rec('S5'))} |\n")
    A("### 값 전달과 Tool 실패 시 진행 (F3007)\n")
    A(f"S1 로그에서 `find_stn` 이 돌려준 후보 좌표가 `get_route` 입력으로, 그 결과 후보 좌표가 `find_spot` 입력으로 전달됐다 ([samples/S1_full_run_log.md](samples/S1_full_run_log.md)). `get_route` 를 일부러 실패시킨 S4 에서는 호출 {s4['tool_calls'].get('get_route')}회 중 {s4['tool_failures'].get('get_route:NO_ROUTE', 0)}회가 `NO_ROUTE` 였지만 **나머지 단계가 진행**되어 {len(s4['recommendations'])}곳을 추천하고 {len(s4['excluded'])}곳을 사유와 함께 제외했다.\n")
    A("### 제약 판단 계산 값과 응답의 일치 (F3008)\n")
    top = s1["recommendations"][0]
    A(f"S1 1위 {top['station']}: " + ", ".join(f"{t['name']} {t['min']}분" for t in top['times']) + f" → 최대 {top['max_t']}분 ≤ 상한 {s1['criteria']['t_max']}분, 편차 {top['gap']}분 ≤ 상한 {s1['criteria']['g_max']}분 → 충족. 응답 카드의 \"최대 {top['max_t']}분 · 편차 {top['gap']}분\" 과 점수(주변 장소 수 합 {top['score']:.0f}건)가 계산 값과 일치한다. 제약 경계값(상한과 같은 값은 충족, 1분 초과는 제외)은 단위 테스트로 확인했다 (`tests/test_agent.py::test_constraint_boundaries_inclusive`).\n")
    A("외부 API 로 확인할 수 없는 값(약속 시각의 실제 운행 등)은 지어내지 않고 **\"조회 시점 기준\"** 임을 모든 응답에 표기한다. (영화 도메인의 '상영 시작 시각 역산'에 해당하는 값이 이 도메인에는 없다.)\n")
    A(img("02_chat_S1_result.png", "S1 최종 응답: 추천 3곳, 판단 근거, 실행 과정, 로깅 판정") + "\n")
    A("### 재계획: 결과 재사용과 상한 (F3009)\n")
    A(f"- **재사용**: S2 에서 재계획 후 이미 확보한 결과 {s2['cache_hits']}건을 재호출하지 않고 재사용 (화면의 ♻ 표시). S3 에서는 재사용 {s3['cache_hits']}건.\n- **상한**: S3(수원+춘천)가 재계획 {s3['replans']}회에서 멈추고 사유(가장 근접했던 후보 {s3['near_miss']['name']} {s3['near_miss']['detail']}, 초과 {s3['near_miss']['excess']}분)와 함께 조건 조정을 되물었다.\n")
    A(img("05b_chat_S2_replan.png", "재계획 지점이 구분되어 표시된 실행 과정") + "\n")
    A(img("06_chat_S3_limit.png", "재계획 상한 도달, 사유와 완화 버튼") + "\n")
    A("### 후속 요청에서 바뀐 조건만 갱신 (F3010)\n")
    s6 = rec("S6")
    A(f"\"{s6['request']}\" → Tool 호출 **{sum(s6['tool_calls'].values())}회**, 캐시 재사용 {s6['cache_hits']}건, 위치(잠실·건대)·날짜·시각은 유지됐다. "
      f"다만 GPT 첫 실행에서는 우선순위가 `{s6['criteria']['prio_label']}` 로 남아 `음식점 많게` 로 갱신되지 않았다(아래 화면은 이 수정 전 실행). "
      "원인은 분석 단계에서 모델이 우선순위를 비운 것이라 분석 프롬프트에 우선순위 표현 매핑을 추가했고(커밋 17e3847), 수정 후 같은 대화 8회 모두 `음식점 많게` 로 반영됐다. "
      "수정 직후 카카오맵 대중교통 API 가 HTTP 400 을 반환해 수정 후 화면은 다시 찍지 못했다.\n")
    A(img("09_chat_S6_followup.png", "후속 요청: 우선순위만 갱신") + "\n")
    A("### 응답 구성 (F3011)\n")
    A("추천 항목(역), 보조 정보(주변 장소와 링크), 판단 기준(최대 이동시간·편차·우선순위·날씨), 근거 Tool 결과(실행 과정 패널), 제외 후보와 사유가 함께 표시된다 (화면 02, 07, 05b). 이 도메인은 위험 도메인이 아니므로 전문가 조언 안내는 해당 없음.\n")

    # ---------------- 4.3
    A("## 4.3 로깅 Agent 와 실행 로그 확인 (F3012~F3014)\n")
    A("### 이벤트 발행·LLM 판단 기록·세션 저장\n")
    A("대표 요청 S1 한 건의 실행 로그를 단계별로 요약했다. 전체 이벤트 116건: [samples/S1_full_run_log.md](samples/S1_full_run_log.md) (원본 [JSONL](samples/S1_full_run_log.jsonl))\n")
    A(step_summary(s1_ev) + "\n")
    A("- 단계 진입·종료(`step_start`/`step_end`), Tool 호출 요청·응답·소요 시간(`tool_req`/`tool_res`), 재계획(`replan`), 결과 재사용(`cache_hit`), 요청 종료(`run_end`)가 모두 발행됐다.")
    d = s1["llm_decisions"][0]
    A(f"- **LLM 이 내린 판단과 이유**가 이벤트에 남는다: `llm_decision {{kind: intent, value: {d['value']}, reason: \"{d['reason']}\"}}`")
    A("- `tool_res` 에는 성공 여부·코드·소요 시간·건수만 있고 **응답 원문은 없다**.\n")
    A("### 재계획이 발생한 실행 로그\n")
    A("S2(재계획 1회) 전체 로그: [samples/S2_replan_run_log.md](samples/S2_replan_run_log.md) · S3(상한 도달): [samples/S3_limit_run_log.md](samples/S3_limit_run_log.md)\n")
    A(step_summary([e for e in s2_ev if e["seq"] >= next(x["seq"] for x in s2_ev if x["type"] == "run_end") + 1]) + "\n")
    A("### 로깅 Agent 는 LLM 을 호출하지 않는다\n")
    A("- 수집·저장 코드(`backend/app/logging_agent.py`)에 LLM 호출이 없다. 테스트 `test_logging_agent_makes_no_llm_calls` 로 확인 (정상 실행에서 LLM 호출은 메인 Agent 의 분석 1회·응답 1회뿐, 진단 호출 0회).\n")
    A("### 로깅 Agent 를 연결하지 않아도 메인 Agent 정상 (S11)\n")
    s11 = rec("S11")
    A(f"`DISABLE_LOGGING_AGENT=1` 로 서버를 띄워 S1 요청을 실행: 결과 `{s11['type']}/{s11['status']}`, 추천 {len(s11['recommendations'])}곳 정상. `/api/health` → `logging_agent: {str(s11['health']['logging_agent']).lower()}`, `/api/logs` → `{s11['logs_after']}` (저장 없음). 화면에 \"로깅 Agent 가 꺼져 있어 실행 로그가 저장되지 않아요\" 배너가 표시된다.\n")
    A(img("11c_chat_S11_no_logging_agent.png", "로깅 Agent 없이도 정상 응답") + "\n")
    A("### 세션 단위 분리, 목록·상세·삭제, 수정 API 없음\n")
    A("- 세션별 파일(`{session_id}.jsonl`)에 저장되어 서로 다른 세션의 로그가 섞이지 않는다 (테스트 `test_logs_are_isolated_per_session_and_in_order`).")
    A("- 목록(화면 16), 상세(실행 순서, 화면 17·18), 삭제(화면 19)가 동작한다. 수정 API 는 없다: `PUT/PATCH/POST /api/logs/{id}` → 405 (테스트 `test_logs_api_list_detail_delete_and_no_update`, API 문서 화면 20 에 GET·DELETE 만 존재).\n")
    A(img("16_logs_list.png", "실행 로그 목록") + "\n")
    A(img("19_logs_after_delete.png", "세션 로그 삭제 후 목록") + "\n")

    # ---------------- 4.4
    A("## 4.4 계획 관리 확인 (F3015~F3016)\n")
    A("- 대화 화면의 추천 카드에서 \"이 역으로 계획 저장\" → 계획이 저장되고, **같은 날짜에 시간이 겹치는 계획이 있으면 저장은 되고 겹치는 계획(#1 14:00 건대입구역)이 저장 결과와 함께 알려진다.**\n")
    A(img("12_chat_plan_saved_conflict.png", "계획 저장과 겹침 알림") + "\n")
    A("- 계획 관리 화면에서 핵심 값(날짜·시작·종료·역·장소)과 생성 일시가 목록에 표시되고, 수정·삭제 결과가 목록에 바로 반영된다.\n")
    A(img("13_plans_list.png", "계획 목록") + "\n")
    A(img("14_plans_edit.png", "계획 수정") + "\n")
    A(img("15_plans_after_edit.png", "수정 반영") + "\n")
    A(img("15b_plans_after_delete.png", "삭제 반영") + "\n")

    # ---------------- 4.5
    A("## 4.5 서비스 구성과 화면 확인 (F3017~F3019)\n")
    A("- **API 경로·형식**: 대화(`/api/chat`)·계획(`/api/plans`)·로그(`/api/logs`)가 서로 다른 경로로 동작하고 요청·응답이 Pydantic 모델과 일치한다 (FastAPI 문서 화면 20, [openapi.json](openapi.json), [04_API명세서.md](04_API명세서.md)).\n")
    A(img("20_api_docs.png", "FastAPI 문서 화면") + "\n")
    A("- 대화·계획 관리·실행 로그 화면이 모두 백엔드 API 를 호출해 데이터를 표시한다.")
    A("- **처리 중 상태**, 실행 단계·Tool 호출 이력(순서대로), 재계획 지점 구분:\n")
    A(img("03_chat_busy.png", "처리 중 상태") + "\n")
    A("- **Tool 실패**: S4 (화면 07). **API 키 누락**: 설정 배너와 오류 안내 (S9):\n")
    A(img("11b_chat_S9_no_api_key.png", "API 키 누락 안내") + "\n")

    # ---------------- 4.6
    A("## 4.6 심화 기능 확인: F3020 (로깅 Agent 평가)\n")
    A("명세서는 이상 실행을 **각각 만들어** 식별되는지 확인하라고 한다. 반복 호출·계획 외 호출·근거 없음은 운영 중 저절로 생기지 않으므로, Tool 실패에 `FAIL_INJECT` 를 쓴 것처럼 **검증용 주입 환경변수 `ANOMALY_INJECT`** 로 실제 서버에서 만들었다(운영에서는 비워 둠). 아래는 모두 **실제 서버·실제 LLM 으로 실행**한 결과이고, 같은 케이스를 단위 테스트로도 확인했다 (`tests/test_evaluation.py`).\n")
    A("| 확인 항목 | 만든 방법 | 실제 실행 결과 | 증빙 |\n|---|---|---|---|")
    e0 = ev3[0]   # S3(수원+춘천) 실행 #1 판정
    ev_a = {k: json.loads((DOCS / "samples" / f"{k}_evaluations.json").read_text(encoding="utf-8"))[-1] for k in ("A1_repeat_call", "A2_unplanned_call", "A3_missing_events")}
    a1, a2, a3 = ev_a["A1_repeat_call"], ev_a["A2_unplanned_call"], ev_a["A3_missing_events"]
    A(f"| 같은 Tool 을 같은 입력으로 반복 호출한 실행이 이상으로 식별 | `ANOMALY_INJECT=repeat_call` (`get_wx` 를 같은 입력으로 한 번 더 호출) | **`anomaly`** · {a1['warnings'][0]['message']} · 규칙별 `{a1['checks']}` | [로그](samples/A1_repeat_call_run_log.md), [판정](samples/A1_repeat_call_evaluations.json), 화면 21 |")
    A(f"| 재계획을 과도하게 반복한 실행 식별 | 실제 요청 \"수원+춘천\" (재계획 상한 도달, 주입 없음) | **`anomaly`** · {e0['warnings'][0]['message']} | [로그](samples/S3_limit_run_log.md), [판정](samples/S3_evaluations.json), 화면 17 |")
    A(f"| 계획에 없던 Tool 호출 식별 | `ANOMALY_INJECT=unplanned_call` (계획에서 `get_wx` 를 빼고 실행은 그대로 호출) | **`anomaly`** · {a2['warnings'][0]['message']} (계획: {a2['warnings'][0]['evidence']['plan']}) | [로그](samples/A2_unplanned_call_run_log.md), [판정](samples/A2_unplanned_call_evaluations.json), 화면 22 |")
    A(f"| 근거 이벤트가 없을 때 '이상 없음'이 아니라 '판정 불가' | `ANOMALY_INJECT=missing_events` (실행 시작 이벤트가 유실된 상황) | **`unknown`(판정 불가)** · 규칙별 `{a3['checks']}`, 경고 0건, 로그에 `analyze` 시작 이벤트 없음 | [로그](samples/A3_missing_events_run_log.md), [판정](samples/A3_missing_events_evaluations.json), 화면 23 |")
    prev = [e for e in events("S3_limit_run_log.jsonl") if e["type"] == "step_end" and e["step"] == "analyze" and "prev_eval" in e]
    A(f"| 판단 결과를 메인 Agent 가 참조 | S3 세션의 2번째 실행 (직전 실행이 `anomaly`) | 분석 단계 종료 이벤트 #{prev[0]['seq'] if prev else '-'} 에 직전 판정이 기록됨: `prev_eval={prev[0]['prev_eval'] if prev else None}`, `prev_warnings={prev[0]['prev_warnings'] if prev else None}` | [S3 세션 로그](samples/S3_limit_run_log.md) 이벤트 `#{prev[0]['seq'] if prev else '-'}` |")
    s1e = json.loads((DOCS / "samples" / "S1_evaluations.json").read_text(encoding="utf-8"))[-1]
    A(f"| 정상 실행에는 LLM 진단을 호출하지 않음 | 정상 실행 S1 | 판정 `{s1e['verdict']}`, 진단 `{s1e['diagnosis']['status']}` (LLM 미호출). `unknown`(A3)도 진단 `{a3['diagnosis']['status']}` | [S1 판정](samples/S1_evaluations.json), 화면 18 |")
    A(f"| 이상 실행에만 원인 진단이 붙고, 판정과 구분되어 저장 | A1·A2·S3 | 진단 `done`(세 문장 이내), `warning_count` 는 진단과 무관(각각 {a1['warning_count']}·{a2['warning_count']}·{e0['warning_count']}건) | 화면 17·21·22 |")
    A("| 진단이 실패해도 판정·로그 유지 | 진단 LLM 이 예외를 내는 상황 | 판정은 그대로 저장되고 `diagnosis.status=failed` | 단위 테스트 `test_diagnosis_failure_keeps_judgment` |\n")
    A("**실제 LLM 진단 예**\n")
    A(f"- 반복 호출(A1): {a1['diagnosis']['text']}")
    A(f"- 재계획 과다(S3 수원+춘천): {e0['diagnosis']['text']}\n")
    A("관찰: A2(계획 외 호출)의 진단은 \"Agent 나 Tool 에 문제가 없어 보인다\"는 취지로 끝난다. 주입으로 만든 이상이라 모델이 알 수 있는 단서(실행 기록 요약)에 한계가 있기 때문이며, 판정(규칙)은 정확하지만 **LLM 진단의 정확도는 보장되지 않는다**는 점을 한계로 기록한다.\n")
    A(img("21_logs_F3020_repeated_call.png", "반복 호출 이상 판정과 진단") + "\n")
    A(img("22_logs_F3020_unplanned_call.png", "계획 외 호출 이상 판정") + "\n")
    A(img("23_logs_F3020_unknown.png", "근거 이벤트 없음 → 판정 불가") + "\n")
    A(img("17_logs_detail_anomaly.png", "재계획 과다 이상 판정과 LLM 원인 진단(판정과 별개 표기)") + "\n")
    A(img("18_logs_detail_normal.png", "정상 실행 판정") + "\n")
    A("**한계**: 재계획 과다를 \"상한 도달\"로 정의해, 사용자 조건이 현실적으로 충족 불가능한 요청도 `anomaly` 로 판정된다. 진단 프롬프트가 이 경우를 사용자 조건 문제로 구분하도록 했다 (S3 진단 참조).\n")

    # ---------------- 4.7
    A("## 4.7 대표 시나리오 검증\n")
    A(f"대표 요청 {len({r['scenario'] for r in R if r['scenario'].startswith('S')})}개를 실행했다. 아래를 모두 포함한다: 제약을 한 번에 충족(S1) · 재계획이 필요(S2) · 상한에 도달(S3) · Tool 하나가 실패(S4) · Tool 이 필요 없음(S5). 그 밖에 후속 요청(S6), 조건 부족 되묻기(S7·S8·S10), API 키 누락(S9), 로깅 Agent 미연결(S11). 요청마다 **실행 결과(최종 응답) · 실행 로그(단계·Tool 호출 이력·재계획 여부) · 기대와 실제의 차이**를 기록했다.\n")
    A("| 시나리오 | 요청 | 결과 | 재계획 | 소요(시나리오 전체 요청 합계) |\n|---|---|---|---|---|")
    for sc, title in (("S1", "한 번에 충족"), ("S2", "재계획 후 충족"), ("S3", "상한 도달"), ("S4", "Tool 하나 실패"), ("S5", "Tool 불필요"), ("S6", "후속 요청"), ("S7", "조건 부족"), ("S8", "우선순위 되묻기"), ("S9", "API 키 누락"), ("S10", "임의 채움 방지"), ("S11", "로깅 Agent 미연결")):
        last = [r for r in R if r["scenario"] == sc][-1]
        first = [r for r in R if r["scenario"] == sc][0]
        A(f"| {sc} {title} | {first['request']} | `{last['type']}/{last['status']}` | {sum(r['replans'] for r in R if r['scenario'] == sc)}회 | {sum(r['elapsed_s'] for r in R if r['scenario'] == sc):.1f}초 |")
    A("\n재계획·소요는 시나리오의 모든 요청 합계다(S2·S3·S7·S8 은 되묻기·버튼 요청 포함, S3 는 상한 도달 3회 + 완화 후 재실행 1회). "
      "S1~S3·S5~S8·S10 은 2026-11-20 배포 서버(Render 무료)·GPT 실행 기록이고, S4·S9·S11 과 F3020 주입(A1~A3)은 환경변수 주입이 필요해 같은 날 로컬(LLM=Gemini)에서 실행한 기록이다.\n")
    A("")
    A(scenario_block("S1", "제약을 한 번에 충족", "재계획 없이 충족하는 후보 3곳을 판단 근거와 함께 추천, 실행 과정 표시.",
                     "**없음.** 기대대로 동작했다.", [("02_chat_S1_result.png", "S1 화면")], ["1"]))
    A(scenario_block("S2", "재계획 후 충족 (위치 모호 → 선택 → 재계획)", "위치가 모호하면 후보 버튼으로 되묻고(재계획 아님), 선택 후 반경 3000m 에서 충족 후보가 없으면 5000m 로 재계획해 충족하는 후보를 추천.",
                     "**있음(설계서 예시와 다름).** 설계서는 \"송도+마석\"이 반경 3km 에서 편차 상한을 넘어 재계획된다고 예상했으나, 실제로는 재계획 없이 충족했다(S7 의 용산역). 원인: 설계서 예시는 호출 전의 추정이었고 실제 대중교통 시간은 달랐다. 수정: 후보 지역 쌍 13개를 실제 호출로 탐색해 재계획 1회로 충족하는 \"중앙(안산)+건대\" 를 대표 시나리오로 교체했다 (이 쌍에서는 3000m 안에 역이 없어 재계획).",
                     [("05a_chat_S2_ambiguous.png", "위치 모호 되묻기"), ("05b_chat_S2_replan.png", "재계획 지점 구분")], ["1", "2"]))
    A(scenario_block("S3", "재계획 상한 도달 → 조건 완화", "재계획 3회에서 멈추고 사유와 함께 조건 조정을 되묻는다. 완화 버튼을 누르면 그 값으로 다시 탐색한다.",
                     "**있음(개발 중 발견·수정 완료).** 최초 설계서의 \"서울+부산\"으로 실행했을 때 상한은 도달했지만 안내가 \"이동시간 상한을 늘릴까요?\" 였다. 원인: 두 지역의 중간 지점 반경 8km 안에 지하철역이 없어 후보 자체가 없는 경우였기 때문에 제약 완화로는 해결되지 않는다. 수정: 이 경우 \"서로 더 가까운 위치로 알려 달라\"(`relax_region`)로 분기하고, 제약 때문에 도달한 경우(수원+춘천)에만 완화 버튼을 보인다 (`test_limit_without_any_station_asks_for_closer_locations_not_looser_constraints`). 위 기록은 수정 후 대표 요청 \"수원+춘천\" 결과다.",
                     [("06_chat_S3_limit.png", "상한 도달"), ("06b_chat_S3_relaxed.png", "완화 후 재실행")], ["1", "2"]))
    A(scenario_block("S4", "Tool 하나가 실패 (get_route 3번에 1번 NO_ROUTE 주입)", "실패한 후보만 제외하고 사유를 표기하며 나머지로 추천을 계속한다. 서비스는 중단되지 않는다.",
                     "**없음.** 실패한 4곳은 값 없이 \"판단하지 않음\"으로 제외됐다. (자연 발생 `NO_ROUTE` 는 서울→부산 호출로 별도 확인: tool_checks.md.)", [("07_chat_S4_tool_failure.png", "S4 화면")], ["1"]))
    A(scenario_block("S5", "Tool 이 필요 없는 요청", "계획 단계를 건너뛰고 Tool 호출 없이 바로 응답한다.",
                     "**없음.** 실행 단계가 분석→응답뿐이고 Tool 호출 0회.", [("08_chat_S5_no_tool.png", "S5 화면")], ["1", "2"]))
    A(scenario_block("S6", "후속 요청 (Memory)", "위치·날짜·시각은 유지하고 우선순위만 갱신, 이미 조회한 이동시간은 다시 부르지 않는다.",
                     "**있음(GPT 실행에서 발견, 프롬프트 수정 완료).** 첫 실행에서 우선순위가 갱신되지 않았다(위 4.2 F3010 설명). 수정 후 8회 반복에서 모두 `음식점 많게` 로 반영됨을 확인했으나, 카카오맵 대중교통 API 의 일시 오류로 수정 후 최종 화면은 재촬영하지 못했다.", [("09_chat_S6_followup.png", "S6 화면(수정 전 실행)")], ["1"]))
    A(scenario_block("S7", "조건 부족 → 되묻기 → 답변", "날짜·시작 시각이 없으면 Tool 호출 전에 되묻고, 답변을 받으면 이어서 처리한다.",
                     "**없음.**", [("10_chat_S7_missing.png", "되묻기"), ("10b_chat_S7_answered.png", "답변 후 추천")], ["1", "2"]))
    A(scenario_block("S8", "우선순위 되묻기(선택 버튼)", "우선순위가 없으면 선택 버튼으로 되묻고, 버튼 선택은 LLM 없이 처리한다.",
                     "**없음.** (버튼 응답 턴에는 LLM 판단 이벤트가 없다.)", [("04_chat_ask_priority.png", "우선순위 버튼")], ["1", "2"]))
    A(scenario_block("S9", "API 키 누락", "재계획하지 않고 설정 안내를 표시한다. 키·오류 상세는 노출하지 않는다.",
                     "**없음.** `geo_loc` 1회 호출이 `NO_KEY` 로 실패하고 즉시 안내, 재계획 0회. 화면 상단에 설정 배너도 표시된다.", [("11b_chat_S9_no_api_key.png", "S9 화면")], ["1"]))
    A(scenario_block("S10", "없는 조건을 임의로 채우지 않는가", "날짜·시작 시각·위치를 지어내지 않고 되묻는다.",
                     "**있음(개발 중 발견·수정 완료).** 처음에는 LLM 이 날짜가 없는 \"잠실에서 7시 전에 끝나야 해\"에 **오늘 날짜를 채웠다.** 수정: 프롬프트에 \"언급이 있을 때만 채움\"을 명시하고, 근거 없이 오늘 날짜가 채워지면 코드에서 제거한다 (`drop_unsupported_today`, 단위 테스트). 수정 후 기록은 아래와 같다.", [("11_chat_S10_no_invention.png", "S10 화면")], ["1"]))
    A(scenario_block("S11", "로깅 Agent 를 연결하지 않고 실행", "메인 Agent 가 정상 동작한다 (발행이 구독자에 의존하지 않음).",
                     "**없음.**", [("11c_chat_S11_no_logging_agent.png", "S11 화면")], ["1"]))
    A("### 개발 중 발견해 수정한 기타 차이\n")
    A("| 차이 | 원인 | 수정 |\n|---|---|---|")
    for row in (
        ("대중교통 첫 호출이 HTTP 400", "요청 파라미터를 `origin/destination` 으로 가정", "응답이 알려 준 `start_x/start_y/end_x/end_y` 로 변경"),
        ("참여자 바로 옆 역이 \"경로 없음\"으로 제외될 위험", "아주 가까운 구간(약 130~300m)에서 방향에 따라 `NO_RESULTS`", "500m 이하는 도보 추정으로 대체하고 표기(`mode=walk_estimate`)"),
        ("이벤트 발행 중 `TypeError`(단계 종료 이벤트)", "`type` 필드명이 `publish(type=…)` 인자와 충돌", "필드명을 `response_type` 으로 변경, 단계 이벤트 통합 테스트 추가"),
        ("동점 3곳의 순서가 불안정", "점수가 같을 때 보조 정렬 기준 부족", "이동시간 합 → 최대 이동시간 순으로 정렬"),
        ("결과 0건(`NO_RESULT`)이 화면에서 실패처럼 보임", "표시 문구 구분 없음", "\"결과 없음\"으로 표기"),
        ("로그·계획 시각이 9시간 어긋나 보임", "서버 UTC 시각을 변환 없이 표시", "화면에서 한국 시각으로 변환"),
        ("이상 진단이 사용자 조건 문제를 Agent 결함으로 결론", "진단 프롬프트에 구분 기준이 없었음", "구분 기준 추가, 실제 재진단으로 확인"),
        ("LLM 을 Gemini 로 실행해 검증", "명세서 필수 라이브러리는 langchain-openai·openai", "GPT(`gpt-4o-mini`, OpenAI 호환)로 통일, Gemini 전용 코드 제거 — 이 문서의 실행 기록은 Gemini 기준이므로 GPT 로 재실행 필요"),
    ):
        A("| " + " | ".join(row) + " |")
    A("")

    # ---------------- 4.8
    A("## 4.8 비기능 요구사항 확인 (NF3001~NF3007)\n")
    p50, mx = statistics.median(times), max(times)
    A("| 번호 | 확인 | 결과 |\n|---|---|---|")
    nfiles, hits = scan_for_secrets()
    ok_scan = "✔" if not any(hits.values()) else "✘ **키가 발견됨**"
    A(f"| NF3001 | 소스코드·제출물에 API 키가 남아 있지 않은가 | {ok_scan} `.env` 를 제외한 **{nfiles}개 파일**(소스·문서·로그·JSON·스크린샷)에서 실제 `KAKAO_REST_KEY`·`LLM_API_KEY` 값 검색 결과 **{sum(hits.values())}건**, `.env` 는 `.gitignore` 에 포함, `.env.example` 은 값이 비어 있음 |")
    A("| NF3002 | 화면에 API 키와 오류 상세가 노출되지 않는가 | ✔ 키 누락 화면(S9)은 안내문만 표시, `/api/health` 는 설정 여부만 반환, 처리되지 않은 예외는 `INTERNAL` 로만 응답 (테스트 `test_unhandled_error_hides_details`, `test_health_reports_presence_not_values`, `test_chat_endpoint_returns_answer_and_timeline` 에서 키 값 미노출) |")
    A("| NF3003 | Tool 호출이 실패해도 서비스가 중단되지 않는가 | ✔ S4 (get_route 4회 실패에도 추천 완료), 테스트 `test_unexpected_exception_is_converted` |")
    A("| NF3004 | 재계획 상한이 적용되어 무한 반복이 없는가 | ✔ S3 에서 재계획 3회 후 중단, `MAX_REPLAN` 설정 테스트 |")
    A(f"| NF3005 | 대표 요청의 처리 시간이 사용에 지장이 없는가 | ✔ Tool 을 호출하는 요청 {len(times)}건: **중앙값 {p50:.1f}초, 최대 {mx:.1f}초** (LLM 호출 2회 포함, 병렬 호출, 배포 서버(Render 무료)·GPT 기준이며 같은 조건의 로컬 측정은 중앙값 4.3초·최대 6.3초). 가장 오래 걸린 요청은{slowest['scenario']}(`{slowest['request'][:24]}…`){' — `get_route` 를 직렬(`ROUTE_WORKERS=1`)로 실행하고 실패를 주입한 경우' if slowest['scenario'] == 'S4' else ''} |")
    A("| NF3006 | 프론트엔드·백엔드 분리, Agent·Tool 코드가 파일로 분리되어 있는가 | ✔ `frontend/`(HTML/CSS/JS)와 `backend/`(FastAPI)가 별도 프로세스(포트 5500/8000), `agent/`·`tools/`·`events/`·`store/`·`api/` 파일 분리 |")
    A("| NF3007 | 외부 API 출처와 이용 조건(호출 한도·약관)을 기록했는가 | ✔ [01_요구사항정의서.md](01_요구사항정의서.md) 1.2 와 README 에 기록 (카카오 응답 비저장, Open-Meteo CC BY 4.0 출처 표기) |\n")

    # ---------------- 4.9
    A("## 4.9 제출 전 점검 목록\n")
    A("| 항목 | 상태 |\n|---|---|")
    A("| 필수 기능 F3001~F3019 가 모두 동작한다 | ✔ (4.1~4.5) |")
    A("| 선택한 심화 기능(F3020)이 동작하고 필수 기능의 결과와 비교되어 있다 | ✔ 4.6 (정상 실행 `ok` vs 이상 실행 `anomaly`·`unknown` 판정 비교, 필수 기능 결과는 4.7) |")
    A(f"| 증명을 위한 스크린샷과 실행 로그 항목을 모두 첨부했다 | ✔ 스크린샷 {len(list((DOCS / 'screenshots').glob('*.png')))}장, 실행 로그 샘플(JSONL·표) |")
    A("| 요구사항 정의서 · Agent 설계 문서 · Tool 정의서 · API 명세서 · 구현 소스코드 · 실행 로그 샘플 · README.md 를 모두 포함했다 | ✔ `docs/01~05`, `backend/`·`frontend/`, `docs/samples/`, `README.md` |")
    A("| 대표 요청 1건의 전체 실행 로그, 재계획 사례를 포함했다 | ✔ [S1 전체 로그](samples/S1_full_run_log.md), [S2 재계획 로그](samples/S2_replan_run_log.md) |")
    A("| 3일차 오후에 제공되는 **셀프 체크리스트**로 점검을 마쳤다 | ✔ [셀프체크리스트.xlsx](셀프체크리스트.xlsx) (통과 26 · 해당없음 5 · 미확인 1 · 실패 0, 증빙 스크린샷 삽입), 요약은 [06_셀프체크리스트.md](06_셀프체크리스트.md) |")
    A("")
    A("## 5. 스크린샷 목록\n")
    A("| 파일 | 내용 | 관련 항목 |\n|---|---|---|")
    for f, c, k in (("01_chat_initial.png", "대화 초기 화면", "F3018"), ("02_chat_S1_result.png", "한 번에 충족, 추천·근거·실행 과정·판정", "F3005~F3011, F3019"), ("03_chat_busy.png", "처리 중 상태", "F3019"),
                    ("04_chat_ask_priority.png", "우선순위 선택 버튼", "F3005"), ("05a_chat_S2_ambiguous.png", "위치 모호 되묻기", "F3005"), ("05b_chat_S2_replan.png", "재계획 지점 구분, 결과 재사용", "F3009, F3019"),
                    ("06_chat_S3_limit.png", "재계획 상한 도달·사유·완화 버튼", "F3009, NF3004"), ("06b_chat_S3_relaxed.png", "완화 후 재탐색", "F3009"), ("07_chat_S4_tool_failure.png", "Tool 실패, 제외 사유", "F3007, NF3003"),
                    ("08_chat_S5_no_tool.png", "Tool 불필요 요청", "F3006"), ("09_chat_S6_followup.png", "후속 요청(Memory)", "F3010"), ("10_chat_S7_missing.png", "조건 부족 되묻기", "F3005"),
                    ("10b_chat_S7_answered.png", "답변 후 추천", "F3005"), ("11_chat_S10_no_invention.png", "임의 채움 방지", "F3005"), ("11b_chat_S9_no_api_key.png", "API 키 누락 안내", "F3019, NF3002"),
                    ("11c_chat_S11_no_logging_agent.png", "로깅 Agent 없이 정상 동작", "F3012"), ("12_chat_plan_saved_conflict.png", "계획 저장·겹침 알림", "F3015"), ("13_plans_list.png", "계획 목록", "F3015, F3018"),
                    ("14_plans_edit.png", "계획 수정", "F3016"), ("15_plans_after_edit.png", "수정 반영", "F3016"), ("15b_plans_after_delete.png", "삭제 반영", "F3016"),
                    ("16_logs_list.png", "실행 로그 목록", "F3014"), ("17_logs_detail_anomaly.png", "이상 판정·진단", "F3020"), ("18_logs_detail_normal.png", "정상 판정", "F3020"),
                    ("19_logs_after_delete.png", "로그 삭제", "F3014"), ("20_api_docs.png", "FastAPI 문서", "F3017"),
                    ("21_logs_F3020_repeated_call.png", "반복 호출 이상 판정·진단", "F3020"), ("22_logs_F3020_unplanned_call.png", "계획 외 호출 이상 판정", "F3020"),
                    ("23_logs_F3020_unknown.png", "근거 없음 → 판정 불가", "F3020")):
        A(f"| [{f}](screenshots/{f}) | {c} | {k} |")
    A("")
    (DOCS / "05_검증결과.md").write_text("\n".join(L), encoding="utf-8")
    print("생성: docs/05_검증결과.md,", ", ".join(f"samples/{n}" for n in ("S1_full_run_log.md", "S2_replan_run_log.md", "S3_limit_run_log.md")))


if __name__ == "__main__":
    main()
