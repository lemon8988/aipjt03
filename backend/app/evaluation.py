"""로깅 Agent 평가 기능 (F3020).

- 판정(verdict)은 LLM 없이 이벤트를 대조하고 횟수를 세는 규칙으로만 한다.
    repeated_call  같은 Tool 이 같은 입력으로 한 실행(요청) 안에서 2번 이상 호출됨
    excess_replan  재계획이 상한(MAX_REPLAN)에 도달함
    unplanned_call 계획 단계가 정한 Tool 목록에 없는 Tool 이 호출됨 (계획 없이 호출한 경우 포함)
- 근거 이벤트가 없으면(실행 시작 이벤트를 놓친 경우 등) '이상 없음'이 아니라 '판정 불가(unknown)' 로 남긴다.
- 경고가 하나라도 나온 실행에만 LLM 이 원인을 진단한다. 진단은 판정과 구분해 diagnosis 필드에 따로 저장하며
  경고 수(warning_count)에 섞지 않는다. 정상·판정 불가 실행에는 LLM 을 호출하지 않는다.
- 진단이 실패해도 판정과 실행 로그는 그대로 남는다.

판정 결과 형식은 메인 Agent 가 그대로 참조할 수 있는 dict 이다 (EvalStore.latest / GET /api/logs/{sid}/evaluations).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import settings
from app.events.bus import Event
from app.logging_agent import valid_session_id

log = logging.getLogger("evaluation")

RULES = ("repeated_call", "excess_replan", "unplanned_call")
VERDICTS = ("ok", "anomaly", "unknown")


def _canon(args) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def evaluate_run(events: list[Event], *, max_replan: int | None = None) -> dict:
    """한 실행(요청 1건)의 이벤트로 이상 징후를 판정한다. LLM 을 쓰지 않는 순수 함수."""
    limit = settings().max_replan if max_replan is None else max_replan
    reqs = [e for e in events if e.get("type") == "tool_req"]
    replans = [e for e in events if e.get("type") == "replan"]
    run_end = next((e for e in reversed(events) if e.get("type") == "run_end"), None)
    plan = None
    for e in events:
        if e.get("type") == "step_end" and e.get("step") == "plan" and isinstance(e.get("plan"), list):
            plan = e["plan"]
    # 모든 실행은 analyze 단계로 시작한다. 그 시작 이벤트가 없으면 판단할 근거가 부족하다.
    has_basis = any(e.get("type") == "step_start" and e.get("step") == "analyze" for e in events)

    stats = {"tool_calls": len(reqs), "replans": len(replans), "total_ms": (run_end or {}).get("total_ms"),
             "event_count": len(events)}
    warnings: list[dict] = []
    checks = {r: "na" for r in RULES}

    if has_basis:
        checks = {r: "ok" for r in RULES}
        # 1) 같은 Tool, 같은 입력의 반복 호출
        counts = Counter((e["tool"], _canon(e.get("args"))) for e in reqs)
        for (tool, args), n in counts.items():
            if n >= 2:
                checks["repeated_call"] = "warn"
                warnings.append({"code": "repeated_call", "severity": "warn",
                                 "message": f"{tool} 가 같은 입력으로 {n}회 호출됨",
                                 "evidence": {"tool": tool, "args": json.loads(args), "count": n}})
        # 2) 재계획 과다 (상한 도달)
        if limit > 0 and len(replans) >= limit:
            checks["excess_replan"] = "warn"
            warnings.append({"code": "excess_replan", "severity": "warn",
                             "message": f"재계획 {len(replans)}회 (상한 {limit}회 도달)",
                             "evidence": {"replans": len(replans), "limit": limit,
                                          "changes": [e.get("change") for e in replans]}})
        # 3) 계획에 없던 Tool 호출
        planned = set(plan or [])
        stray = Counter(e["tool"] for e in reqs if e["tool"] not in planned)
        if stray:
            checks["unplanned_call"] = "warn"
            why = "계획 단계 없이 호출됨" if plan is None else "계획에 없던 Tool"
            warnings.append({"code": "unplanned_call", "severity": "warn",
                             "message": f"{why}: " + ", ".join(f"{t}×{n}" for t, n in stray.items()),
                             "evidence": {"tools": dict(stray), "plan": plan}})

    verdict = "unknown" if not has_basis else ("anomaly" if warnings else "ok")
    return {
        "verdict": verdict,                 # ok | anomaly | unknown(판정 불가)
        "basis": has_basis,
        "warning_count": len(warnings),     # 진단은 포함하지 않는다
        "warnings": warnings,
        "checks": checks,                   # 규칙별 ok | warn | na(판정 불가)
        "stats": stats,
        "diagnosis": {"status": "skipped", "text": None},   # 판정과 구분된 LLM 진단
    }


def summarize_events(events: list[Event], limit: int = 80) -> str:
    """LLM 진단에 넘길 실행 기록 요약. 원본 응답은 로그에 없으므로 인자·결과 코드·횟수만 담긴다."""
    lines = []
    for e in events[:limit]:
        t = e.get("type")
        if t in ("step_start", "step_end"):
            extra = {k: v for k, v in e.items() if k in ("intent", "reason", "plan", "candidates", "passed", "decision", "status", "ask", "error")}
            lines.append(f"{t} {e.get('step')} {json.dumps(extra, ensure_ascii=False) if extra else ''}".strip())
        elif t == "tool_req":
            lines.append(f"tool_req {e.get('tool')} {_canon(e.get('args'))}")
        elif t == "tool_res":
            lines.append(f"tool_res {e.get('tool')} ok={e.get('ok')} code={e.get('code')} count={e.get('count')}")
        elif t in ("replan", "llm_decision", "cache_hit", "run_end"):
            lines.append(f"{t} " + json.dumps({k: v for k, v in e.items() if k not in ("session_id", "seq", "ts", "type")}, ensure_ascii=False))
    if len(events) > limit:
        lines.append(f"... (이후 {len(events) - limit}건 생략)")
    return "\n".join(lines)


def trim_sentences(text: str, n: int = 3, max_chars: int = 500) -> str:
    """진단은 세 문장 이내 (모델이 길게 써도 잘라낸다)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?。])\s+|\n+", text.strip()) if p.strip()]
    return " ".join(parts[:n])[:max_chars]


