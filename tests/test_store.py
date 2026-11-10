import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools import store
from tools.compare import compare_reports


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "swarm.db"))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(store, "_now", lambda: c.now)
    return c


def make_report(name="Budget tracker", level="Moderate", cost=0.0, opportunities=None):
    if opportunities is None:
        opportunities = [
            {"name": name, "what": "track spending", "who": "students",
             "evidence_strength": {"level": level, "people": 4}},
            {"name": "Second idea", "what": "other", "who": "others",
             "evidence_strength": {"level": "Thin", "people": 1}},
        ]
    return {"domain": "Test", "opportunities": opportunities, "cost_usd": cost}


# ── Reports ─────────────────────────────────────────

def test_save_then_get_report_returns_report_with_saved_meta():
    report = make_report(name="Budget tracker", level="Strong")
    report_id = store.save_report(report, "Budget apps for students")

    got = store.get_report(report_id)

    assert got["domain"] == "Test"
    assert got["opportunities"][0]["name"] == "Budget tracker"
    saved = got["saved"]
    assert saved["id"] == report_id
    assert saved["prompt"] == "Budget apps for students"
    assert saved["kind"] == "report"
    assert saved["top_idea"] == "Budget tracker"
    assert saved["level"] == "Strong"


def test_get_report_unknown_id_returns_none():
    assert store.get_report("nope") is None


def test_list_reports_keeps_report_deep_tracked_newest_first_and_skips_api(clock):
    first = store.save_report(make_report(), "Prompt one", kind="report")
    clock.advance(seconds=60)
    api = store.save_report(make_report(), "Prompt two", kind="api")
    clock.advance(seconds=60)
    deep = store.save_report(make_report(), "Prompt one", kind="deep", parent_id=first)
    clock.advance(seconds=60)
    tracked = store.save_report(make_report(), "Prompt three", kind="tracked")

    listed = store.list_reports()

    ids = [r["id"] for r in listed]
    assert ids == [tracked, deep, first]
    assert api not in ids
    assert {r["kind"] for r in listed} == {"report", "deep", "tracked"}


def test_delete_report_removes_it_and_unknown_id_returns_false():
    report_id = store.save_report(make_report(), "Budget apps for students")

    assert store.delete_report(report_id) is True
    assert store.get_report(report_id) is None
    assert store.delete_report(report_id) is False
    assert store.delete_report("never-existed") is False


def test_previous_report_matches_question_despite_case_and_punctuation(clock):
    first = store.save_report(make_report(), "Budget apps for students", kind="report")
    clock.advance(seconds=60)
    deep = store.save_report(make_report(), "Budget apps for students", kind="deep", parent_id=first)
    clock.advance(seconds=60)
    second = store.save_report(make_report(), "budget apps, for students?", kind="report")

    previous = store.previous_report(second)

    assert previous is not None
    assert previous["saved"]["id"] == first
    assert previous["saved"]["id"] != deep


def test_previous_report_is_none_for_first_run(clock):
    first = store.save_report(make_report(), "Budget apps for students", kind="report")
    assert store.previous_report(first) is None


# ── Daily spending ──────────────────────────────────

def test_record_spend_and_spent_today_add_up_and_ignore_non_positive(monkeypatch):
    store.record_spend(0.5, "report: a")
    store.record_spend(0.25, "deep: b")
    store.record_spend(0, "zero")
    store.record_spend(-1.0, "negative")

    assert store.spent_today() == pytest.approx(0.75)


def test_spending_can_run_turns_false_when_reserve_would_pass_limit(monkeypatch):
    monkeypatch.setattr(store, "DAILY_LIMIT_USD", 0.2)

    assert store.spending()["can_run"] is True

    store.record_spend(0.1, "report: a")
    s = store.spending()

    assert s["spent_today"] == pytest.approx(0.1)
    assert s["daily_limit"] == 0.2
    assert s["can_run"] is False  # 0.1 + 0.15 > 0.2


# ── Tracked questions ───────────────────────────────

