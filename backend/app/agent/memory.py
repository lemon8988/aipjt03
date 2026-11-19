"""대화 맥락 Memory (F3010). 세션 메모리 안에서만 보관하고 DB 에는 쓰지 않는다.

보관: 조건(slots), 해석한 위치 좌표, 직전 추천 요약, 후속 요청에서 재사용할 Tool 결과 캐시(정규화된 값만)
보관하지 않음: Tool 원본 응답(카카오 약관), 중간 판단 전문
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

SLOT_KEYS = ("locs", "purpose", "date", "start", "end", "prio", "t_max", "g_max")


def fresh_slots() -> dict:
    return {"locs": [], "purpose": None, "date": None, "start": None, "end": None,
            "prio": None, "t_max": None, "g_max": None}


def cache_key(tool: str, args: dict) -> str:
    def norm(v):
        if isinstance(v, float):
            return round(v, 5)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        return v
    return tool + "|" + json.dumps(norm(args), sort_keys=True, ensure_ascii=False)


@dataclass
class SessionMemory:
    slots: dict = field(default_factory=fresh_slots)
    geo: dict = field(default_factory=dict)        # 입력 위치명 → {name,addr,x,y}
    cache: dict = field(default_factory=dict)      # cache_key → 정규화된 Tool 결과
    summary: dict | None = None                    # 직전 추천 요약
    pending: dict | None = None                    # 되묻기 상태 {kind, intent}
    turns: int = 0

    def context(self) -> dict:
        """분석 LLM 에 넘길 맥락 (원본 응답 없음)."""
        return {"slots": self.slots, "last_summary": self.summary,
                "pending": self.pending, "has_previous": self.turns > 0}


class MemoryStore:
    def __init__(self) -> None:
        self._m: dict[str, SessionMemory] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionMemory:
        with self._lock:
            return self._m.setdefault(session_id, SessionMemory())

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._m.pop(session_id, None)
