"""
compare.py — what changed between two runs of the same question.

Idea names are written by a model and change wording between runs, so ideas
are matched by shared words rather than exact names. Quotes are matched by
their link, which does not change.
"""

import re

_STOP = {
    "with", "that", "this", "from", "your", "their", "they", "them", "have", "into", "about", "than",
    "more", "most", "less", "what", "when", "where", "which", "while", "will", "would", "could", "should",
    "app", "apps", "tool", "tools", "platform", "service", "based", "first", "build", "make", "help",
}
SIMILARITY = 0.3


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in _STOP}


def _label(opp: dict) -> set:
    return _words(f"{opp.get('name', '')} {opp.get('what', '')}")


def _similar(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= SIMILARITY


def _quote_links(report: dict) -> set:
    return {
        q.get("url")
        for opp in report.get("opportunities") or []
        for q in (opp.get("evidence") or []) + ((opp.get("pay_signals") or {}).get("quotes") or [])
        if q.get("url")
    }


def compare_reports(old: dict, new: dict) -> dict:
    old_opps, new_opps = old.get("opportunities") or [], new.get("opportunities") or []
    old_labels = [_label(o) for o in old_opps]
    new_labels = [_label(o) for o in new_opps]

    new_ideas = [o.get("name", "") for o, words in zip(new_opps, new_labels)
                 if not any(_similar(words, w) for w in old_labels)]
    gone_ideas = [o.get("name", "") for o, words in zip(old_opps, old_labels)
                  if not any(_similar(words, w) for w in new_labels)]
    continuing = [o.get("name", "") for o, words in zip(new_opps, new_labels)
                  if any(_similar(words, w) for w in old_labels)]

    seen = _quote_links(old)
    fresh = [
        {k: q.get(k) for k in ("quote", "platform", "url", "created")}
        for opp in new_opps for q in opp.get("evidence") or []
        if q.get("url") and q["url"] not in seen
    ]
    return {
        "previous_id": (old.get("saved") or {}).get("id"),
        "previous_date": (old.get("saved") or {}).get("created_at"),
        "new_ideas": new_ideas,
        "gone_ideas": gone_ideas,
        "continuing": continuing,
        "new_quotes": len(fresh),
        "new_quote_examples": fresh[:3],
    }
