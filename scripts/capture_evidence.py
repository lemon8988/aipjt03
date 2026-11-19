"""제출용 증거 수집 (개발 도구): 실제 서버·실제 외부 API·실제 LLM 으로 대표 시나리오를 브라우저에서 실행하고
스크린샷(docs/screenshots), 시나리오 기록(docs/verification/records.json), 실행 로그 샘플(docs/samples)을 저장한다.

필요: pip install playwright (설치된 Chrome 을 사용), .env 의 KAKAO_REST_KEY / LLM_API_KEY, 포트 8000·5500 비어 있을 것
실행: python scripts/capture_evidence.py
서버는 상황별로 새로 띄운다(정상 / Tool 실패 주입 / 카카오 키 없음 / 로깅 Agent 끔). 데이터는 임시 폴더를 쓰므로 저장소 data/ 를 건드리지 않는다.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SHOTS, SAMPLES, VERIF = DOCS / "screenshots", DOCS / "samples", DOCS / "verification"
API, WEB = "http://localhost:8000", "http://localhost:5500"
EXPAND = "#messages{max-height:none !important;overflow:visible !important}.trace{max-height:none !important;position:static !important}"

RECORDS: list[dict] = []
SESSIONS: dict[str, str] = {}


# ------------------------------------------------------------------ 서버
def _port_free(port: int) -> bool:
    try:
        requests.get(f"http://localhost:{port}/", timeout=1)
        return False
    except requests.RequestException:
        return True


@contextlib.contextmanager
def backend(**env_over):
    if not _port_free(8000):
        raise SystemExit("포트 8000 을 쓰는 프로세스가 있습니다. 종료한 뒤 다시 실행하세요.")
    data = tempfile.mkdtemp(prefix="evidence_")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "ROUTE_PROVIDER": "kakao", "DATA_DIR": data, **env_over}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8000"],
                         cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                requests.get(API + "/api/health", timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.5)
        else:
            raise SystemExit("백엔드가 시작되지 않았습니다.")
        yield data
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
        time.sleep(1)


# ------------------------------------------------------------------ 브라우저 헬퍼
def shot(page, name: str, expand: bool = True, full: bool = True) -> str:
    """expand: 채팅 영역의 높이 제한을 풀고 접힌 '제외된 후보' 목록을 펼쳐 찍는다. full=False 는 현재 화면 영역만."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    h = page.add_style_tag(content=EXPAND) if expand else None
    if expand:
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    if not full:
        page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(250)
    page.screenshot(path=str(SHOTS / name), full_page=full)
    if h:
        page.evaluate("el => el.remove()", h)
    print("  📷", name)
    return name


def fresh_chat(page):
    page.goto(WEB + "/index.html")
    page.evaluate("sessionStorage.clear()")
    page.reload()
    page.wait_for_selector("#input")
    page.wait_for_timeout(400)  # 상태 배너(/api/health) 렌더 대기


def session_id(page) -> str:
    return page.evaluate("sessionStorage.getItem('session_id')")


def ask(page, text: str | None = None, choice: str | None = None, busy_shot: str | None = None) -> tuple[dict, float]:
    t0 = time.time()
    with page.expect_response(lambda r: r.url.endswith("/api/chat") and r.request.method == "POST", timeout=180000) as info:
        if text is not None:
            page.fill("#input", text)
            page.press("#input", "Enter")
        else:
            page.get_by_role("button", name=choice).first.click()
        if busy_shot:
            page.wait_for_selector(".typing")
            shot(page, busy_shot, expand=False)
    data = info.value.json()
    page.wait_for_selector(".typing", state="detached", timeout=30000)
    page.wait_for_function("!document.getElementById('send').disabled")
    return data, time.time() - t0


