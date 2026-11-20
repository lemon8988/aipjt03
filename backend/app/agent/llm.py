"""LLM 사용 지점은 두 곳뿐이다: 요청 분석(분석 단계)과 응답 문장 생성(응답 단계).
계획·실행·판단은 LLM 없이 규칙/계산식으로 처리한다 (설계서 4장).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from app.config import settings

log = logging.getLogger(__name__)

USAGE_TEXT = (
    "약속 장소 플래너예요. 참여자 2~4명의 위치와 날짜·시작 시각을 알려 주시면 "
    "이동 부담이 적은 만남 역과 주변 장소를 추천해요. "
    "예) \"잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트, 할 거 많은 곳으로\""
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Analysis(BaseModel):
    intent: Literal["new_meet", "edit_cond", "save_plan", "chat"] = "chat"
    reason: str = ""
    locs: list[str] | None = None
    purpose: str | None = None
    date: str | None = None
    start: str | None = None
    end: str | None = None
    prio: Literal["travel", "things", "food"] | None = None
    t_max: int | None = None
    g_max: int | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v):  # 형식이 틀리면 지어내지 않고 비운다 → 되묻기
        return v if v and _DATE_RE.match(v) else None

    @field_validator("start", "end")
    @classmethod
    def _time(cls, v):
        return v if v and _TIME_RE.match(v) else None

    @field_validator("locs")
    @classmethod
    def _locs(cls, v):
        return [s.strip() for s in v if isinstance(s, str) and s.strip()] if v is not None else None

    @field_validator("t_max", "g_max")
    @classmethod
    def _mins(cls, v):
        return v if v is not None and 5 <= v <= 360 else None


ANALYZE_SYSTEM = """당신은 약속 장소 플래너의 요청 분석기입니다. 사용자 메시지를 분석해 JSON 하나만 출력하세요.
규칙:
- intent: new_meet(새 약속 추천 요청), edit_cond(직전 조건 일부 변경/추가 정보 제공), save_plan(저장 요청), chat(인사·사용법·잡담).
- 메시지에 없는 값은 반드시 null. 추측하거나 기본값을 채우지 마세요.
- locs: 참여자들의 출발 위치 이름 목록(사용자가 말한 표현 그대로, 예 ["잠실","건대"]). 언급이 없으면 null.
- date: 메시지에 날짜·요일·상대 표현(오늘/내일/모레/이번 주 토요일 등)이 **있을 때만** YYYY-MM-DD 로. 없으면 반드시 null (오늘 날짜를 기본값으로 쓰지 마세요). 요일만 있으면 오늘 이후 가장 가까운 해당 요일(오늘 포함).
- start/end: 메시지에 시각이 있을 때만 24시간제 HH:MM. "7시 전에 끝나야 해"는 end=19:00 이고 start 는 null.
- prio: travel(이동 부담 적게), things(할 거 많게), food(음식점 많게) 중 사용자가 밝힌 것. 없으면 null.
- t_max/g_max: 사용자가 직접 말한 최대 이동시간(분)/참여자 간 이동시간 편차 상한(분). 없으면 null.
- edit_cond 에서는 사용자가 바꾼 값만 채우고 나머지는 null. 현재 조건(context)은 참고만 하세요.
- reason: 의도를 그렇게 분류한 이유를 한 문장으로.
출력 형식(키 고정): {"intent":..,"reason":..,"locs":..,"purpose":..,"date":..,"start":..,"end":..,"prio":..,"t_max":..,"g_max":..}"""

COMPOSE_SYSTEM = """당신은 약속 장소 플래너의 응답 작성기입니다. 주어진 facts(JSON)만 근거로 한국어로 작성하세요.
형식(반드시 지킬 것): 줄글 문단 금지. 짧은 항목을 불릿("- ")으로 나열하고, 아래 순서의 소제목 줄(이모지 + 굵은 글씨 없이 평문)을 사용하세요. 해당 내용이 없는 소제목은 생략합니다.
📍 추천: 날짜(date_note)와 1위 역 이름 한 줄
🚇 이동시간: 추천 역마다 "- 역이름: 출발지A N분 · 출발지B N분 (최대 N분, 편차 N분)" 한 줄씩
🍽️ 주변 장소: 우선순위 기준에 맞는 장소 건수를 불릿 1~2개로
☔ 날씨: 날씨 정보가 있을 때만 한 줄
💡 참고: 제외된 후보의 대표 사유 한 줄, 이동시간이 조회 시점 기준이라는 안내 한 줄
규칙: facts 에 없는 숫자·장소·사실을 만들지 말 것. 출발지는 facts 의 name 표기 그대로 부르고(예: '건대'를 '건대역'으로 바꾸지 말 것), 역 이름은 station 값을 그대로 쓸 것. 각 불릿은 한 줄로 짧게.
status 가 limit 이면 📍 대신 "⚠️ 조건 충족 후보 없음" 소제목 아래에 사유(near_miss)와 조건 조정 제안을 불릿으로 작성."""


DIAGNOSE_SYSTEM = """당신은 Agent 실행 로그 진단가입니다. 규칙 기반 판정이 '이상'으로 표시한 실행의 기록과 경고를 읽고, 원인을 **세 문장 이내**로 진단하세요.
포함할 것: ① 어느 쪽의 문제인지(메인 Agent의 계획·실행·판단 / Tool·외부 API / 사용자 입력·조건) ② 무엇을 고치면 되는지.
구분 기준: 재계획이 상한에 도달했더라도 Tool 이 정상 응답(ok)했고 후보는 있었는데 사용자의 이동시간·편차 제약을 넘긴 것이라면 Agent 결함이 아니라 '사용자 조건이 현실적으로 충족 불가'(예: 두 지역이 너무 멂)일 수 있습니다.
Tool 호출 실패 코드(tool_res ok=False), 후보 수·통과 수(candidates/passed), 같은 입력의 반복 호출처럼 기록에 보이는 근거로 판단하세요.
경고와 기록에 없는 사실은 지어내지 말고, 근거가 부족하면 그렇게 말하세요."""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("BAD_OUTPUT", "LLM 응답에서 JSON 을 찾지 못했습니다.")
    try:
        return json.loads(m.group(0))
    except ValueError:
        raise LLMError("BAD_OUTPUT", "LLM 응답 JSON 형식이 올바르지 않습니다.") from None


_DATE_HINT = re.compile(r"오늘|당일|금일|지금|\d+\s*월|\d+\s*일|\d+\s*/\s*\d+|\d{4}-\d{2}-\d{2}")


def drop_unsupported_today(a: Analysis, message: str, today: str) -> Analysis:
    """모델이 날짜 언급 없이 '오늘'을 기본값으로 채우는 경우를 막는다 (값을 지어내지 않고 되묻기로 넘김)."""
    if a.date == today and not _DATE_HINT.search(message):
        a.date = None
    return a


class LLMClient:
    def __init__(self) -> None:
        self._model = None

    def _chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 1024) -> str:
        """OpenAI 호환 Chat Completions (GPT). json_mode 이면 JSON 객체 출력을 요청한다."""
        s = settings()
        if not s.llm_key:
            raise LLMError("NO_KEY", "LLM_API_KEY 가 설정되지 않았습니다.")
        try:
            if self._model is None:
                from langchain_openai import ChatOpenAI
                self._model = ChatOpenAI(model=s.llm_model, api_key=s.llm_key, base_url=s.llm_base_url,
                                         temperature=0, timeout=40, max_retries=1)
            model = self._model.bind(response_format={"type": "json_object"}) if json_mode else self._model
            msg = model.invoke([("system", system), ("human", user)])
            return msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001  (오류 상세·키는 노출하지 않는다)
            code = getattr(e, "status_code", None)
            # 원인 파악용 서버 로그 (화면에는 노출하지 않는다). 키 형태 문자열은 가린다.
            detail = re.sub(r"(sk-|AIza|Bearer\s)\S+", r"***", str(e))[:200]
            log.warning("LLM 호출 실패: %s status=%s model=%s base_url=%s detail=%s",
                        type(e).__name__, code, s.llm_model, s.llm_base_url, detail)
            if code in (401, 403):
                raise LLMError("NO_KEY", "LLM API 키가 유효하지 않습니다.") from e
            raise LLMError("LLM_ERROR", "LLM 호출에 실패했습니다.") from e

    def analyze(self, message: str, context: dict, today: str, weekday: str) -> Analysis:
        user = (f"오늘: {today} ({weekday}요일, KST)\ncontext: {json.dumps(context, ensure_ascii=False)}\n"
                f"사용자 메시지: {message}")
        text = self._chat(ANALYZE_SYSTEM, user, json_mode=True)
        try:
            a = Analysis(**_extract_json(text))
        except ValidationError:
            raise LLMError("BAD_OUTPUT", "LLM 분석 결과 형식이 올바르지 않습니다.") from None
        return drop_unsupported_today(a, message, today)

    def compose(self, facts: dict) -> str:
        return self._chat(COMPOSE_SYSTEM, json.dumps(facts, ensure_ascii=False), max_tokens=700).strip()

    def diagnose(self, summary: str, warnings: list) -> str:
        """이상으로 판정된 실행에만 호출된다 (F3020). 판정과 별개로 저장되는 원인 진단."""
        user = f"경고: {json.dumps(warnings, ensure_ascii=False)}\n\n실행 기록:\n{summary}"
        return self._chat(DIAGNOSE_SYSTEM, user, max_tokens=400).strip()

    def chat_reply(self, message: str) -> str:
        return self._chat("당신은 약속 장소 플래너 안내 도우미입니다. 서비스 소개: " + USAGE_TEXT +
                          " 두세 문장으로 짧게 답하세요.", message).strip()
