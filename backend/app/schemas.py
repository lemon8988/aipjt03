"""API 요청·응답 모델 (F3017)."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ChatChoice(BaseModel):
    kind: Literal["priority", "disambiguate", "patch"]
    value: Any
    name: str | None = None  # disambiguate: 원래 입력한 위치 이름


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    message: str = Field(default="", max_length=500)
    choice: ChatChoice | None = None

    @model_validator(mode="after")
    def _need_input(self):
        if not self.message.strip() and self.choice is None:
            raise ValueError("message 또는 choice 가 필요합니다.")
        return self


class ChatResponse(BaseModel):
    session_id: str
    type: Literal["answer", "ask", "error"]
    status: str
    answer: str
    ask: dict | None = None
    result: dict | None = None
    error_code: str | None = None
    timeline: list[dict] = []  # 이번 요청의 실행 이벤트 (단계·Tool 호출·재계획)
    evaluation: dict | None = None  # 로깅 Agent 의 이번 실행 판정 (로깅 Agent 가 꺼져 있으면 None)


class PlanBase(BaseModel):
    date: str
    start_time: str
    end_time: str | None = None
    participants: list[str] = Field(min_length=1, max_length=8)
    purpose: str | None = None
    priority: Literal["travel", "things", "food"] | None = None
    station: str = Field(min_length=1, max_length=60)
    spot: str | None = Field(default=None, max_length=100)

    @field_validator("date")
    @classmethod
    def _d(cls, v):
        if not _DATE.match(v):
            raise ValueError("date 는 YYYY-MM-DD 형식이어야 합니다.")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def _t(cls, v):
        if v is not None and not _TIME.match(v):
            raise ValueError("시각은 HH:MM 형식이어야 합니다.")
        return v

    @model_validator(mode="after")
    def _order(self):
        if self.end_time and self.end_time <= self.start_time:
            raise ValueError("종료 시각은 시작 시각보다 늦어야 합니다.")
        return self


class PlanIn(PlanBase):
    pass


class PlanPatch(BaseModel):
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    participants: list[str] | None = Field(default=None, min_length=1, max_length=8)
    purpose: str | None = None
    priority: Literal["travel", "things", "food"] | None = None
    station: str | None = Field(default=None, min_length=1, max_length=60)
    spot: str | None = Field(default=None, max_length=100)

    @field_validator("date")
    @classmethod
    def _d(cls, v):
        if v is not None and not _DATE.match(v):
            raise ValueError("date 는 YYYY-MM-DD 형식이어야 합니다.")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def _t(cls, v):
        if v is not None and not _TIME.match(v):
            raise ValueError("시각은 HH:MM 형식이어야 합니다.")
        return v


class PlanOut(PlanBase):
    id: int
    created_at: str
    updated_at: str


class Conflict(BaseModel):
    id: int
    date: str
    start_time: str
    end_time: str | None = None
    station: str


class PlanSaveResult(BaseModel):
    plan: PlanOut
    conflicts: list[Conflict] = []  # 겹치는 계획이 있어도 저장은 막지 않는다 (F3015)


class HealthOut(BaseModel):
    status: str
    kakao_key: bool
    llm_key: bool
    route_provider: str
    logging_agent: bool
