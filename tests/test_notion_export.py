import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import tools.notion_export as notion
from tools.notion_export import NotionError, create_database, send_report

KEY = "secret_test_key_123"
URL = "https://app.test/report/abc"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class Recorder:
    """Stands in for requests.post / requests.patch and records each call."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"id": "page-1", "url": "https://notion.so/page-1"})


def sample_report(**overrides):
    report = {
        "domain": "pet care",
        "the_call": {
            "recommended_focus": "Vet bill tracker",
            "for_whom": "Dog owners",
            "why_now": "Bills are rising",
            "validation_step": "Interview five owners",
        },
        "evidence_notice": "Only two platforms were read.",
        "opportunities": [{
            "name": "Vet bill tracker",
            "what": "Tracks vet costs.",
            "evidence_strength": {"level": "Thin", "people": 4, "platforms": ["App Store", "YouTube"]},
            "evidence": [
                {"quote": "I lose every receipt", "platform": "App Store", "url": "https://a.test/1"},
                {"quote": "no good tool", "platform": "YouTube", "url": "http://a.test/2"},
                {"quote": "wish it existed", "platform": "Forum", "url": ""},
            ],
            "pay_signals": {"people": 2, "quotes": []},
            "existing_solutions": {
                "verdict": "partial", "note": "",
                "products": [{"name": "PetPal"}, {"name": "VetBook"}],
            },
        }],
        "evidence_coverage": {
            "items_read": 120, "quotes_verified": 9, "people": 7,
            "not_searched": ["Reddit"], "limits": "Small sample.",
        },
        "cost_usd": 1.25,
        "saved": {"id": "r1", "created_at": "2026-11-10T19:12:00+00:00",
                  "prompt": "What do dog owners need?", "kind": "deep"},
    }
    report.update(overrides)
    return report


def plain(block):
    kind = block["type"]
    return "".join(p["text"]["content"] for p in block[kind]["rich_text"])


def test_create_database_sends_properties_and_returns_id(monkeypatch):
    post = Recorder([FakeResponse(200, {"id": "db-9"})])
    monkeypatch.setattr(notion.requests, "post", post)
    assert create_database(KEY, "parent-1") == "db-9"
    call = post.calls[0]
    assert call["url"] == "https://api.notion.com/v1/databases"
    assert call["timeout"] == 30
    assert call["headers"]["Authorization"] == "Bearer " + KEY
    assert call["headers"]["Notion-Version"] == "2022-06-28"
    body = call["json"]
    assert body["parent"] == {"type": "page_id", "page_id": "parent-1"}
    assert body["title"][0]["text"]["content"] == "Swarm Reports"
    props = body["properties"]
    assert set(props) == {"Question", "Date", "Top idea", "Evidence", "People",
                          "Cost (USD)", "Report", "Kind"}
    assert "title" in props["Question"]
    assert [o["name"] for o in props["Evidence"]["select"]["options"]] == [
        "Strong", "Moderate", "Narrow", "Thin", "None"]
    assert [o["name"] for o in props["Kind"]["select"]["options"]] == ["report", "deep", "tracked"]
    assert props["Cost (USD)"]["number"]["format"] == "dollar"


def test_send_report_builds_properties(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    url = send_report(KEY, "db-1", sample_report(), URL)
    assert url == "https://notion.so/page-1"
    call = post.calls[0]
    assert call["url"] == "https://api.notion.com/v1/pages"
    assert call["json"]["parent"] == {"database_id": "db-1"}
    props = call["json"]["properties"]
    assert props["Question"]["title"][0]["text"]["content"] == "What do dog owners need?"
    assert props["Date"]["date"]["start"] == "2026-11-10"
    assert props["Top idea"]["rich_text"][0]["text"]["content"] == "Vet bill tracker"
    assert props["Evidence"]["select"]["name"] == "Thin"
    assert props["People"]["number"] == 4
    assert props["Cost (USD)"]["number"] == 1.25
    assert props["Report"]["url"] == URL
    assert props["Kind"]["select"]["name"] == "deep"


def test_send_report_defaults_without_opportunities(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    report = sample_report(opportunities=[], saved={})
    send_report(KEY, "db-1", report, URL)
    props = post.calls[0]["json"]["properties"]
    assert props["Question"]["title"][0]["text"]["content"] == "pet care"
    assert props["Top idea"]["rich_text"][0]["text"]["content"] == "Vet bill tracker"
    assert props["Evidence"]["select"]["name"] == "None"
    assert props["People"]["number"] == 0
    assert props["Kind"]["select"]["name"] == "report"
    assert "Date" not in props


def test_block_order_and_text(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    send_report(KEY, "db-1", sample_report(), URL)
    blocks = post.calls[0]["json"]["children"]
    texts = [plain(b) for b in blocks]
    assert blocks[0]["type"] == "callout"
    assert texts[1:6] == ["The call", "Vet bill tracker", "For: Dog owners",
                          "Why now: Bills are rising", "This week: Interview five owners"]
    assert "1. Vet bill tracker" in texts
    assert "Evidence: Thin · 4 people · App Store, YouTube" in texts
    assert "Signs of a future buyer: 2 people" in texts
    assert "Already exists? Partly solved by others: PetPal, VetBook" in texts
    assert "Read 120 posts and comments. 9 quotes verified from 7 people." in texts
    assert "Not searched: Reddit" in texts
    assert texts[-1] == "Open the full report"
    assert blocks[-1]["paragraph"]["rich_text"][0]["text"]["link"]["url"] == URL


def test_quotes_link_only_https(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    send_report(KEY, "db-1", sample_report(), URL)
    bullets = [b for b in post.calls[0]["json"]["children"] if b["type"] == "bulleted_list_item"]
    assert len(bullets) == 3
    platform_parts = [b["bulleted_list_item"]["rich_text"][-1]["text"] for b in bullets]
    assert platform_parts[0]["link"]["url"] == "https://a.test/1"
    assert "link" not in platform_parts[1]  # http://
    assert "link" not in platform_parts[2]  # empty
    assert plain(bullets[0]) == '"I lose every receipt" — App Store'


def test_quotes_capped_at_five(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    report = sample_report()
    report["opportunities"][0]["evidence"] = [
        {"quote": "q%d" % i, "platform": "X", "url": "https://x.test"} for i in range(9)]
    send_report(KEY, "db-1", report, URL)
    bullets = [b for b in post.calls[0]["json"]["children"] if b["type"] == "bulleted_list_item"]
    assert len(bullets) == 5


def test_long_text_is_split(monkeypatch):
    post = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    report = sample_report()
    report["evidence_coverage"]["limits"] = "x" * 4500
    send_report(KEY, "db-1", report, URL)
    blocks = post.calls[0]["json"]["children"]
    long_block = [b for b in blocks if plain(b).startswith("xxx")][0]
    parts = long_block["paragraph"]["rich_text"]
    assert [len(p["text"]["content"]) for p in parts] == [2000, 2000, 500]
    for block in blocks:
        for part in block[block["type"]]["rich_text"]:
            assert len(part["text"]["content"]) <= 2000


def test_more_than_100_blocks_uses_patch(monkeypatch):
    post = Recorder()
    patch = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    monkeypatch.setattr(notion.requests, "patch", patch)
    report = sample_report()
    report["opportunities"] = [dict(report["opportunities"][0], name="Idea %d" % i)
                               for i in range(60)]
    total = len(notion.build_blocks(report, URL))
    assert total > 200
    send_report(KEY, "db-1", report, URL)
    assert len(post.calls) == 1
    assert len(post.calls[0]["json"]["children"]) == 100
    assert len(patch.calls) == (total - 100 + 99) // 100
    assert all(c["url"] == "https://api.notion.com/v1/blocks/page-1/children" for c in patch.calls)
    assert all(len(c["json"]["children"]) <= 100 for c in patch.calls)
    sent = len(post.calls[0]["json"]["children"]) + sum(len(c["json"]["children"]) for c in patch.calls)
    assert sent == total


def test_small_report_makes_no_patch(monkeypatch):
    post = Recorder()
    patch = Recorder()
    monkeypatch.setattr(notion.requests, "post", post)
    monkeypatch.setattr(notion.requests, "patch", patch)
    send_report(KEY, "db-1", sample_report(), URL)
    assert patch.calls == []


def test_error_response_raises_without_key(monkeypatch):
    post = Recorder([FakeResponse(401, {"message": "API token is invalid."})])
    monkeypatch.setattr(notion.requests, "post", post)
    with pytest.raises(NotionError) as err:
        send_report(KEY, "db-1", sample_report(), URL)
    assert "API token is invalid." in str(err.value)
    assert "401" in str(err.value)
    assert KEY not in str(err.value)


def test_error_without_json_body(monkeypatch):
    class BadBody(FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(notion.requests, "post", Recorder([BadBody(500)]))
    with pytest.raises(NotionError) as err:
        create_database(KEY, "parent-1")
    assert "500" in str(err.value)
    assert KEY not in str(err.value)



def test_buyer_signals_show_their_breakdown():
    report = {"opportunities": [{"name": "Idea", "evidence_strength": {"level": "Thin", "people": 3},
                                 "buyer_signals": {"people": 3, "kinds": {"workaround": 2, "tried_products": 1}}}]}
    texts = ["".join(p["text"]["content"] for p in b[b["type"]]["rich_text"]) for b in notion.build_blocks(report, "")]
    assert "Signs of a future buyer: 3 people (2 built a workaround, 1 tried other products)" in texts
