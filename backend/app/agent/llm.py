"""LLM 사용 지점은 두 곳뿐이다: 요청 분석(분석 단계)과 응답 문장 생성(응답 단계).
계획·실행·판단은 LLM 없이 규칙/계산식으로 처리한다 (설계서 4장).
"""
from __future__ import annotations

import json
import re
from typing import Literal

import requests

from pydantic import BaseModel, ValidationError, field_validator

from app.config import settings

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

COMPOSE_SYSTEM = """당신은 약속 장소 플래너의 응답 작성기입니다. 주어진 facts(JSON)만 근거로 한국어로 3~6문장 요약을 작성하세요.
규칙: facts 에 없는 숫자·장소·사실을 만들지 말 것. 출발지는 facts 의 name 표기 그대로 부르고(예: '건대'를 '건대역'으로 바꾸지 말 것), 역 이름은 station 값을 그대로 쓸 것. 이동시간은 조회 시점 기준임을 한 번 언급. 날짜 해석(date_note)이 있으면 언급.
추천 역별 핵심 근거(참여자별 이동시간, 편차, 우선순위 기준)를 짚고, 제외된 후보가 있으면 대표 사유를 한 줄로. 날씨 정보가 있으면 반영.
status 가 limit 이면 조건을 만족하는 후보가 없었음을 사유(near_miss)와 함께 알리고 조건 조정을 제안."""


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


def gemini_generate(system: str, user: str, *, json_mode: bool = False, max_tokens: int = 1024,
                    tools: list | None = None, timeout: float = 40) -> dict:
    """Gemini generateContent (GMS 경유). 키는 헤더로만 보낸다(URL·로그에 남지 않게). 원본 응답 JSON 을 돌려준다."""
    s = settings()
    if not s.llm_key:
        raise LLMError("NO_KEY", "LLM_API_KEY 가 설정되지 않았습니다.")
    url = f"{s.llm_base_url.rstrip('/')}/models/{s.llm_model}:generateContent"
    body: dict = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                  "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
    try:
        r = requests.post(url, json=body, headers={"x-goog-api-key": s.llm_key}, timeout=timeout)
    except requests.RequestException as e:
        raise LLMError("LLM_ERROR", "LLM 서버에 연결하지 못했습니다.") from e
    if r.status_code in (401, 403):
        raise LLMError("NO_KEY", "LLM API 키가 유효하지 않습니다.")
    if r.status_code != 200:
        raise LLMError("LLM_ERROR", f"LLM 호출에 실패했습니다. (HTTP {r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise LLMError("LLM_ERROR", "LLM 응답 형식이 올바르지 않습니다.") from None


def _gemini_text(payload: dict) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError("LLM_ERROR", "LLM 응답이 비어 있습니다. (안전 필터 등)") from None


def gemini_tool_declarations(tools: list[dict]) -> list[dict]:
    """OpenAI 형식 Tool 명세 → Gemini functionDeclarations ('default' 같은 미지원 필드 제거)."""
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if k != "default"}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return [{"name": t["function"]["name"], "description": t["function"]["description"],
             "parameters": clean(t["function"]["parameters"])} for t in tools]


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
        s = settings()
        if s.llm_provider == "gemini":
            return _gemini_text(gemini_generate(system, user, json_mode=json_mode, max_tokens=max_tokens))
        if not s.llm_key:
            raise LLMError("NO_KEY", "LLM_API_KEY 가 설정되지 않았습니다.")
        try:
            if self._model is None:
                from langchain_openai import ChatOpenAI
                self._model = ChatOpenAI(model=s.llm_model, api_key=s.llm_key, base_url=s.llm_base_url,
                                         temperature=0, timeout=40, max_retries=1)
            msg = self._model.invoke([("system", system), ("human", user)])
            return msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False)
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
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
