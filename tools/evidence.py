"""
evidence.py — read what people actually wrote, and keep the report honest about it.

Google snippets are fragments, so they are never treated as evidence. This module:
  1. Fetches real posts and comments through public APIs (the connectors).
  2. Checks that every quote a model extracts really appears in the fetched text.
  3. Counts different people instead of mentions.
  4. Attaches those numbers to the report in code, so a model cannot inflate them.

Everything fetched here is untrusted text written by strangers.
"""

import html
import os
import re
import unicodedata
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import requests

USER_AGENT = "market-research-swarm/1.0"
TIMEOUT_SEC = 20
LOOKBACK_DAYS = 540
MAX_TEXT_CHARS = 1500
MAX_CONNECTORS_PER_BRIEF = 4
MAX_QUOTES_SHOWN = 8
MIN_QUOTE_CHARS = 15

# Fixed list, because the planner is a model and must not choose arbitrary hosts
# for the server to call. Every host here answered /search.json on 2026-11-09.
DISCOURSE_FORUMS = (
    "community.fly.io",
    "community.home-assistant.io",
    "community.make.com",
    "community.n8n.io",
    "community.openai.com",
    "forum.bubble.io",
    "forum.cursor.com",
    "forum.freecodecamp.org",
    "forum.obsidian.md",
    "replit.discourse.group",
)

NOT_SEARCHED = [
    "Reddit (no approved API access)",
    "Facebook (groups are private, no API)",
    "Discord servers (private, no API)",
]

EVIDENCE_KINDS = ("complaint", "wish", "workaround")

# Sites with no approved API. A person can browse them and paste what they find.
PASTED_SOURCES = {
    "reddit": ("Reddit", r"https://(?:[a-z0-9-]+\.)?reddit\.com/\S+"),
    "facebook": ("Facebook", r"https://(?:[a-z0-9-]+\.)?facebook\.com/\S+"),
}
PASTED_SUFFIX = " (pasted by you)"
MAX_PASTED_CHARS = 30000
MAX_PASTED_PER_SOURCE = 45
MIN_PASTED_CHARS = 20

# Deliberately narrow: "I built an app and it keeps crashing" is a complaint.
# The extraction model catches subtler promotion by marking it "seller".
_PROMO = re.compile(
    r"\b(hire me|seeking work|dm me|check out my|book a (call|demo)|promo code|"
    r"discount code|use code|free trial|we offer|we help|our agency|"
    r"(i|we) (made|built|created)\b.{0,50}\b(to solve|that solves|to fix|that fixes|which solves))\b",
    re.IGNORECASE,
)


class SourceSkipped(Exception):
    """The source could not be used for a known reason, such as a missing API key."""


@dataclass
class Item:
    platform: str
    author: str
    text: str
    url: str
    created: str = ""
    status: str = "unknown"  # open | answered | unknown
    title: str = ""
    id: str = ""


# ── Text helpers ────────────────────────────────────

def clean_html(raw: str) -> str:
    """Turn post HTML into plain text, dropping code and text quoted from others."""
    text = raw or ""
    text = re.sub(r"(?is)<(blockquote|aside|pre|code)\b.*?</\1>", " ", text)
    text = re.sub(r"(?is)<p>\s*&gt;.*?(?=<p>|$)", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize(text: str) -> str:
    """Compare text ignoring case, spacing and curly-versus-straight punctuation."""
    text = unicodedata.normalize("NFKC", text or "")
    for fancy, plain in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")):
        text = text.replace(fancy, plain)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_verbatim(quote: str, source_text: str) -> bool:
    wanted = normalize(quote)
    return len(wanted) >= MIN_QUOTE_CHARS and wanted in normalize(source_text)


def is_promotional(text: str) -> bool:
    return bool(_PROMO.search(text or ""))


def person_key(item: dict) -> str:
    """One identity per platform. Unknown authors collapse into one person, on purpose."""
    author = re.sub(r"\s+", " ", (item.get("author") or "").strip().lower())
    if author in ("", "deleted", "[deleted]", "anonymous", "ghost"):
        author = "unknown"
    return f"{item.get('platform', '')}:{author}"


def is_pasted(platform: str) -> bool:
    return (platform or "").endswith(PASTED_SUFFIX)


def _redact(text: str) -> str:
    text = re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "[email removed]", text)
    return re.sub(r"(?<!\d)(?:\+?\d[\s().-]?){10,14}(?!\d)", "[phone removed]", text)


