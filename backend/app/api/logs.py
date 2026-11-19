"""실행 로그: 목록·상세·삭제만 제공한다. 수정(PUT/PATCH) 경로는 의도적으로 없다 (F3014).
평가(F3020)는 조회만 가능하며 로그 삭제 시 함께 삭제된다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.container import Container, get_container

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_logs(c: Container = Depends(get_container)) -> list[dict]:
    return [{**s, **c.evals.summary(s["session_id"])} for s in c.log_store.list_sessions()]


@router.get("/{session_id}")
def get_log(session_id: str, c: Container = Depends(get_container)) -> dict:
    events = c.log_store.get_session(session_id)
    if events is None:
        raise HTTPException(404, "실행 로그를 찾을 수 없습니다.")
    return {"session_id": session_id, "events": events}  # 실행(저장) 순서 그대로


@router.get("/{session_id}/evaluations")
def get_evaluations(session_id: str, c: Container = Depends(get_container)) -> dict:
    """이상 징후 판정(규칙 기반)과, 이상 실행에만 붙는 LLM 원인 진단. 진단은 판정과 구분되어 있다."""
    if c.log_store.get_session(session_id) is None:
        raise HTTPException(404, "실행 로그를 찾을 수 없습니다.")
    return {"session_id": session_id, "evaluations": c.evals.list(session_id)}


@router.delete("/{session_id}", status_code=204)
def delete_log(session_id: str, c: Container = Depends(get_container)):
    if not c.log_store.delete_session(session_id):
        raise HTTPException(404, "실행 로그를 찾을 수 없습니다.")
    c.evals.delete(session_id)
    return Response(status_code=204)
