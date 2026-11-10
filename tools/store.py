"""
store.py — saved reports, daily spending and tracked questions, in one SQLite file.

The file lives in data/swarm.db (not in git). Every function opens its own
connection, so the web server's threads and the tracker thread can share it.
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("SWARM_DB", os.path.join(REPO_ROOT, "data", "swarm.db"))
LOCAL_TZ = ZoneInfo("America/Vancouver")  # the owner's day, for the daily limit
DAILY_LIMIT_USD = float(os.environ.get("DAILY_LIMIT_USD", "3.0"))
RUN_RESERVE_USD = 0.15  # refuse a run that could push the day past the limit
TRACK_EVERY_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    prompt_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT,
    tracker_id TEXT,
    top_idea TEXT,
    level TEXT,
    cost REAL,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_by_prompt ON reports (prompt_key, created_at);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    amount REAL NOT NULL,
    label TEXT
);
CREATE TABLE IF NOT EXISTS trackers (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    every_days INTEGER NOT NULL,
    next_run TEXT NOT NULL,
    last_report_id TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

LISTED_KINDS = ("report", "deep", "tracked")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def prompt_key(prompt: str) -> str:
    """Treat the same question typed slightly differently as the same question."""
    return re.sub(r"[^a-z0-9]+", " ", (prompt or "").lower()).strip()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Reports ─────────────────────────────────────────

def save_report(report: dict, prompt: str, kind: str = "report", parent_id: str = None,
                tracker_id: str = None) -> str:
    report_id = new_id()
    opps = report.get("opportunities") or []
    top = opps[0] if opps else {}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO reports (id, created_at, prompt, prompt_key, kind, parent_id, tracker_id, "
            "top_idea, level, cost, report_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_id, _now().isoformat(timespec="seconds"), prompt, prompt_key(prompt), kind,
                parent_id, tracker_id,
                top.get("name") or (report.get("the_call") or {}).get("recommended_focus", ""),
                (top.get("evidence_strength") or {}).get("level", "None"),
                report.get("cost_usd", 0.0), json.dumps(report, ensure_ascii=False),
            ),
        )
    return report_id


def _meta(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in ("id", "created_at", "prompt", "kind", "parent_id", "tracker_id",
                                "top_idea", "level", "cost")}


def get_report(report_id: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return None
    report = json.loads(row["report_json"])
    report["saved"] = _meta(row)
    return report


def list_reports(limit: int = 40) -> list:
    marks = ",".join("?" * len(LISTED_KINDS))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM reports WHERE kind IN ({marks}) ORDER BY created_at DESC LIMIT ?",
            (*LISTED_KINDS, limit),
        ).fetchall()
    return [_meta(r) for r in rows]


def delete_report(report_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM reports WHERE id = ?", (report_id,)).rowcount > 0


def previous_report(report_id: str):
    """The last saved run of the same question before this one, if any."""
    with _connect() as conn:
        row = conn.execute("SELECT prompt_key, created_at FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            return None
        marks = ",".join("?" * len(LISTED_KINDS))
        prev = conn.execute(
            f"SELECT id FROM reports WHERE prompt_key = ? AND created_at < ? AND id != ? "
            f"AND kind IN ({marks}) AND kind != 'deep' ORDER BY created_at DESC LIMIT 1",
            (row["prompt_key"], row["created_at"], report_id, *LISTED_KINDS),
        ).fetchone()
    return get_report(prev["id"]) if prev else None


# ── Daily spending ──────────────────────────────────

def _today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def record_spend(amount: float, label: str) -> None:
    if amount <= 0:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO spend (at, day, amount, label) VALUES (?,?,?,?)",
            (_now().isoformat(timespec="seconds"), _today(), round(amount, 5), label[:200]),
        )


def spent_today() -> float:
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM spend WHERE day = ?", (_today(),)).fetchone()
    return round(row["total"], 4)


def spending() -> dict:
    spent = spent_today()
    return {
        "spent_today": spent,
        "daily_limit": DAILY_LIMIT_USD,
        "can_run": spent + RUN_RESERVE_USD <= DAILY_LIMIT_USD,
    }


# ── Tracked questions ───────────────────────────────

def create_tracker(prompt: str, last_report_id: str, every_days: int = TRACK_EVERY_DAYS) -> str:
    key = prompt_key(prompt)
    with _connect() as conn:
        for row in conn.execute("SELECT id, prompt FROM trackers WHERE active = 1").fetchall():
            if prompt_key(row["prompt"]) == key:
                return row["id"]  # already tracked
        tracker_id = new_id()
        conn.execute(
            "INSERT INTO trackers (id, prompt, created_at, every_days, next_run, last_report_id, active) "
            "VALUES (?,?,?,?,?,?,1)",
            (tracker_id, prompt, _now().isoformat(timespec="seconds"), every_days,
             (_now() + timedelta(days=every_days)).isoformat(timespec="seconds"), last_report_id),
        )
    return tracker_id


def list_trackers() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM trackers WHERE active = 1 ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def tracker_for(prompt: str):
    key = prompt_key(prompt)
    return next((t for t in list_trackers() if prompt_key(t["prompt"]) == key), None)


def stop_tracker(tracker_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("UPDATE trackers SET active = 0 WHERE id = ?", (tracker_id,)).rowcount > 0


def due_trackers() -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trackers WHERE active = 1 AND next_run <= ?", (_now().isoformat(timespec="seconds"),)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_tracker_run(tracker_id: str, report_id: str = None, retry_hours: int = 0) -> None:
    """Schedule the next run. A failed or skipped run retries sooner instead of waiting a month."""
    with _connect() as conn:
        row = conn.execute("SELECT every_days FROM trackers WHERE id = ?", (tracker_id,)).fetchone()
        if not row:
            return
        delay = timedelta(hours=retry_hours) if retry_hours else timedelta(days=row["every_days"])
        conn.execute(
            "UPDATE trackers SET next_run = ?, last_report_id = COALESCE(?, last_report_id) WHERE id = ?",
            ((_now() + delay).isoformat(timespec="seconds"), report_id, tracker_id),
        )


# ── Settings ────────────────────────────────────────

def get_setting(key: str):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