def parse_pasted(source: str, raw: str) -> tuple:
    """Split pasted text into one item per blank-line-separated block.

    Returns (items, blocks_not_used). Each block counts as one person, because
    pasted text has no reliable author. Reports disclose that.
    """
    label, url_pattern = PASTED_SOURCES[source]
    blocks = re.split(r"\n\s*\n", (raw or "").replace("\r\n", "\n").strip()[:MAX_PASTED_CHARS])
    items, unused = [], 0
    for block in blocks:
        link = re.search(url_pattern, block)
        text = re.sub(r"\s+", " ", re.sub(url_pattern, " ", block)).strip()
        if len(text) < MIN_PASTED_CHARS:
            unused += 1 if block.strip() else 0
            continue
        if len(items) >= MAX_PASTED_PER_SOURCE:
            unused += 1
            continue
        items.append(
            Item(
                platform=label + PASTED_SUFFIX,
                author=f"pasted-{source}-{len(items) + 1}",
                text=_redact(text),
                url=link.group(0) if link else "",
            )
        )
    return items, unused


def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


def _get(url: str, params: dict = None, headers: dict = None) -> dict:
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        timeout=TIMEOUT_SEC,
    )
    resp.raise_for_status()
    return resp.json()


# ── Connectors ──────────────────────────────────────

def hn_comments(query: str, limit: int = 30) -> list:
    data = _get(
        "https://hn.algolia.com/api/v1/search",
        {
            "query": query,
            "tags": "comment",
            "hitsPerPage": limit,
            "numericFilters": f"created_at_i>{int(_cutoff().timestamp())}",
        },
    )
    return [
        Item(
            platform="Hacker News",
            author=h.get("author") or "",
            text=clean_html(h.get("comment_text") or ""),
            url=f"https://news.ycombinator.com/item?id={h['objectID']}",
            created=(h.get("created_at") or "")[:10],
            title=h.get("story_title") or "",
        )
        for h in data.get("hits", [])
    ]


def stack_exchange(query: str, site: str = "stackoverflow", limit: int = 25) -> list:
    if not re.fullmatch(r"[a-z][a-z.]{1,40}", site or ""):
        raise ValueError(f"invalid Stack Exchange site: {site!r}")
    data = _get(
        "https://api.stackexchange.com/2.3/search/advanced",
        {
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": site,
            "pagesize": limit,
            "fromdate": int(_cutoff().timestamp()),
            "filter": "withbody",
        },
    )
    items = []
    for q in data.get("items", []):
        created = datetime.fromtimestamp(q.get("creation_date", 0), timezone.utc)
        answered = q.get("accepted_answer_id") or q.get("is_answered")
        items.append(
            Item(
                platform=f"Stack Exchange ({site})",
                author=(q.get("owner") or {}).get("display_name", ""),
                text=f"{html.unescape(q.get('title', ''))}. {clean_html(q.get('body', ''))}",
                url=q.get("link", ""),
                created=created.strftime("%Y-%m-%d"),
                status="answered" if answered else "open",
                title=html.unescape(q.get("title", "")),
            )
        )
    return items


def github_issues(query: str, limit: int = 25) -> list:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    since = _cutoff().strftime("%Y-%m-%d")
    data = _get(
        "https://api.github.com/search/issues",
        {"q": f"{query} is:issue created:>{since}", "per_page": limit},
        headers,
    )
    items = []
    for issue in data.get("items", []):
        user = issue.get("user") or {}
        if user.get("type") == "Bot":
            continue
        if issue.get("state") == "open":
            status = "open"
        elif issue.get("state_reason") == "completed":
            status = "answered"
        else:
            status = "unknown"
        items.append(
            Item(
                platform="GitHub",
                author=user.get("login", ""),
                text=f"{issue.get('title', '')}. {clean_html(issue.get('body') or '')}",
                url=issue.get("html_url", ""),
                created=(issue.get("created_at") or "")[:10],
                status=status,
                title=issue.get("title", ""),
            )
        )
    return items


