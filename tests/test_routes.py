import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv
import pytest

# server.py calls load_dotenv() at import time, which would read the real .env.
# Skip that so the tests never see real keys.
# server.py also builds an OpenAI client at import, which refuses an empty key.
# Give it a dummy key for the import only; nothing here makes a model call.
_real_load_dotenv = dotenv.load_dotenv
_saved_key = os.environ.get("OPENROUTER_API_KEY")
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ["OPENROUTER_API_KEY"] = "test-dummy-key"
try:
    import server  # noqa: E402
finally:
    dotenv.load_dotenv = _real_load_dotenv
    if _saved_key is None:
        os.environ.pop("OPENROUTER_API_KEY", None)
    else:
        os.environ["OPENROUTER_API_KEY"] = _saved_key

from tools import store  # noqa: E402

PROMPT = "Budget apps for students"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "swarm.db"))


@pytest.fixture(autouse=True)
def clear_jobs():
    server.JOBS.clear()
    yield
    server.JOBS.clear()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(store, "_now", lambda: c.now)
    return c


def fake_pipeline(prompt, pasted=None, focus=None, cost=None, progress=None):
    if progress:
        progress("read", "Read 3 posts")
    if cost:
        cost.add(0.05)
    report = {
        "domain": "Test",
        "the_call": {"recommended_focus": "Build X"},
        "opportunities": [{
            "name": "Idea A", "what": "a", "who": "w", "evidence": [],
            "evidence_strength": {"level": "Thin", "people": 1},
        }],
        "cost_usd": 0.05,
    }
    if focus:
        report["focus"] = focus
    return report


@pytest.fixture(autouse=True)
def stub_pipeline(monkeypatch):
    monkeypatch.setattr(server, "run_pipeline", fake_pipeline)


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


def wait_job(client, job_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} still running after 5 seconds")


def wait_for_spend(client, amount, timeout=5):
    # The job records its spend just after it reports done, so wait for that.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/spending").get_json()["spent_today"] >= amount - 1e-9:
            return
        time.sleep(0.05)
    pytest.fail(f"spending never reached {amount}")


def run_report(client, prompt=PROMPT):
    resp = client.post("/jobs", json={"prompt": prompt})
    assert resp.status_code == 202
    job = wait_job(client, resp.get_json()["job_id"])
    assert job["status"] == "done", job
    return job["report_id"]


# ── Jobs ────────────────────────────────────────────

def test_post_jobs_without_prompt_is_400(client):
    assert client.post("/jobs", json={}).status_code == 400
    assert client.post("/jobs", json={"prompt": "   "}).status_code == 400


def test_job_runs_to_done_and_report_is_saved_and_listed(client):
    resp = client.post("/jobs", json={"prompt": PROMPT})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = wait_job(client, job_id)

    assert job["status"] == "done"
    assert job["report_id"]
    assert job["done_stages"] == ["plan", "read", "check", "write", "extras"]

    report = client.get(f"/reports/{job['report_id']}").get_json()
    assert report["saved"]["kind"] == "report"
    assert report["saved"]["id"] == job["report_id"]
    assert report["saved"]["prompt"] == PROMPT

    index = client.get("/reports").get_json()
    assert job["report_id"] in [r["id"] for r in index["reports"]]

    wait_for_spend(client, 0.05)
    assert client.get("/spending").get_json()["spent_today"] == pytest.approx(0.05)


