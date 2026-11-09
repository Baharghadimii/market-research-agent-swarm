import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.evidence import (
    apply_evidence,
    coverage_summary,
    evidence_stats,
    parse_pasted,
    clean_html,
    discourse_posts,
    person_key,
    quote_is_verbatim,
    strength_of,
    verify_extractions,
)

import pytest

import tools.evidence as evidence


def row(i, author="ann", platform="GitHub", status="open"):
    return {
        "id": i, "author": author, "platform": platform, "status": status,
        "quote": "the export keeps failing", "url": "https://x.test/1",
        "created": "2026-01-01", "kind": "complaint", "theme": "export",
        "text": "the export keeps failing on large files",
    }


def test_quote_must_really_appear():
    text = "I’ve tried   everything and the export keeps failing."
    assert quote_is_verbatim("I've tried everything and the EXPORT keeps failing", text)
    assert not quote_is_verbatim("the export always fails for everyone", text)
    assert not quote_is_verbatim("export", text)


def test_clean_html_removes_text_quoted_from_others():
    raw = "<blockquote>someone else's words</blockquote><p>my own &amp; words</p><pre>code</pre>"
    assert clean_html(raw) == "my own & words"


def test_verify_rejects_invented_quotes_and_sellers():
    store = {
        "E1": {"id": "E1", "text": "the export keeps failing on large files"},
        "E2": {"id": "E2", "text": "hire me, I fix export problems quickly and cheaply"},
        "E3": {"id": "E3", "text": "love this tool, it has been very good for our team"},
    }
    verified, counts = verify_extractions([
        {"item_id": "E1", "quote": "the export keeps failing", "kind": "complaint", "theme": "export"},
        {"item_id": "E1", "quote": "exports are totally unusable now", "kind": "complaint"},
        {"item_id": "E9", "quote": "the export keeps failing", "kind": "complaint"},
        {"item_id": "E2", "quote": "I fix export problems quickly", "kind": "complaint"},
        {"item_id": "E3", "quote": "very good for our team", "kind": "seller"},
    ], store)
    assert list(verified) == ["E1"]
    assert counts == {"extracted": 5, "rejected_unverified": 2, "filtered_sellers": 2}


def test_same_person_counts_once_and_unknowns_collapse():
    assert person_key({"author": " Ann ", "platform": "GitHub"}) == person_key({"author": "ann", "platform": "GitHub"})
    assert person_key({"author": "ann", "platform": "GitHub"}) != person_key({"author": "ann", "platform": "App Store"})
    assert person_key({"author": "", "platform": "X"}) == person_key({"author": "[deleted]", "platform": "X"})


def test_strength_needs_people_and_platforms():
    assert strength_of(40, 1) == "Narrow"
    assert strength_of(3, 2) == "Thin"
    assert strength_of(5, 2) == "Moderate"
    assert strength_of(15, 3) == "Strong"


def report_with(*ids_per_opportunity):
    return {
        "the_call": {"recommended_focus": "first idea"},
        "watch_list": [],
        "opportunities": [
            {"name": f"Opp {n}", "what": f"idea {n}", "who": "w", "timing": "t",
             "validation_plan": ["step"], "evidence_ids": ids}
            for n, ids in enumerate(ids_per_opportunity)
        ],
    }


def test_invented_ids_are_ignored_and_counts_come_from_code():
    verified = {"E1": row("E1"), "E2": row("E2", author="ann")}
    report = apply_evidence(report_with(["E1", "E2", "E99"]), verified)
    opp = report["opportunities"][0]
    assert opp["evidence_strength"]["people"] == 1
    assert opp["evidence_strength"]["mentions"] == 2
    assert report["evidence_notice"].startswith("Evidence is thin")


def test_unsupported_top_opportunity_cannot_stay_the_recommendation():
    verified = {"E1": row("E1")}
    report = apply_evidence(report_with([], ["E1"]), verified)
    assert [o["name"] for o in report["opportunities"]] == ["Opp 1"]
    assert report["the_call"]["recommended_focus"] == "idea 1"
    assert "Opp 0" in report["watch_list"][0]