def record(sid_key: str, scenario: str, turn: str, request: str, data: dict, elapsed: float, shots: list[str]) -> dict:
    tl = data.get("timeline", [])
    calls: dict[str, int] = {}
    fails: dict[str, int] = {}
    for e in tl:
        if e["type"] == "tool_req":
            calls[e["tool"]] = calls.get(e["tool"], 0) + 1
        if e["type"] == "tool_res" and not e["ok"]:
            k = f"{e['tool']}:{e['code']}"
            fails[k] = fails.get(k, 0) + 1
    res = data.get("result") or {}
    rec = {
        "scenario": scenario, "turn": turn, "request": request, "elapsed_s": round(elapsed, 1),
        "type": data["type"], "status": data["status"], "error_code": data.get("error_code"),
        "answer": data.get("answer"),
        "ask": ({"kind": data["ask"]["kind"], "choices": [c["label"] for c in data["ask"]["choices"]]} if data.get("ask") else None),
        "recommendations": [{"rank": r["rank"], "station": r["station"],
                             "times": [{"name": t["name"], "min": t["min"], "transfers": t["transfers"], "walk": t["walk"]} for t in r["times"]],
                             "max_t": r["max_t"], "gap": r["gap"], "score": r["score"],
                             "spot_counts": {c: v["total"] for c, v in r["spots"].items()}} for r in res.get("recommendations", [])],
        "excluded": res.get("excluded", []), "criteria": res.get("criteria"), "weather": res.get("weather"),
        "notes": res.get("notes", []), "replans": res.get("replans", 0), "near_miss": res.get("near_miss"),
        "tool_calls": calls, "tool_failures": fails,
        "cache_hits": sum(1 for e in tl if e["type"] == "cache_hit"),
        "replan_events": [e["change"] for e in tl if e["type"] == "replan"],
        "steps": [e["step"] for e in tl if e["type"] == "step_start"],
        "llm_decisions": [{"kind": e["kind"], "value": e["value"], "reason": e["reason"]} for e in tl if e["type"] == "llm_decision"],
        "evaluation": ({"verdict": data["evaluation"]["verdict"], "warnings": [w["message"] for w in data["evaluation"]["warnings"]]}
                       if data.get("evaluation") else None),
        "session_id": data["session_id"], "screenshots": shots,
    }
    RECORDS.append(rec)
    SESSIONS[sid_key] = data["session_id"]
    print(f"  [{scenario}/{turn}] {rec['type']}/{rec['status']} {elapsed:.1f}s 재계획={rec['replans']} 호출={calls}")
    return rec


def save_log(sid_key: str, filename: str):
    sid = SESSIONS[sid_key]
    SAMPLES.mkdir(parents=True, exist_ok=True)
    events = requests.get(f"{API}/api/logs/{sid}", timeout=10).json()["events"]
    (SAMPLES / filename).write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    return len(events)


# ------------------------------------------------------------------ 시나리오
S1 = "잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트, 할 거 많은 곳으로"


