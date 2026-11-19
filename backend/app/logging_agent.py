"""로깅 서브 Agent + 실행 로그 저장소 (F3013, F3014).

- 이벤트를 받는 즉시 세션별 JSONL 에 순서대로 덧붙인다.
- 수집·저장에 LLM 을 호출하지 않는다. 사실을 그대로 남긴다.
- 저장 실패는 삼킨다 (대화는 정상 동작해야 한다).
- 수정 API 는 없다: 목록·상세·삭제만 제공한다.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from app.events.bus import Event, EventBus

log = logging.getLogger("logging_agent")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_session_id(sid: str) -> bool:
    return bool(_SID_RE.match(sid or ""))


class LoggingAgent:
    def __init__(self, bus: EventBus, log_dir: Path, evaluator=None):
        self.dir = Path(log_dir)
        self.evaluator = evaluator          # F3020: 실행 종료 시 규칙으로 판정 (LLM 진단은 평가기가 이상일 때만)
        self._lock = threading.Lock()
        self._run: dict[str, list[Event]] = {}   # 세션별 '현재 실행'의 이벤트 버퍼
        bus.subscribe(self.on_event)

    def on_event(self, event: Event) -> None:
        sid = event.get("session_id", "")
        if not valid_session_id(sid):
            return
        self._store(sid, event)
        self._evaluate_if_run_ended(sid, event)

    def _evaluate_if_run_ended(self, sid: str, event: Event) -> None:
        if self.evaluator is None:
            return
        try:
            if event.get("type") == "step_start" and event.get("step") == "analyze":
                self._run[sid] = []                      # 새 실행 시작 (끝나지 않은 이전 실행의 잔여 버퍼 폐기)
            self._run.setdefault(sid, []).append(event)
            if event.get("type") == "run_end":
                self.evaluator.on_run_end(sid, self._run.pop(sid, []))
        except Exception:  # noqa: BLE001 - 평가 실패가 대화에 영향을 주면 안 된다
            log.warning("evaluation failed", exc_info=True)

    def _store(self, sid: str, event: Event) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self._lock, open(self.dir / f"{sid}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            log.warning("log write failed", exc_info=True)


class LogStore:
    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir)

    def _read(self, path: Path) -> list[Event]:
        events: list[Event] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        return events

    def list_sessions(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for p in self.dir.glob("*.jsonl"):
            ev = self._read(p)
            if not ev:
                continue
            ends = [e for e in ev if e["type"] == "run_end"]
            out.append({
                "session_id": p.stem,
                "started_at": ev[0].get("ts"),
                "last_at": ev[-1].get("ts"),
                "event_count": len(ev),
                "runs": len(ends),
                "last_status": ends[-1].get("status") if ends else None,
                "replans": sum(1 for e in ev if e["type"] == "replan"),
            })
        return sorted(out, key=lambda s: s["last_at"] or "", reverse=True)

    def get_session(self, sid: str) -> list[Event] | None:
        if not valid_session_id(sid):
            return None
        p = self.dir / f"{sid}.jsonl"
        return self._read(p) if p.exists() else None

    def delete_session(self, sid: str) -> bool:
        if not valid_session_id(sid):
            return False
        p = self.dir / f"{sid}.jsonl"
        if not p.exists():
            return False
        p.unlink()
        return True