def test_internal_ids_do_not_reach_readers():
    report = report_with(["E1"])
    report["watch_list"] = ["Too simple [E5], too advanced [E7, E8]."]
    report = apply_evidence(report, {"E1": row("E1")})
    assert report["watch_list"] == ["Too simple, too advanced."]


def test_no_evidence_means_no_recommendation():
    report = apply_evidence(report_with(["E5"]), {})
    assert report["opportunities"] == []
    assert report["the_call"]["recommended_focus"].startswith("Cannot make a call")


def test_forum_host_must_be_on_the_list():
    with pytest.raises(ValueError):
        discourse_posts("169.254.169.254", "anything")


def test_old_and_happy_app_reviews_are_dropped(monkeypatch):
    def review(rating, date):
        return {"im:rating": {"label": str(rating)}, "updated": {"label": date},
                "title": {"label": "t"}, "content": {"label": "body"},
                "author": {"name": {"label": "a"}}}

    recent = (evidence._cutoff() + evidence.timedelta(days=1)).strftime("%Y-%m-%d")
    feed = {"feed": {"entry": [review(1, recent), review(5, recent), review(1, "2020-01-01")]}}
    search = {"results": [{"trackId": 1, "trackName": "App", "trackViewUrl": "https://x.test"}]}
    monkeypatch.setattr(evidence, "_get", lambda url, *a, **k: search if "search" in url else feed)
    assert len(evidence.app_store_reviews("App", apps=1)) == 1


def test_pasted_text_splits_on_blank_lines_and_keeps_links():
    raw = (
        "This app charged me twice and support never replied.\nhttps://www.reddit.com/r/x/comments/1/a\n\n"
        "ok\n\n"
        "I gave up and made my own spreadsheet to track it all."
    )
    items, unused = parse_pasted("reddit", raw)
    assert len(items) == 2 and unused == 1
    assert items[0].url == "https://www.reddit.com/r/x/comments/1/a"
    assert "reddit.com" not in items[0].text
    assert items[1].url == ""
    assert items[0].platform == "Reddit (pasted by you)"
    assert items[0].author != items[1].author


def test_pasted_text_removes_contact_details_and_foreign_links():
    text = "Email me at jane.doe@example.com or call 604-555-0199 about this awful app https://evil.test/x"
    item = parse_pasted("facebook", text)[0][0]
    assert "jane.doe" not in item.text and "555" not in item.text
    assert item.url == ""


def test_pasted_alone_cannot_be_strong():
    rows = {}
    for n in range(10):
        rows[f"A{n}"] = row(f"A{n}", author=f"a{n}", platform="Reddit (pasted by you)")
        rows[f"B{n}"] = row(f"B{n}", author=f"b{n}", platform="Facebook (pasted by you)")
    rows["C"] = row("C", author="c", platform="GitHub")
    stats = evidence_stats(list(rows), rows)
    assert stats["level"] == "Moderate"
    assert stats["pasted"] == 20


def test_report_warns_about_pasted_evidence():
    rows = {"E1": row("E1", platform="Reddit (pasted by you)")}
    report = apply_evidence(report_with(["E1"]), rows)
    assert "could not be independently checked" in report["evidence_notice"]


def test_pasted_only_idea_cannot_outrank_checked_evidence():
    rows = {"E1": row("E1", platform="Reddit (pasted by you)"), "E2": row("E2", author="bo")}
    report = apply_evidence(report_with(["E1"], ["E2"]), rows)
    assert [o["name"] for o in report["opportunities"]] == ["Opp 1", "Opp 0"]
    assert report["the_call"]["recommended_focus"] == "idea 1"


def test_coverage_says_reddit_was_only_pasted():
    log = [{"type": "pasted", "target": "Reddit", "items": 3}]
    cov = coverage_summary(log, {}, {}, {})
    assert "Reddit (only the comments you pasted)" in cov["not_searched"]
    assert "Facebook (groups are private, no API)" in cov["not_searched"]
    assert "Facebook (only the comments you pasted)" in coverage_summary(
        [{"type": "pasted", "target": "Facebook", "items": 1}], {}, {}, {}
    )["not_searched"]
    assert "not against the original site" in cov["limits"]