def app_store_reviews(query: str, apps: int = 2, max_rating: int = 3) -> list:
    found = _get(
        "https://itunes.apple.com/search",
        {"term": query, "entity": "software", "country": "us", "limit": apps},
    )
    items = []
    since = _cutoff().strftime("%Y-%m-%d")
    for app in found.get("results", [])[:apps]:
        feed = _get(
            f"https://itunes.apple.com/us/rss/customerreviews/id={int(app['trackId'])}/sortBy=mostRecent/json"
        )
        entries = feed.get("feed", {}).get("entry", [])
        entries = [entries] if isinstance(entries, dict) else entries
        for entry in entries:
            rating = int(entry.get("im:rating", {}).get("label", 5))
            created = entry.get("updated", {}).get("label", "")[:10]
            if rating > max_rating or created < since:
                continue
            headline = entry.get("title", {}).get("label", "")
            body = entry.get("content", {}).get("label", "")
            items.append(
                Item(
                    platform="App Store",
                    author=entry.get("author", {}).get("name", {}).get("label", ""),
                    text=f"{headline}. {body}",
                    url=app.get("trackViewUrl", ""),
                    created=created,
                    title=f"{app.get('trackName', '')} ({rating} stars)",
                )
            )
    return items


def discourse_posts(forum: str, query: str, topics: int = 8, posts_per_topic: int = 5) -> list:
    if forum not in DISCOURSE_FORUMS:
        raise ValueError(f"forum not on the allowed list: {forum!r}")
    base = f"https://{forum}"
    since = _cutoff().strftime("%Y-%m-%d")
    found = _get(f"{base}/search.json", {"q": f"{query} after:{since}"})
    items = []
    for topic in found.get("topics", [])[:topics]:
        time.sleep(0.4)  # these are community-run forums, so go gently
        thread = _get(f"{base}/t/{int(topic['id'])}.json")
        if topic.get("has_accepted_answer"):
            status = "answered"
        elif topic.get("closed") or thread.get("closed"):
            status = "unknown"
        else:
            status = "open"
        for post in thread.get("post_stream", {}).get("posts", [])[:posts_per_topic]:
            if post.get("post_type") != 1:
                continue
            items.append(
                Item(
                    platform=forum,
                    author=post.get("username", ""),
                    text=clean_html(post.get("cooked", "")),
                    url=f"{base}/t/{thread.get('slug', 'topic')}/{int(topic['id'])}/{post.get('post_number', 1)}",
                    created=(post.get("created_at") or "")[:10],
                    status=status,
                    title=thread.get("title", ""),
                )
            )
    return items


def youtube_comments(query: str, videos: int = 3, limit: int = 20) -> list:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise SourceSkipped("YOUTUBE_API_KEY is not set")
    found = _get(
        "https://www.googleapis.com/youtube/v3/search",
        {"part": "snippet", "type": "video", "q": query, "maxResults": videos, "key": key},
    )
    items = []
    for video in found.get("items", []):
        video_id = video["id"]["videoId"]
        try:
            threads = _get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                {
                    "part": "snippet",
                    "videoId": video_id,
                    "maxResults": limit,
                    "order": "relevance",
                    "textFormat": "plainText",
                    "key": key,
                },
            )
        except requests.HTTPError:
            continue  # comments are turned off on this video
        for thread in threads.get("items", []):
            comment = thread["snippet"]["topLevelComment"]["snippet"]
            items.append(
                Item(
                    platform="YouTube",
                    author=comment.get("authorDisplayName", ""),
                    text=re.sub(r"\s+", " ", comment.get("textDisplay", "")).strip(),
                    url=f"https://www.youtube.com/watch?v={video_id}&lc={thread['id']}",
                    created=comment.get("publishedAt", "")[:10],
                    title=video["snippet"].get("title", ""),
                )
            )
    return items


def _dispatch(spec: dict) -> list:
    kind, query = spec.get("type"), (spec.get("query") or "").strip()
    if not query:
        raise ValueError("connector has no query")
    if kind == "hn_comments":
        return hn_comments(query)
    if kind == "stack_exchange":
        return stack_exchange(query, spec.get("site") or "stackoverflow")
    if kind == "github_issues":
        return github_issues(query)
    if kind == "app_store_reviews":
        return app_store_reviews(query)
    if kind == "discourse":
        return discourse_posts(spec.get("forum", ""), query)
    if kind == "youtube_comments":
        return youtube_comments(query)
    raise ValueError(f"unknown connector type: {kind!r}")


