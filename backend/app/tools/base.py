"""Tool 공통 계층: 응답 형식, 실패 코드, 레지스트리, 실패 주입 (F3001)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.config import settings

ERROR_CODES = {
    "NO_KEY", "BAD_INPUT", "NO_RESULT", "NO_ROUTE", "AMBIGUOUS",
    "OUT_OF_RANGE", "RATE_LIMIT", "API_ERROR", "TIMEOUT",
}


class ToolError(Exception):
    """Tool 내부에서 던지면 call_tool 이 실패 응답으로 바꾼다. 밖으로는 나가지 않는다."""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def ok(data: dict, basis: str | None = None, notes: list[str] | None = None) -> dict:
    return {"ok": True, "data": data, "meta": {"basis": basis or "", "notes": list(notes or [])}}


def fail(code: str, message: str, detail: Any = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "detail": detail}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str  # "기능"이 아니라 "언제 쓰는지"를 적는다
    input_schema: dict
    fn: Callable[[dict], dict]
    required: tuple[str, ...] = ()
    kind: str = "external"  # external | local


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    return spec


def list_specs() -> list[dict]:
    """Agent/문서에 노출하는 명세: 이름·설명·입력 스키마만 (구현은 노출하지 않는다)."""
    return [
        {"name": s.name, "kind": s.kind, "description": s.description, "input_schema": s.input_schema}
        for s in REGISTRY.values()
    ]


def openai_tools() -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": s.name, "description": s.description, "parameters": s.input_schema}}
        for s in REGISTRY.values()
    ]


_inject_lock = threading.Lock()
_inject_counts: dict[str, int] = {}


def reset_injection() -> None:
    with _inject_lock:
        _inject_counts.clear()


def _maybe_inject(name: str) -> dict | None:
    """FAIL_INJECT="get_route:API_ERROR:3" → get_route 를 3번에 1번 API_ERROR 로 실패시킨다."""
    for part in (p.strip() for p in settings().fail_inject.split(",")):
        bits = part.split(":")
        if len(bits) < 2 or bits[0] != name:
            continue
        every = int(bits[2]) if len(bits) > 2 and bits[2].isdigit() else 1
        with _inject_lock:
            n = _inject_counts.get(part, 0) + 1
            _inject_counts[part] = n
        if n % every == 0:
            return fail(bits[1], f"주입된 실패({bits[1]})")
    return None


def call_tool(name: str, args: dict) -> dict:
    """모든 Tool 호출의 단일 진입점. 예외는 절대 밖으로 던지지 않는다."""
    spec = REGISTRY.get(name)
    if spec is None:
        return fail("BAD_INPUT", f"알 수 없는 Tool: {name}")
    missing = [k for k in spec.required if args.get(k) in (None, "")]
    if missing:
        return fail("BAD_INPUT", f"필수 인자 누락: {', '.join(missing)}")
    injected = _maybe_inject(name)
    if injected:
        return injected
    try:
        result = spec.fn(args)
    except ToolError as e:
        return fail(e.code, e.message, e.detail)
    except Exception as e:  # noqa: BLE001 - 어떤 예외도 서비스를 멈추면 안 된다 (NF3003)
        return fail("API_ERROR", "Tool 실행 중 오류가 발생했습니다.", type(e).__name__)
    return result
