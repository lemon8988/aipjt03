"""약속 계획 저장소 (F3015, F3016). SQLite.

- 종료 시각은 null 로 저장할 수 있다(사용자가 말하지 않은 값은 채우지 않는다).
  겹침 판정에서만 시작 + 기본 체류 시간(3시간)을 쓴다.
- 같은 날짜에 시간이 겹쳐도 저장은 막지 않고 conflicts 로 알린다.
- 카카오 약관상 응답 저장 금지이므로 사용자가 확정한 값(이름·시각)만 저장하고 좌표·주소·URL 은 저장하지 않는다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

FIELDS = ("date", "start_time", "end_time", "participants", "purpose", "priority", "station", "spot")

_DDL = """
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT,
  participants TEXT NOT NULL,
  purpose TEXT,
  priority TEXT,
  station TEXT NOT NULL,
  spot TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)"""


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _span(plan: dict) -> tuple[int, int]:
    start = _minutes(plan["start_time"])
    end = _minutes(plan["end_time"]) if plan.get("end_time") else start + settings().default_stay_h * 60
    return start, end


def overlaps(a: dict, b: dict) -> bool:
    """같은 날짜이고 시간 구간이 겹치는가. 맞닿는 경계(10:00 종료/10:00 시작)는 겹침이 아니다."""
    if a["date"] != b["date"]:
        return False
    a0, a1 = _span(a)
    b0, b1 = _span(b)
    return a0 < b1 and a1 > b0


class PlanStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(_DDL)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["participants"] = json.loads(d["participants"])
        return d

    def _conflicts(self, c: sqlite3.Connection, plan: dict, exclude_id: int | None = None) -> list[dict]:
        rows = c.execute("SELECT * FROM plans WHERE date = ?", (plan["date"],)).fetchall()
        out = []
        for r in rows:
            other = self._row(r)
            if other["id"] != exclude_id and overlaps(plan, other):
                out.append({k: other[k] for k in ("id", "date", "start_time", "end_time", "station")})
        return out

    def create(self, data: dict) -> tuple[dict, list[dict]]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO plans (date,start_time,end_time,participants,purpose,priority,station,spot,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (data["date"], data["start_time"], data.get("end_time"),
                 json.dumps(data["participants"], ensure_ascii=False), data.get("purpose"),
                 data.get("priority"), data["station"], data.get("spot"), now, now))
            plan = self._row(c.execute("SELECT * FROM plans WHERE id = ?", (cur.lastrowid,)).fetchone())
            return plan, self._conflicts(c, plan, exclude_id=plan["id"])

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM plans ORDER BY date, start_time, id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, plan_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._row(r) if r else None

    def update(self, plan_id: int, patch: dict) -> tuple[dict, list[dict]] | None:
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if not r:
                return None
            plan = self._row(r)
            plan.update({k: v for k, v in patch.items() if k in FIELDS})
            c.execute(
                "UPDATE plans SET date=?,start_time=?,end_time=?,participants=?,purpose=?,priority=?,"
                "station=?,spot=?,updated_at=? WHERE id=?",
                (plan["date"], plan["start_time"], plan.get("end_time"),
                 json.dumps(plan["participants"], ensure_ascii=False), plan.get("purpose"),
                 plan.get("priority"), plan["station"], plan.get("spot"),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), plan_id))
            plan = self._row(c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone())
            return plan, self._conflicts(c, plan, exclude_id=plan_id)

    def delete(self, plan_id: int) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM plans WHERE id = ?", (plan_id,)).rowcount > 0