def run_connectors(specs: list) -> tuple:
    """Run a brief's connectors. Failures are recorded and returned, never hidden."""
    items, log = [], []
    for spec in (specs or [])[:MAX_CONNECTORS_PER_BRIEF]:
        entry = {
            "type": spec.get("type"),
            "target": spec.get("forum") or spec.get("site") or "",
            "query": spec.get("query", ""),
            "status": "ok",
            "items": 0,
            "detail": "",
        }
        try:
            found = [i for i in _dispatch(spec) if i.text and i.url.startswith("https://")]
            entry["items"] = len(found)
            items.extend(found)
        except SourceSkipped as e:
            entry.update(status="skipped", detail=str(e))
        except Exception as e:
            entry.update(status="failed", detail=f"{type(e).__name__}: {e}"[:200])
            print(f"[WARN] connector failed {entry}", flush=True)
        log.append(entry)
    return items, log


def assign_ids(items_by_brief: dict) -> dict:
    """Give items run-wide ids and drop duplicates. Returns {id: item dict}."""
    store, seen = {}, set()
    for items in items_by_brief.values():
        kept = []
        for item in items:
            key = (item.url, normalize(item.text)[:300])
            if key in seen:
                continue
            seen.add(key)
            item.id = f"E{len(store) + 1}"
            item.text = item.text[:MAX_TEXT_CHARS]
            store[item.id] = asdict(item)
            kept.append(item)
        items[:] = kept
    return store


# ── Verification and reporting ──────────────────────

def verify_extractions(extractions: list, store: dict) -> tuple:
    """Keep only real, non-promotional quotes. Returns (verified, counts)."""
    verified, counts = {}, {"extracted": 0, "rejected_unverified": 0, "filtered_sellers": 0}
    for entry in extractions:
        counts["extracted"] += 1
        item = store.get(entry.get("item_id"))
        quote = (entry.get("quote") or "").strip()
        kind = entry.get("kind")
        if not item or not quote_is_verbatim(quote, item["text"]):
            counts["rejected_unverified"] += 1
        elif kind == "seller" or is_promotional(item["text"]):
            counts["filtered_sellers"] += 1
        elif kind in EVIDENCE_KINDS and item["id"] not in verified:
            verified[item["id"]] = {
                **item,
                "quote": quote,
                "kind": kind,
                "theme": (entry.get("theme") or "").strip()[:80],
            }
    return verified, counts


LEVEL_RANK = {"Strong": 3, "Moderate": 2, "Narrow": 1, "Thin": 0}


def strength_of(people: int, platforms: int) -> str:
    if people >= 15 and platforms >= 3:
        return "Strong"
    if people >= 5 and platforms >= 2:
        return "Moderate"
    if people >= 5:
        return "Narrow"  # plenty of voices, but all from one place
    return "Thin"


def evidence_stats(ids: list, verified: dict) -> dict:
    rows = [verified[i] for i in ids]
    platforms = sorted({r["platform"] for r in rows})
    people = len({person_key(r) for r in rows})
    level = strength_of(people, len(platforms))

    # Pasted comments can support a finding, but cannot make it "Strong" alone,
    # because their source and authors could not be checked.
    checked = [r for r in rows if not is_pasted(r["platform"])]
    checked_people = len({person_key(r) for r in checked})
    checked_level = strength_of(checked_people, len({r["platform"] for r in checked}))
    if level == "Strong" and checked_level != "Strong":
        level = "Moderate"

    return {
        "level": level,
        "people": people,
        "pasted": people - checked_people,
        "checked_level": checked_level if checked_people else "None",
        "checked_people": checked_people,
        "mentions": len(rows),
        "platforms": platforms,
        "open": sum(r["status"] == "open" for r in rows),
        "answered": sum(r["status"] == "answered" for r in rows),
        "unknown": sum(r["status"] == "unknown" for r in rows),
    }


def _quotes_for_display(ids: list, verified: dict) -> list:
    """One quote per person first, so a single loud poster cannot fill the card."""
    first, rest, seen = [], [], set()
    for i in ids:
        key = person_key(verified[i])
        (rest if key in seen else first).append(i)
        seen.add(key)
    return [
        {k: verified[i][k] for k in ("id", "quote", "platform", "url", "created", "status", "kind", "theme")}
        for i in (first + rest)[:MAX_QUOTES_SHOWN]
    ]