def phase_main(pw):
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_context(viewport={"width": 1360, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul").new_page()

    print("\n[S1] 제약을 한 번에 충족 + 처리 중 상태")
    fresh_chat(page)
    page.goto(WEB + "/index.html"); page.wait_for_selector("#input")
    shot(page, "01_chat_initial.png", expand=False)
    d, t = ask(page, S1, busy_shot="03_chat_busy.png")
    record("S1", "S1", "1", S1, d, t, [shot(page, "02_chat_S1_result.png")])
    n = save_log("S1", "S1_full_run_log.jsonl")
    print("  로그 이벤트", n)
    (SAMPLES / "S1_evaluations.json").write_text(json.dumps(
        requests.get(f"{API}/api/logs/{SESSIONS['S1']}/evaluations", timeout=10).json()["evaluations"], ensure_ascii=False, indent=2), encoding="utf-8")

    print("[S6] 후속 요청(Memory) + 계획 저장·겹침")
    r = "같은 조건에서 음식점 많은 쪽으로"
    d, t = ask(page, r)
    record("S1", "S6", "1", r, d, t, [shot(page, "09_chat_S6_followup.png")])
    cards = page.locator(".msg.bot").last.locator("button", has_text="이 역으로 계획 저장")
    cards.nth(0).click(); page.wait_for_selector(".save-msg.ok")
    cards.nth(1).click(); page.wait_for_selector(".save-msg.warn")
    shot(page, "12_chat_plan_saved_conflict.png")

    print("[S5] Tool 이 필요 없는 요청")
    fresh_chat(page)
    d, t = ask(page, "안녕")
    rec = record("S5", "S5", "1", "안녕", d, t, [])
    d, t = ask(page, "사용법 알려줘")
    rec2 = record("S5", "S5", "2", "사용법 알려줘", d, t, [shot(page, "08_chat_S5_no_tool.png")])
    rec["screenshots"] = rec2["screenshots"]

    print("[S7] 조건 부족 → 되묻기 → 답변")
    fresh_chat(page)
    r = "송도랑 마석에서 모일 장소 추천해줘"
    d, t = ask(page, r)
    record("S7", "S7", "1", r, d, t, [shot(page, "10_chat_S7_missing.png")])
    r2 = "토요일 3시, 이동 부담 적게"
    d, t = ask(page, r2)
    record("S7", "S7", "2", r2, d, t, [shot(page, "10b_chat_S7_answered.png")])

    print("[S8] 우선순위 되묻기(선택 버튼)")
    fresh_chat(page)
    r = "잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트"
    d, t = ask(page, r)
    record("S8", "S8", "1", r, d, t, [shot(page, "04_chat_ask_priority.png")])
    d, t = ask(page, choice="음식점 많게")
    record("S8", "S8", "2", "(버튼) 음식점 많게", d, t, [])

    print("[S10] 없는 조건을 임의로 채우지 않는가")
    fresh_chat(page)
    r = "잠실에서 7시 전에 끝나야 해"
    d, t = ask(page, r)
    record("S10", "S10", "1", r, d, t, [shot(page, "11_chat_S10_no_invention.png")])

    print("[S2] 재계획 후 충족 (위치 모호 → 선택 → 재계획)")
    fresh_chat(page)
    r = "중앙이랑 건대 사는 둘이 토요일 오후 2시에 만날래, 이동 적게"
    d, t = ask(page, r)
    record("S2", "S2", "1", r, d, t, [shot(page, "05a_chat_S2_ambiguous.png")])
    first = d["ask"]["choices"][0]["label"]
    d, t = ask(page, choice=first)
    record("S2", "S2", "2", f"(버튼) {first}", d, t, [shot(page, "05b_chat_S2_replan.png")])
    print("  로그 이벤트", save_log("S2", "S2_replan_run_log.jsonl"))

    print("[S3] 재계획 상한 도달 + 완화")
    fresh_chat(page)
    r = "수원이랑 춘천 사는 둘이 토요일 오후 2시에 만나고 싶어, 이동 적게"
    d, t = ask(page, r)
    record("S3", "S3", "1", r, d, t, [shot(page, "06_chat_S3_limit.png")])
    save_log("S3", "S3_limit_run_log.jsonl")
    d, t = ask(page, choice="최대 이동시간 120분으로 완화")
    record("S3", "S3", "2", "(버튼) 최대 이동시간 120분으로 완화", d, t, [shot(page, "06b_chat_S3_relaxed.png")])
    save_log("S3", "S3_limit_run_log.jsonl")   # 완화 후 재실행까지 포함 (2번째 실행의 분석 단계에 직전 판정 참조가 기록됨)
    sid3 = SESSIONS["S3"]
    for _ in range(30):  # 이상 실행의 LLM 진단(백그라운드) 완료 대기
        evs = requests.get(f"{API}/api/logs/{sid3}/evaluations", timeout=10).json()["evaluations"]
        if all(e["diagnosis"]["status"] != "pending" for e in evs):
            break
        time.sleep(1)
    (SAMPLES / "S3_evaluations.json").write_text(json.dumps(evs, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[계획 관리 화면]")
    page.goto(WEB + "/plans.html"); page.wait_for_selector(".plan-table")
    shot(page, "13_plans_list.png", expand=False)
    row2 = page.locator(".plan-table tbody tr").nth(1)
    row2.get_by_role("button", name="수정").click()
    page.locator('.plan-table input[type="time"]').first.fill("18:00")
    shot(page, "14_plans_edit.png", expand=False)
    page.get_by_role("button", name="저장").click(); page.wait_for_selector(".banner")
    shot(page, "15_plans_after_edit.png", expand=False)
    page.once("dialog", lambda dlg: dlg.accept())
    page.locator(".plan-table tbody tr").first.get_by_role("button", name="삭제").click()
    page.wait_for_function("document.querySelector('.banner') && document.querySelector('.banner').textContent.includes('삭제')")
    page.wait_for_timeout(400)
    shot(page, "15b_plans_after_delete.png", expand=False)

    print("[실행 로그 화면]")
    page.goto(WEB + "/logs.html"); page.wait_for_selector(".list-item")
    shot(page, "16_logs_list.png", expand=False)
    for key, name in (("S3", "17_logs_detail_anomaly.png"), ("S1", "18_logs_detail_normal.png")):
        page.locator(".list-item", has_text=SESSIONS[key][:12]).click()
        page.wait_for_selector(".evalcard")
        page.wait_for_timeout(500)
        shot(page, name, expand=False, full=False)
    page.once("dialog", lambda dlg: dlg.accept())
    page.locator(".list-item", has_text=SESSIONS["S5"][:12]).click()
    page.wait_for_selector("text=이 로그 삭제")
    page.get_by_role("button", name="이 로그 삭제").click()
    page.wait_for_selector(".banner.info")
    page.wait_for_timeout(400)
    shot(page, "19_logs_after_delete.png", expand=False)

    print("[API 문서]")
    page.goto(API + "/docs"); page.wait_for_selector(".swagger-ui .opblock", timeout=30000)
    page.wait_for_timeout(800)
    shot(page, "20_api_docs.png", expand=False)
    (DOCS / "openapi.json").write_text(json.dumps(requests.get(API + "/openapi.json", timeout=10).json(), ensure_ascii=False, indent=2), encoding="utf-8")
    b.close()


def phase_toolfail(pw):
    print("\n[S4] Tool 하나가 실패 (get_route 3번에 1번 NO_ROUTE 주입)")
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_context(viewport={"width": 1360, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul").new_page()
    fresh_chat(page)
    r = "잠실이랑 건대 사는 둘이 토요일 오후 2시에 만날래, 이동 부담 적게"
    d, t = ask(page, r)
    record("S4", "S4", "1", r, d, t, [shot(page, "07_chat_S4_tool_failure.png")])
    save_log("S4", "S4_tool_failure_run_log.jsonl")
    b.close()


def phase_nokey(pw):
    print("\n[S9] 카카오 API 키 누락")
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_context(viewport={"width": 1360, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul").new_page()
    fresh_chat(page)
    d, t = ask(page, S1)
    record("S9", "S9", "1", S1, d, t, [shot(page, "11b_chat_S9_no_api_key.png")])
    b.close()


def phase_nologging(pw):
    print("\n[S11] 로깅 Agent 를 연결하지 않고 실행")
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_context(viewport={"width": 1360, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul").new_page()
    fresh_chat(page)
    d, t = ask(page, S1)
    rec = record("S11", "S11", "1", S1, d, t, [shot(page, "11c_chat_S11_no_logging_agent.png")])
    rec["logs_after"] = requests.get(API + "/api/logs", timeout=10).json()
    rec["health"] = requests.get(API + "/api/health", timeout=10).json()
    b.close()


ANOMALY_CASES = [
    ("repeat_call", "A1", "같은 Tool 을 같은 입력으로 반복 호출", "21_logs_F3020_repeated_call.png"),
    ("unplanned_call", "A2", "계획에 없던 Tool 호출", "22_logs_F3020_unplanned_call.png"),
    ("missing_events", "A3", "근거 이벤트 없음 → 판정 불가", "23_logs_F3020_unknown.png"),
]
A_REQ = "잠실이랑 건대 사는 둘이 토요일 오후 2시에 만날래, 이동 부담 적게"


def phase_anomaly(pw, mode: str, key: str, title: str, shot_name: str):
    print(f"\n[{key}] F3020 이상 주입 ANOMALY_INJECT={mode}: {title}")
    b = pw.chromium.launch(channel="chrome", headless=True)
    page = b.new_context(viewport={"width": 1360, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul").new_page()
    fresh_chat(page)
    d, t = ask(page, A_REQ)
    rec = record(key, key, "1", A_REQ, d, t, [])
    sid = d["session_id"]
    for _ in range(30):   # 이상 실행의 LLM 진단(백그라운드) 완료 대기
        evs = requests.get(f"{API}/api/logs/{sid}/evaluations", timeout=10).json()["evaluations"]
        if all(e["diagnosis"]["status"] != "pending" for e in evs):
            break
        time.sleep(1)
    (SAMPLES / f"{key}_{mode}_evaluations.json").write_text(json.dumps(evs, ensure_ascii=False, indent=2), encoding="utf-8")
    save_log(key, f"{key}_{mode}_run_log.jsonl")
    rec["evaluation_final"] = {"verdict": evs[-1]["verdict"], "checks": evs[-1]["checks"], "warnings": [w["message"] for w in evs[-1]["warnings"]],
                               "diagnosis": evs[-1]["diagnosis"]}
    page.goto(WEB + "/logs.html"); page.wait_for_selector(".list-item")
    page.locator(".list-item").first.click(); page.wait_for_selector(".evalcard"); page.wait_for_timeout(600)
    rec["screenshots"] = [shot(page, shot_name, expand=False, full=False)]
    b.close()


def main():
    for p in (SHOTS,):
        if p.exists():
            shutil.rmtree(p)
    SHOTS.mkdir(parents=True, exist_ok=True); SAMPLES.mkdir(parents=True, exist_ok=True); VERIF.mkdir(parents=True, exist_ok=True)
    if not _port_free(5500):
        raise SystemExit("포트 5500 을 쓰는 프로세스가 있습니다. 종료한 뒤 다시 실행하세요.")
    web = subprocess.Popen([sys.executable, "-m", "http.server", "5500", "--directory", str(ROOT / "frontend")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    try:
        with sync_playwright() as pw:
            with backend():
                phase_main(pw)
            with backend(FAIL_INJECT="get_route:NO_ROUTE:3", ROUTE_WORKERS="1"):
                phase_toolfail(pw)
            with backend(KAKAO_REST_KEY=""):
                phase_nokey(pw)
            with backend(DISABLE_LOGGING_AGENT="1"):
                phase_nologging(pw)
            for mode, key, title, shot_name in ANOMALY_CASES:
                with backend(ANOMALY_INJECT=mode):
                    phase_anomaly(pw, mode, key, title, shot_name)
    finally:
        web.terminate()
        (VERIF / "records.json").write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n기록 {len(RECORDS)}건 저장: {VERIF / 'records.json'}")


if __name__ == "__main__":
    main()