def test_create_tracker_is_idempotent_across_case_and_punctuation():
    report_id = store.save_report(make_report(), "Budget apps for students")

    first = store.create_tracker("Budget apps for students", report_id)
    second = store.create_tracker("budget apps, for students?", report_id)

    assert first == second
    assert len(store.list_trackers()) == 1


def test_tracker_for_finds_tracker_and_stop_tracker_deactivates_it():
    report_id = store.save_report(make_report(), "Budget apps for students")
    tracker_id = store.create_tracker("Budget apps for students", report_id)

    found = store.tracker_for("BUDGET APPS FOR STUDENTS!")
    assert found is not None
    assert found["id"] == tracker_id

    assert store.stop_tracker(tracker_id) is True
    assert store.tracker_for("Budget apps for students") is None
    assert store.list_trackers() == []


def test_due_trackers_only_once_next_run_is_past(clock):
    report_id = store.save_report(make_report(), "Budget apps for students")
    tracker_id = store.create_tracker("Budget apps for students", report_id)

    assert store.due_trackers() == []

    clock.advance(days=31)
    due = store.due_trackers()
    assert [t["id"] for t in due] == [tracker_id]

    store.mark_tracker_run(tracker_id, retry_hours=6)
    assert store.due_trackers() == []


def test_mark_tracker_run_retry_and_success_schedule(clock):
    report_id = store.save_report(make_report(), "Budget apps for students")
    tracker_id = store.create_tracker("Budget apps for students", report_id)

    def next_run():
        return datetime.fromisoformat(store.tracker_for("Budget apps for students")["next_run"])

    now = clock.now

    store.mark_tracker_run(tracker_id, retry_hours=6)
    assert abs((next_run() - (now + timedelta(hours=6))).total_seconds()) < 2

    store.mark_tracker_run(tracker_id, "run-report-id")
    assert abs((next_run() - (now + timedelta(days=30))).total_seconds()) < 2
    assert store.tracker_for("Budget apps for students")["last_report_id"] == "run-report-id"


# ── Settings ────────────────────────────────────────

def test_get_setting_missing_is_none_and_set_setting_overwrites():
    assert store.get_setting("notion_database_id") is None

    store.set_setting("notion_database_id", "abc")
    assert store.get_setting("notion_database_id") == "abc"

    store.set_setting("notion_database_id", "def")
    assert store.get_setting("notion_database_id") == "def"


# ── Compare ─────────────────────────────────────────

def test_compare_reports_matches_ideas_and_counts_new_quotes():
    old = {
        "saved": {"id": "old1", "created_at": "2026-03-01T12:00:00+00:00"},
        "opportunities": [
            {"name": "Budget tracker for students",
             "what": "Track monthly spending and savings goals",
             "who": "students",
             "evidence": [{"quote": "q1", "url": "https://x.test/old"}]},
            {"name": "Meal planning for families",
             "what": "Weekly dinner menus and recipes",
             "who": "families",
             "evidence": []},
        ],
    }
    new = {
        "saved": {"id": "new1", "created_at": "2026-03-08T12:00:00+00:00"},
        "opportunities": [
            {"name": "Student budget tracking app",
             "what": "Track monthly spending and savings goals",
             "who": "students",
             "evidence": [
                 {"quote": "q1 again", "platform": "Reddit", "url": "https://x.test/old", "created": "2026-03-07"},
                 {"quote": "q2", "platform": "HN", "url": "https://x.test/new1", "created": "2026-03-07"},
                 {"quote": "q3", "platform": "GitHub", "url": "https://x.test/new2", "created": "2026-03-07"},
             ]},
            {"name": "Shared grocery lists for roommates",
             "what": "Shared lists for splitting groceries",
             "who": "roommates",
             "evidence": []},
        ],
    }

    diff = compare_reports(old, new)

    assert diff["gone_ideas"] == ["Meal planning for families"]
    assert diff["new_ideas"] == ["Shared grocery lists for roommates"]
    assert diff["continuing"] == ["Student budget tracking app"]
    assert diff["new_quotes"] == 2
    assert diff["previous_id"] == "old1"
    assert diff["previous_date"] == "2026-03-01T12:00:00+00:00"
