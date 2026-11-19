"""FastAPI 앱. 실행: cd backend && uvicorn app.main:app --reload --port 8000"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import chat, logs, plans
from app.config import settings
from app.container import get_container
from app.schemas import HealthOut

log = logging.getLogger("api")

app = FastAPI(title="약속 장소 플래너 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500", "http://localhost:8080", "http://127.0.0.1:8080"],
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(chat.router)
app.include_router(plans.router)
app.include_router(logs.router)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    # 오류 상세·키는 화면에 노출하지 않는다 (NF3002). 상세는 서버 로그에만 남긴다.
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error_code": "INTERNAL", "message": "서버 내부 오류가 발생했습니다."})


@app.get("/api/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    """키 '값'이 아니라 설정 여부만 알려 준다. 프론트가 설정 안내를 띄우는 데 쓴다."""
    s, c = settings(), get_container()
    return HealthOut(status="ok", kakao_key=bool(s.kakao_key), llm_key=bool(s.llm_key),
                     route_provider=s.route_provider, logging_agent=c.logging_agent is not None)
