from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.container import Container, get_container
from app.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, c: Container = Depends(get_container)) -> ChatResponse:
    """대화 요청. 세션 id 가 없으면 서버가 발급한다. 응답에 이번 요청의 실행 이벤트(timeline)를 포함한다."""
    sid = req.session_id or uuid.uuid4().hex
    events: list[dict] = []

    def collect(e: dict) -> None:
        if e.get("session_id") == sid:
            events.append(e)

    c.bus.subscribe(collect)  # 화면 표시용 수집기. 로깅 Agent 와는 별개의 구독자다
    try:
        payload = c.agent.run(sid, req.message.strip(), req.choice.model_dump() if req.choice else None)
    finally:
        c.bus.unsubscribe(collect)
    evaluation = c.evals.latest(sid) if c.logging_enabled else None
    return ChatResponse(session_id=sid, timeline=events, evaluation=evaluation, **payload)