class EvalStore:
    """판정 결과 저장소: 세션별 JSON 파일(실행 순서 리스트). 로그 원본(JSONL)과 분리한다."""

    def __init__(self, base_dir: Path):
        self.dir = Path(base_dir)
        self._lock = threading.Lock()

    def _path(self, sid: str) -> Path | None:
        return self.dir / f"{sid}.json" if valid_session_id(sid) else None

    def _read(self, p: Path) -> list[dict]:
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def _write(self, p: Path, data: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def append(self, sid: str, rec: dict) -> dict:
        p = self._path(sid)
        with self._lock:
            data = self._read(p) if p else []
            rec = {**rec, "session_id": sid, "run_index": len(data) + 1,
                   "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if p:
                self._write(p, data + [rec])
        return rec

    def list(self, sid: str) -> list[dict]:
        p = self._path(sid)
        return self._read(p) if p else []

    def latest(self, sid: str) -> dict | None:
        data = self.list(sid)
        return data[-1] if data else None

    def update_diagnosis(self, sid: str, run_index: int, diagnosis: dict) -> None:
        p = self._path(sid)
        if not p:
            return
        with self._lock:
            data = self._read(p)
            for r in data:
                if r["run_index"] == run_index:
                    r["diagnosis"] = diagnosis
            self._write(p, data)

    def summary(self, sid: str) -> dict:
        data = self.list(sid)
        return {"latest_verdict": data[-1]["verdict"] if data else None,
                "anomaly_runs": sum(1 for r in data if r["verdict"] == "anomaly"),
                "evaluated_runs": len(data)}

    def delete(self, sid: str) -> None:
        p = self._path(sid)
        if p and p.exists():
            p.unlink()


class RunEvaluator:
    """실행 종료 시 호출된다: 규칙으로 판정 → 저장 → (이상일 때만) 원인 진단."""

    def __init__(self, store: EvalStore, diagnose_fn: Callable[[str, list], str] | None = None,
                 background: bool = True):
        self.store, self.diagnose_fn, self.background = store, diagnose_fn, background

    def on_run_end(self, sid: str, events: list[Event]) -> dict:
        rec = evaluate_run(events)
        if rec["verdict"] == "anomaly" and self.diagnose_fn is not None:
            rec["diagnosis"] = {"status": "pending", "text": None}
        saved = self.store.append(sid, rec)  # 판정은 진단과 무관하게 먼저 저장된다
        if saved["diagnosis"]["status"] == "pending":
            args = (sid, saved["run_index"], summarize_events(events), saved["warnings"])
            if self.background:
                threading.Thread(target=self._diagnose, args=args, daemon=True).start()
            else:
                self._diagnose(*args)
        return saved

    def _diagnose(self, sid: str, run_index: int, summary: str, warnings: list) -> None:
        try:
            text = trim_sentences(self.diagnose_fn(summary, warnings))  # type: ignore[misc]
            diag = {"status": "done", "text": text} if text else {"status": "failed", "text": None}
        except Exception:  # noqa: BLE001 - 진단 실패가 판정·로그·대화에 영향을 주면 안 된다
            log.warning("diagnosis failed", exc_info=True)
            diag = {"status": "failed", "text": None}
        try:
            self.store.update_diagnosis(sid, run_index, diag)
        except Exception:  # noqa: BLE001
            log.warning("diagnosis save failed", exc_info=True)