def _strip_ids(value):
    """Remove internal ids such as [E12] from text readers will see."""
    if isinstance(value, str):
        return re.sub(r"\s*\[E\d+(?:\s*,\s*E\d+)*\]", "", value)
    if isinstance(value, list):
        return [_strip_ids(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_ids(v) for k, v in value.items()}
    return value


def apply_evidence(report: dict, verified: dict) -> dict:
    """Replace model claims about evidence with numbers computed from verified quotes."""
    ids_by_opportunity = [opp.pop("evidence_ids", []) for opp in report.get("opportunities") or []]
    report = _strip_ids(report)
    for opp, ids in zip(report.get("opportunities") or [], ids_by_opportunity):
        opp["evidence_ids"] = ids
    run_platforms = {r["platform"] for r in verified.values()}
    original = report.get("opportunities") or []
    kept, dropped = [], []

    for opp in original:
        ids = list(dict.fromkeys(i for i in opp.pop("evidence_ids", []) or [] if i in verified))
        if not ids:
            dropped.append(opp.get("name") or "Unnamed opportunity")
            continue
        stats = evidence_stats(ids, verified)
        opp["evidence_strength"] = stats
        opp["evidence"] = _quotes_for_display(ids, verified)
        opp["signal_convergence"] = (
            f"{len(stats['platforms'])}/{len(run_platforms)} platforms: "
            f"{', '.join(stats['platforms'])} · {stats['people']} different people"
        )
        kept.append(opp)

    # Rank by checked evidence only: spread across platforms first, then how many
    # people. Pasted comments are shown, but cannot move an idea up. Ties keep the
    # synthesizer's order.
    kept.sort(key=lambda o: (
        -LEVEL_RANK.get(o["evidence_strength"]["checked_level"], -1),
        -o["evidence_strength"]["checked_people"],
    ))

    report["opportunities"] = kept
    watch = report.get("watch_list") or []
    report["watch_list"] = watch + [f"{n} — removed: no verified quote supports it" for n in dropped]

    if not kept:
        report["the_call"] = {
            "recommended_focus": "Cannot make a call: no opportunity had verified quotes from real people.",
            "for_whom": "",
            "why_now": "",
            "validation_step": "Talk to people in this market directly before building anything.",
        }
        report["evidence_notice"] = "No opportunity survived quote checking, so this report makes no recommendation."
        return report

    top = kept[0]
    if not original or original[0] is not top:
        plan = top.get("validation_plan") or [""]
        report["the_call"] = {
            "recommended_focus": top.get("what", ""),
            "for_whom": top.get("who", ""),
            "why_now": top.get("timing", ""),
            "validation_step": plan[0],
        }
    stats = top["evidence_strength"]
    if stats["level"] == "Thin":
        report["evidence_notice"] = (
            f"Evidence is thin: the top opportunity rests on {stats['people']} "
            f"{'person' if stats['people'] == 1 else 'different people'} across "
            f"{len(stats['platforms'])} {'platform' if len(stats['platforms']) == 1 else 'platforms'}. "
            "Treat it as a lead to test, not a finding."
        )
    elif stats["level"] == "Narrow":
        report["evidence_notice"] = (
            f"Evidence is narrow: {stats['people']} different people raised this, but only on "
            f"{stats['platforms'][0]}. Check whether it shows up anywhere else before relying on it."
        )
    if stats["pasted"]:
        note = (
            f"This includes {stats['pasted']} {'person' if stats['pasted'] == 1 else 'people'} from comments "
            "you pasted, which could not be independently checked."
        )
        report["evidence_notice"] = " ".join(filter(None, [report.get("evidence_notice"), note]))
    return report


def coverage_summary(connector_logs: list, store: dict, verified: dict, counts: dict) -> dict:
    """What this run did and did not see, for the report itself."""
    pasted = {c["target"] for c in connector_logs if c.get("type") == "pasted" and c.get("items")}
    not_searched = [
        f"{entry.split(' (')[0]} (only the comments you pasted)" if entry.split(" (")[0] in pasted else entry
        for entry in NOT_SEARCHED
    ]
    limits = (
        "Comments show what hurts, not whether anyone will pay. "
        "The same person on two platforms counts as two people."
    )
    if pasted:
        limits += (
            " Pasted comments were checked against what you pasted, not against the original site, "
            "and each pasted block counts as one person."
        )
    return {
        "items_read": len(store),
        "quotes_extracted": counts.get("extracted", 0),
        "quotes_verified": len(verified),
        "quotes_rejected": counts.get("rejected_unverified", 0),
        "sellers_filtered": counts.get("filtered_sellers", 0),
        "people": len({person_key(r) for r in verified.values()}),
        "connectors": connector_logs,
        "not_searched": not_searched,
        "limits": limits,
    }
