"""
notion_export.py — send a saved report to a Notion database called "Swarm Reports".

  1. create_database() makes the database once, under a page you choose.
  2. send_report() adds one page per report, with the key findings as page content.

Notion limits respected here: 2000 characters per rich_text object, 100 blocks per
request, and links only on https:// URLs. The API key is never put in an error.
"""

import requests

NOTION_VERSION = "2022-06-28"
DATABASE_TITLE = "Swarm Reports"
API_BASE = "https://api.notion.com/v1"
TIMEOUT_SEC = 30
MAX_TEXT_CHARS = 2000
MAX_BLOCKS_PER_REQUEST = 100
MAX_QUOTES_PER_IDEA = 5

EVIDENCE_LEVELS = ("Strong", "Moderate", "Narrow", "Thin", "None")
KINDS = ("report", "deep", "tracked")

VERDICT_WORDING = {
    "covered": "Already solved by others",
    "partial": "Partly solved by others",
    "none_found": "No direct solution found",
    "unknown": "Could not tell",
}


class NotionError(Exception):
    """Notion refused a request, or could not be reached."""


# ---------- HTTP ----------

def _headers(api_key):
    return {
        "Authorization": "Bearer " + api_key,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _check(resp):
    """Return the JSON body, or raise NotionError with Notion's own message."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if not 200 <= resp.status_code < 300:
        message = body.get("message") or "request failed"
        raise NotionError("Notion error %s: %s" % (resp.status_code, message))
    return body


def _call(method, url, api_key, payload):
    """Send one request. Network errors are reported without the URL or headers."""
    try:
        resp = method(url, headers=_headers(api_key), json=payload, timeout=TIMEOUT_SEC)
    except requests.RequestException as exc:
        raise NotionError("Could not reach Notion (%s)" % type(exc).__name__)
    return _check(resp)


# ---------- rich text and blocks ----------

def rich_text(text, link=None):
    """Split text into rich_text objects of at most 2000 characters each."""
    text = text or ""
    href = link if isinstance(link, str) and link.startswith("https://") else None
    parts = []
    for i in range(0, len(text), MAX_TEXT_CHARS):
        content = {"content": text[i:i + MAX_TEXT_CHARS]}
        if href:
            content["link"] = {"url": href}
        parts.append({"type": "text", "text": content})
    return parts


def _block(kind, *segments):
    """A block of the given type; each segment is a rich_text list."""
    rich = [part for seg in segments for part in seg]
    return {"object": "block", "type": kind, kind: {"rich_text": rich}}


def _paragraph(text, link=None):
    return _block("paragraph", rich_text(text, link))


def _text_of(value):
    return value if isinstance(value, str) else ""


def _opportunity_blocks(n, opp):
    """Heading, summary, evidence line, quotes and 'already exists' line for one idea."""
    blocks = [_block("heading_3", rich_text("%d. %s" % (n, _text_of(opp.get("name")))))]
    if opp.get("what"):
        blocks.append(_paragraph(_text_of(opp["what"])))

    strength = opp.get("evidence_strength") or {}
    line = ["Evidence: %s" % (strength.get("level") or "None")]
    if strength.get("people") is not None:
        line.append("%s people" % strength["people"])
    if strength.get("platforms"):
        line.append(", ".join(str(p) for p in strength["platforms"]))
    blocks.append(_paragraph(" · ".join(line)))

    pay = opp.get("pay_signals") or {}
    if pay.get("people"):
        blocks.append(_paragraph("Signs people would pay: %s people" % pay["people"]))

    for ev in (opp.get("evidence") or [])[:MAX_QUOTES_PER_IDEA]:
        blocks.append(_block(
            "bulleted_list_item",
            rich_text('"%s"' % _text_of(ev.get("quote"))),
            rich_text(" — "),
            rich_text(_text_of(ev.get("platform")), ev.get("url")),
        ))

    solutions = opp.get("existing_solutions")
    if solutions:
        wording = VERDICT_WORDING.get(solutions.get("verdict"), VERDICT_WORDING["unknown"])
        names = ", ".join(p.get("name", "") for p in solutions.get("products") or [] if p.get("name"))
        blocks.append(_paragraph("Already exists? %s%s" % (wording, ": " + names if names else "")))
    return blocks


def build_blocks(report, report_url):
    """Turn a report into the ordered list of Notion blocks for the page body."""
    blocks = []
    if report.get("evidence_notice"):
        blocks.append(_block("callout", rich_text(_text_of(report["evidence_notice"]))))

    call = report.get("the_call") or {}
    blocks.append(_block("heading_2", rich_text("The call")))
    if call.get("recommended_focus"):
        blocks.append(_paragraph(_text_of(call["recommended_focus"])))
    for label, key in (("For", "for_whom"), ("Why now", "why_now"), ("This week", "validation_step")):
        if call.get(key):
            blocks.append(_paragraph("%s: %s" % (label, _text_of(call[key]))))

    for n, opp in enumerate(report.get("opportunities") or [], start=1):
        blocks.extend(_opportunity_blocks(n, opp))

    cov = report.get("evidence_coverage") or {}
    blocks.append(_block("heading_2", rich_text("What this report read")))
    blocks.append(_paragraph("Read %s posts and comments. %s quotes verified from %s people." % (
        cov.get("items_read", 0), cov.get("quotes_verified", 0), cov.get("people", 0))))
    if cov.get("not_searched"):
        blocks.append(_paragraph("Not searched: " + "; ".join(str(s) for s in cov["not_searched"])))
    if cov.get("limits"):
        blocks.append(_paragraph(_text_of(cov["limits"])))

    blocks.append(_paragraph("Open the full report", report_url))
    return blocks


# ---------- properties ----------

def _page_properties(database_report, report_url):
    """The database row for this report."""
    report = database_report
    saved = report.get("saved") or {}
    opps = report.get("opportunities") or []
    first = opps[0] if opps else {}
    strength = first.get("evidence_strength") or {}

    question = saved.get("prompt") or report.get("domain") or "Untitled report"
    top_idea = first.get("name") or (report.get("the_call") or {}).get("recommended_focus") or ""
    level = strength.get("level") if strength.get("level") in EVIDENCE_LEVELS else "None"
    kind = saved.get("kind") if saved.get("kind") in KINDS else "report"

    props = {
        "Question": {"title": rich_text(question)},
        "Top idea": {"rich_text": rich_text(top_idea)},
        "Evidence": {"select": {"name": level}},
        "People": {"number": strength.get("people") or 0},
        "Cost (USD)": {"number": report.get("cost_usd") or 0},
        "Report": {"url": report_url},
        "Kind": {"select": {"name": kind}},
    }
    created = saved.get("created_at")
    if isinstance(created, str) and len(created) >= 10:
        props["Date"] = {"date": {"start": created[:10]}}
    return props


# ---------- public API ----------

def create_database(api_key, parent_page_id):
    """Create the Swarm Reports database under the given page and return its id."""
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
        "properties": {
            "Question": {"title": {}},
            "Date": {"date": {}},
            "Top idea": {"rich_text": {}},
            "Evidence": {"select": {"options": [{"name": n} for n in EVIDENCE_LEVELS]}},
            "People": {"number": {}},
            "Cost (USD)": {"number": {"format": "dollar"}},
            "Report": {"url": {}},
            "Kind": {"select": {"options": [{"name": n} for n in KINDS]}},
        },
    }
    return _call(requests.post, API_BASE + "/databases", api_key, payload).get("id", "")


def send_report(api_key, database_id, report, report_url):
    """Create one page in the database for this report and return the new page's URL.

    Notion accepts 100 blocks per request: the first 100 go in the create call,
    the rest are appended in batches of 100.
    """
    blocks = build_blocks(report, report_url)
    payload = {
        "parent": {"database_id": database_id},
        "properties": _page_properties(report, report_url),
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    }
    page = _call(requests.post, API_BASE + "/pages", api_key, payload)

    rest = blocks[MAX_BLOCKS_PER_REQUEST:]
    for i in range(0, len(rest), MAX_BLOCKS_PER_REQUEST):
        _call(requests.patch, "%s/blocks/%s/children" % (API_BASE, page.get("id", "")),
              api_key, {"children": rest[i:i + MAX_BLOCKS_PER_REQUEST]})
    return page.get("url", "")