def test_unknown_job_is_404(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_post_jobs_is_429_when_day_is_at_limit(client, monkeypatch):
    monkeypatch.setattr(store, "DAILY_LIMIT_USD", 0.01)

    resp = client.post("/jobs", json={"prompt": PROMPT})

    assert resp.status_code == 429
    assert "limit" in resp.get_json()["error"].lower()


# ── Synchronous research ────────────────────────────

def test_post_research_returns_api_report_not_listed_and_records_cost(client):
    resp = client.post("/research", json={"prompt": PROMPT})

    assert resp.status_code == 200
    body = resp.get_json()
    report_id = body["saved"]["id"]
    assert report_id
    assert body["opportunities"][0]["name"] == "Idea A"

    listed = client.get("/reports").get_json()["reports"]
    assert report_id not in [r["id"] for r in listed]

    assert client.get("/spending").get_json()["spent_today"] == pytest.approx(0.05)


# ── Deeper runs ─────────────────────────────────────

def test_deeper_rejects_invalid_index(client):
    report_id = run_report(client)

    assert client.post(f"/reports/{report_id}/deeper", json={"index": 5}).status_code == 400
    assert client.post(f"/reports/{report_id}/deeper", json={"index": -1}).status_code == 400
    assert client.post(f"/reports/{report_id}/deeper", json={"index": "0"}).status_code == 400
    assert client.post(f"/reports/{report_id}/deeper", json={}).status_code == 400


def test_deeper_on_index_zero_makes_deep_child_report(client):
    original = run_report(client)

    resp = client.post(f"/reports/{original}/deeper", json={"index": 0})
    assert resp.status_code == 202

    job = wait_job(client, resp.get_json()["job_id"])
    assert job["status"] == "done"

    deep = client.get(f"/reports/{job['report_id']}").get_json()
    assert deep["saved"]["kind"] == "deep"
    assert deep["saved"]["parent_id"] == original
    assert deep["focus"]["name"] == "Idea A"


# ── Trackers ────────────────────────────────────────

def test_track_then_report_shows_tracker_and_delete_stops_it(client):
    report_id = run_report(client)

    resp = client.post(f"/reports/{report_id}/track")
    assert resp.status_code == 200
    tracker_id = resp.get_json()["tracker"]["id"]
    assert tracker_id

    report = client.get(f"/reports/{report_id}").get_json()
    assert report["tracker"] is not None
    assert report["tracker"]["id"] == tracker_id

    assert client.delete(f"/trackers/{tracker_id}").status_code == 200

    report = client.get(f"/reports/{report_id}").get_json()
    assert report["tracker"] is None


# ── Compare ─────────────────────────────────────────

def test_second_run_of_same_question_gets_compare_against_first(client, clock):
    first = run_report(client)
    clock.advance(days=7)
    second = run_report(client, prompt="budget apps, for students?")

    report = client.get(f"/reports/{second}").get_json()

    assert report["compare"] is not None
    assert report["compare"]["previous_id"] == first
    assert report["compare"]["continuing"] == ["Idea A"]


# ── Delete, Notion ──────────────────────────────────

def test_delete_report_then_get_is_404(client):
    report_id = run_report(client)

    resp = client.delete(f"/reports/{report_id}")
    assert resp.status_code == 200
    assert resp.get_json() == {"deleted": True}

    assert client.get(f"/reports/{report_id}").status_code == 404


@pytest.mark.parametrize("present, missing", [
    ("NOTION_PARENT_PAGE_ID", "NOTION_API_KEY"),
    ("NOTION_API_KEY", "NOTION_PARENT_PAGE_ID"),
])
def test_notion_export_is_400_when_setup_is_missing(client, monkeypatch, present, missing):
    report_id = run_report(client)
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setenv(present, "test-value")

    resp = client.post(f"/reports/{report_id}/notion")

    assert resp.status_code == 400
    assert "Notion" in resp.get_json()["error"]



def test_finished_reports_go_to_notion_once(client, monkeypatch):
    sent = []
    monkeypatch.setenv("NOTION_API_KEY", "test-key")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "parent-page")
    monkeypatch.setattr(server, "create_database", lambda key, parent: "db-1")
    monkeypatch.setattr(server, "send_report",
                        lambda key, db, report, url: sent.append((db, url)) or f"https://notion.test/{len(sent)}")
    r = client.post("/jobs", json={"prompt": "Notion test"},
                    headers={"Host": "swarm.example", "X-Forwarded-Proto": "https"})
    job = wait_job(client, r.get_json()["job_id"])
    detail = client.get(f"/reports/{job['report_id']}").get_json()
    assert detail["notion_url"] == "https://notion.test/1"
    assert sent == [("db-1", f"https://swarm.example/#report={job['report_id']}")]
    again = client.post(f"/reports/{job['report_id']}/notion").get_json()
    assert again["url"] == "https://notion.test/1" and len(sent) == 1  # no duplicate page


def test_notion_failure_does_not_break_a_run(client, monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "test-key")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "parent-page")

    def broken(*a, **k):
        raise server.NotionError("Notion error 500: down")

    monkeypatch.setattr(server, "create_database", broken)
    r = client.post("/jobs", json={"prompt": "Notion down"})
    job = wait_job(client, r.get_json()["job_id"])
    assert job["status"] == "done"
    assert client.get(f"/reports/{job['report_id']}").get_json()["notion_url"] is None



def test_salvage_keeps_complete_quotes_from_a_broken_answer():
    raw = ('{"evidence": [{"item_id": "E1", "quote": "first complete quote here", "kind": "complaint"}, '
           '{"item_id": "E2", "quote": "second complete one", "kind": "wish"}, {"item_id": "E3", "quote": "cut of')
    assert [e["item_id"] for e in server.salvage_list(raw, "evidence")] == ["E1", "E2"]
    assert server.salvage_list("no json at all", "evidence") == []


def test_broken_report_json_gets_one_retry(monkeypatch):
    answers = iter(['{"domain": "x", "opportunities": [ {"name": "a" "what": "missing comma"} ]}',
                    '{"domain": "x", "the_call": {}, "opportunities": []}'])

    class Msg:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]

    monkeypatch.setattr(server, "call_llm_with_fallback", lambda models, **k: (Msg(next(answers)), models[0]))
    report, cost = server.run_synthesizer("x", {}, {}, "prompt")
    assert report["domain"] == "x" and report["opportunities"] == []



def test_active_jobs_count_for_safe_deploys(client):
    assert client.get("/jobs/active").get_json()["running"] == 0
    server.JOBS["x"] = {"id": "x", "status": "running", "started": time.time()}
    assert client.get("/jobs/active").get_json()["running"] == 1
