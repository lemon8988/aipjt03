"""실행 이벤트 발행/구독 (F3012). 발행하는 쪽은 누가 구독하는지 모른다.

이벤트 공통 필드: session_id, seq, ts, type
type: step_start, step_end, tool_req, tool_res, cache_hit, llm_decision, replan, run_end
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("events")
Event = dict
Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Handler] = []
        self._seq: dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()

    def subscribe(self, fn: Handler) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Handler) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def publish(self, session_id: str, type: str, **fields) -> Event:
        # 락 안에서 핸들러를 호출해 seq 순서와 저장 순서를 일치시킨다.
        with self._lock:
            self._seq[session_id] += 1
            event: Event = {
                "session_id": session_id,
                "seq": self._seq[session_id],
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "type": type,
                **fields,
            }
            for fn in list(self._subs):
                try:
                    fn(event)
                except Exception:  # noqa: BLE001 - 구독자 실패가 메인 Agent 에 영향을 주면 안 된다
                    log.warning("event subscriber failed", exc_info=True)
        return event
