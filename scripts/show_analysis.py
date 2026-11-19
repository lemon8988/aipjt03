"""검증 4.2: 요청 하나에서 추출된 조건(기준·선호·제약)을 출력하고, 요청에 없던 조건이 채워지지 않았는지 확인한다.
실제 LLM 을 호출한다. 실행: python scripts/show_analysis.py  → docs/verification/analysis_outputs.md"""
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.agent.llm import LLMClient  # noqa: E402
from app.utils.geo import WEEKDAYS_KO, now_kst  # noqa: E402

CASES = [
    ("기준·선호가 모두 있는 요청", "잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트, 할 거 많은 곳으로"),
    ("제약(이동시간 상한)이 있는 요청", "수원이랑 의정부 사는 둘이 내일 저녁 7시에 만날래, 이동은 60분 넘지 않게"),
    ("날짜·시작 시각이 없는 요청 (임의로 채우면 안 됨)", "송도랑 마석에서 모일 장소 추천해줘"),
    ("종료 시각만 있는 요청 (시작 시각을 지어내면 안 됨)", "잠실에서 7시 전에 끝나야 해"),
    ("Tool 이 필요 없는 요청", "안녕"),
]
KEYS = ("intent", "locs", "purpose", "date", "start", "end", "prio", "t_max", "g_max")

if __name__ == "__main__":
    llm, n = LLMClient(), now_kst()
    today, wd = n.strftime("%Y-%m-%d"), WEEKDAYS_KO[n.weekday()]
    ctx = {"slots": {}, "has_previous": False, "pending": None, "last_summary": None}
    out = ["# 요청 분석 결과 (검증 4.2: 조건 추출, 임의 채움 여부)", "",
           f"실제 LLM 호출 · 기준일 {today}({wd}) · 값이 `null` 인 조건은 요청에 없어서 비워 둔 것(임의로 채우지 않음)", "",
           "| 케이스 | 요청 | " + " | ".join(KEYS) + " |", "|---|---|" + "---|" * len(KEYS)]
    for title, msg in CASES:
        a = llm.analyze(msg, ctx, today, wd).model_dump()
        out.append(f"| {title} | {msg} | " + " | ".join(f"`{a[k]}`" if a[k] not in (None, []) else "`null`" for k in KEYS) + " |")
        print(title, {k: a[k] for k in KEYS})
    out += ["", "※ 위치·날짜·시각이 부족한 요청은 Agent 가 Tool 을 부르기 전에 되묻는다 (docs/screenshots/10_chat_S7_missing.png, 11_chat_S10_no_invention.png)."]
    p = ROOT / "docs" / "verification" / "analysis_outputs.md"
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("저장:", p)
