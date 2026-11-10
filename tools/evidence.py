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
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "market-research-swarm/1.0"
TIMEOUT_SEC = 20
LOOKBACK_DAYS = 540
MAX_TEXT_CHARS = 1500
MAX_CONNECTORS_PER_BRIEF = 4
MAX_QUOTES_SHOWN = 8
MIN_QUOTE_CHARS = 15

# Fixed list, because the planner is a model and must not choose arbitrary hosts
# for the server to call. Every host answered /search.json and had posts in the
# last day when checked on 2026-11-09 and 2026-11-10.
FORUM_TOPICS = {
    "community.fly.io": "app hosting",
    "community.home-assistant.io": "smart home",
    "community.make.com": "no-code automation",
    "community.n8n.io": "workflow automation",
    "community.openai.com": "AI developers",
    "forum.bubble.io": "no-code app builders",
    "forum.cursor.com": "AI coding",
    "forum.freecodecamp.org": "people learning to code",
    "forum.obsidian.md": "note-taking",
    "replit.discourse.group": "app builders",
    "community.monzo.com": "UK banking and personal finance",
    "community.wanikani.com": "learning Japanese",
    "discuss.pixls.us": "photography and photo editing",
    "forum.literatureandlatte.com": "writers and novelists",
    "community.glowforge.com": "laser cutting makers and small craft businesses",
    "goingtoseed.discourse.group": "gardening and seed saving",
}
DISCOURSE_FORUMS = tuple(FORUM_TOPICS)

NOT_SEARCHED = [
    "Reddit (no approved API access)",
    "Facebook (groups are private, no API)",
    "Discord servers (private, no API)",
]

EVIDENCE_KINDS = ("complaint", "wish", "workaround")
PAY_KINDS = ("pays_now", "would_pay", "switched")

# Sites with no approved API. A person can browse them and paste what they find.
PASTED_SOURCES = {
    "reddit": ("Reddit", r"https://(?:[a-z0-9-]+\.)?reddit\.com/\S+"),
    "facebook": ("Facebook", r"https://(?:[a-z0-9-]+\.)?facebook\.com/\S+"),
}
PASTED_SUFFIX = " (pasted by you)"
MIN_PASTED_CHARS = 20

# Deliberately narrow: "I built an app and it keeps crashing" is a complaint.
# The extraction model catches subtler promotion by marking it "seller".
_PROMO = re.compile(
    r"\b(hire me|seeking work|dm me|check out my|book a (call|demo)|promo code|"
    r"discount code|use code|free trial|we offer|we help|our agency|"
    r"available for (hire|work|freelance|contract)|willing to relocate|remote:\s*(yes|no)|"
    r"r[eé]sum[eé]\s*/\s*cv|"
    r"(i|we) (also )?(fix|finish|build|rescue|audit|help)\b.{0,40}\b(apps?|founders|startups|teams|clients|projects)\b|"
    r"helping (founders|startups|teams|businesses|clients)|"
    r"(i|we) (made|built|created)\b.{0,50}\b(to solve|that solves|to fix|that fixes|which solves))\b",
    re.IGNORECASE,
)

# Hacker News threads where people advertise themselves or jobs. Their comments
# describe what someone sells or hires for, not a problem the writer has.
_HN_JOB_THREADS = re.compile(r"who (wants to be|is) hired|seeking (work|freelancer)|freelancer\?", re.IGNORECASE)


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


# Whole pages copied with Cmd+A carry buttons, counters and menus between comments.
# These patterns mark that clutter. They also mark where one comment ends.
_PAGE_NOISE = {
    "reply", "share", "award", "report", "save", "follow", "join", "joined", "upvote", "downvote",
    "vote", "comment", "comments", "more", "like", "love", "edited", "op", "promoted", "sponsored",
    "best", "top", "new", "old", "controversial", "q&a", "open", "sort by:", "sort by", "author",
    "top fan", "most relevant", "all comments", "newest", "write a comment…", "write a comment...",
    "add a comment", "back", "home", "popular", "explore", "log in", "sign up", "get app", "•", "·",
    "translate", "see translation", "hide", "view insights", "send", "copy link", "menu",
    "skip to main content", "open menu", "reddit", "go to comments", "search comments", "open chat",
    "create post", "create", "advertise on reddit", "community info", "admin", "moderator",
    "group expert", "rising contributor", "all-star contributor", "anonymous participant", "facebook",
}
_BULLETS = {"•", "·"}
_PAGE_TIME = re.compile(
    r"^(?:[•·]\s*)?(?:\d+\s*(?:s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|week|weeks|"
    r"mo|mos|month|months|y|yr|yrs|year|years)\.?(?:\s+ago)?|just now|yesterday)(?:\s*[•·].*)?$",
    re.IGNORECASE,
)
_PAGE_COUNT = re.compile(
    r"^[\d.,]+\s*[km]?(?:\s*(?:votes?|points?|upvotes?|comments?|replies|repl(?:y|ies)|likes?|"
    r"reactions?|shares?|members?|online))?$",
    re.IGNORECASE,
)
_PAGE_MORE = re.compile(
    r"^(?:\d+\s+more\s+repl(?:y|ies)|view (?:all|more|\d+).*|see more.*|show more.*|"
    r"continue this thread.*|load more.*|more repl(?:y|ies)|read more.*|"
    r"r/[a-z0-9_]+|u/[a-z0-9_-]+ .*|level \d+)$",
    re.IGNORECASE,
)
_REDDIT_HANDLE = re.compile(r"^(?:u/)?([A-Za-z0-9_-]{3,24})$")
_FB_NAME = re.compile(r"^(?:[A-Z][\w'’.-]*)(?: [A-Z][\w'’.-]*){1,3}$")
MAX_PASTED_CHARS = 150000
MAX_PASTED_PER_SOURCE = 150


def _line_kind(line: str) -> str:
    low = line.strip().lower()
    if _PAGE_TIME.match(low):
        return "time"
    if low in _PAGE_NOISE or _PAGE_COUNT.match(low) or _PAGE_MORE.match(low):
        return "noise"
    return "text"


def looks_like_whole_page(raw: str) -> bool:
    lines = [l for l in (raw or "").splitlines() if l.strip()]
    if len(lines) < 8:
        return False
    marks = sum(_line_kind(l) != "text" for l in lines)
    return marks >= 4 and marks / len(lines) >= 0.12


def _page_blocks(source: str, raw: str, url_pattern: str) -> list:
    """Split a whole copied page into (author, text, url) comments."""
    lines = [l.strip() for l in raw.splitlines()]
    blocks, current, author, page_url = [], [], "", ""

    def close():
        # A name followed by badges such as "OP" or "Top fan" keeps its author
        # until the comment text arrives, so only reset after a comment is saved.
        nonlocal current, author
        if current:
            blocks.append((author, " ".join(current), page_url))
            current, author = [], ""

    def neighbour(index: int, step: int) -> str:
        j = index + step
        while 0 <= j < len(lines):
            if lines[j] and lines[j] not in _BULLETS:
                return lines[j]
            j += step
        return ""

    for index, line in enumerate(lines):
        if not line:
            continue
        link = re.fullmatch(url_pattern, line)
        if link:
            close()
            page_url = link.group(0)
            continue
        kind = _line_kind(line)
        if kind == "noise":
            close()
            continue
        if kind == "time":
            if current:
                close()
            continue
        upcoming, previous = neighbour(index, 1), neighbour(index, -1)
        handle = _REDDIT_HANDLE.match(line)
        if source == "reddit" and handle and (
            line.startswith("u/")
            or _line_kind(upcoming) == "time"
            or upcoming.lower() == "op"
            or (not current and _line_kind(previous) == "time" and _line_kind(upcoming) == "text")
        ):
            close()
            author = handle.group(1)
            continue
        if source == "facebook" and _FB_NAME.match(line) and (
            _line_kind(upcoming) == "text" or upcoming.lower() in _PAGE_NOISE
        ) and _line_kind(upcoming) != "time":
            close()
            author = line
            continue
        current.append(line)
    close()
    return blocks


def parse_pasted(source: str, raw: str) -> tuple:
    """Split pasted text into comments.

    Accepts single comments separated by blank lines, or whole pages copied with
    Cmd+A. On whole pages, the comment author is detected when possible so one
    person counts once; otherwise each comment counts as one person, which
    reports disclose. Returns (items, blocks_not_used, mode).
    """
    label, url_pattern = PASTED_SOURCES[source]
    raw = (raw or "").replace("\r\n", "\n").strip()[:MAX_PASTED_CHARS]
    if looks_like_whole_page(raw):
        mode, blocks = "page", _page_blocks(source, raw, url_pattern)
    else:
        mode, blocks = "comments", []
        for block in re.split(r"\n\s*\n", raw):
            link = re.search(url_pattern, block)
            blocks.append(("", re.sub(url_pattern, " ", block), link.group(0) if link else ""))

    items, unused = [], 0
    for author, block, link in blocks:
        text = re.sub(r"\s+", " ", block).strip()
        if len(text) < MIN_PASTED_CHARS:
            unused += 1 if text else 0
            continue
        if len(items) >= MAX_PASTED_PER_SOURCE:
            unused += 1
            continue
        items.append(
            Item(
                platform=label + PASTED_SUFFIX,
                author=f"pasted-{source}-{author.lower()}" if author else f"pasted-{source}-#{len(items) + 1}",
                text=_redact(text),
                url=link,
            )
        )
    return items, unused, mode


def preview_pasted(source: str, raw: str) -> dict:
    """What the swarm would take from a paste, without spending anything."""
    items, unused, mode = parse_pasted(source, raw)
    return {
        "mode": mode,
        "comments": len(items),
        "people": len({person_key(asdict(i)) for i in items}),
        "unused": unused,
        "capped": len(items) >= MAX_PASTED_PER_SOURCE,
        "samples": [i.text[:160] for i in items[:3]],
    }


_REDDIT_THREAD = re.compile(r"^https://(?:www\.|old\.)?reddit\.com/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)")


def pick_reddit_threads(results: list, limit: int = 8) -> list:
    """Keep real Reddit discussion threads from search results, one per thread."""
    threads, seen = [], set()
    for r in results:
        match = _REDDIT_THREAD.match(r.get("link") or "")
        if not match or match.group(2) in seen:
            continue
        seen.add(match.group(2))
        title = re.sub(r"\s*[:|-]\s*r/[A-Za-z0-9_]+\s*$", "", r.get("title") or "").strip()
        threads.append({
            "title": title or "Reddit thread",
            "subreddit": f"r/{match.group(1)}",
            "url": match.group(0),
            "date": r.get("date", ""),
        })
        if len(threads) >= limit:
            break
    return threads


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
        if not _HN_JOB_THREADS.search(h.get("story_title") or "")
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


def app_store_reviews(query: str, apps: int = 2, max_rating: int = 3,
                      countries: tuple = ("us", "ca", "gb")) -> list:
    """1-3 star reviews from several countries' stores. The same review can appear
    in more than one store, so reviews are de-duplicated by author and text."""
    found = _get(
        "https://itunes.apple.com/search",
        {"term": query, "entity": "software", "country": "us", "limit": apps},
    )
    items, seen = [], set()
    since = _cutoff().strftime("%Y-%m-%d")
    for app in found.get("results", [])[:apps]:
        track_id = int(app["trackId"])
        for country in countries:
            try:
                feed = _get(f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortBy=mostRecent/json")
            except requests.HTTPError:
                continue  # the app is not sold in that country
            entries = feed.get("feed", {}).get("entry", [])
            entries = [entries] if isinstance(entries, dict) else entries
            for entry in entries:
                rating = int(entry.get("im:rating", {}).get("label", 5))
                created = entry.get("updated", {}).get("label", "")[:10]
                if rating > max_rating or created < since:
                    continue
                headline = entry.get("title", {}).get("label", "")
                body = entry.get("content", {}).get("label", "")
                author = entry.get("author", {}).get("name", {}).get("label", "")
                key = (author, normalize(body)[:200])
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    Item(
                        platform="App Store",
                        author=author,
                        text=f"{headline}. {body}",
                        url=f"https://apps.apple.com/{country}/app/id{track_id}",
                        created=created,
                        title=f"{app.get('trackName', '')} ({rating} stars, {country.upper()} store)",
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


def lemmy_comments(query: str, limit: int = 30) -> list:
    """Comments on Lemmy, an open, Reddit-like network, through lemmy.world's public API."""
    data = _get(
        "https://lemmy.world/api/v3/search",
        {"q": query, "type_": "Comments", "sort": "TopYear", "limit": limit},
    )
    items = []
    for row in data.get("comments", []):
        comment, creator = row.get("comment") or {}, row.get("creator") or {}
        if comment.get("removed") or comment.get("deleted") or creator.get("bot_account"):
            continue
        text = re.sub(r"(?m)^\s*>.*$", " ", comment.get("content") or "")  # drop text quoted from others
        items.append(
            Item(
                platform="Lemmy",
                author=creator.get("actor_id") or creator.get("name", ""),
                text=re.sub(r"\s+", " ", text).strip(),
                url=comment.get("ap_id", ""),
                created=(comment.get("published") or "")[:10],
                title=(row.get("post") or {}).get("name", ""),
            )
        )
    return items


def github_discussions(query: str, limit: int = 20) -> list:
    """GitHub Discussions threads. GitHub only allows this search with a token."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SourceSkipped("GITHUB_TOKEN is not set")
    since = _cutoff().strftime("%Y-%m-%d")
    graphql = (
        "query($q: String!, $n: Int!) { search(query: $q, type: DISCUSSION, first: $n) { nodes { "
        "... on Discussion { title body url createdAt author { login } answer { id } } } } }"
    )
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": graphql, "variables": {"q": f"{query} created:>{since}", "n": limit}},
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "GitHub GraphQL error"))
    items = []
    for node in ((data.get("data") or {}).get("search") or {}).get("nodes") or []:
        if not node or not node.get("url"):
            continue
        body = re.sub(r"\s+", " ", node.get("body") or "")
        items.append(
            Item(
                platform="GitHub Discussions",
                author=(node.get("author") or {}).get("login", ""),
                text=f"{node.get('title', '')}. {body}".strip(),
                url=node["url"],
                created=(node.get("createdAt") or "")[:10],
                status="answered" if node.get("answer") else "open",
                title=node.get("title", ""),
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
    if kind == "lemmy_comments":
        return lemmy_comments(query)
    if kind == "github_discussions":
        return github_discussions(query)
    raise ValueError(f"unknown connector type: {kind!r}")


def _run_one_connector(spec: dict) -> tuple:
    entry = {
        "type": spec.get("type"),
        "target": spec.get("forum") or spec.get("site") or "",
        "query": spec.get("query", ""),
        "status": "ok",
        "items": 0,
        "detail": "",
        "seconds": 0.0,
    }
    started, found = time.time(), []
    try:
        found = [i for i in _dispatch(spec) if i.text and i.url.startswith("https://")]
        entry["items"] = len(found)
    except SourceSkipped as e:
        entry.update(status="skipped", detail=str(e))
    except Exception as e:
        entry.update(status="failed", detail=f"{type(e).__name__}: {e}"[:200])
        print(f"[WARN] connector failed {entry}", flush=True)
    entry["seconds"] = round(time.time() - started, 1)
    return found, entry


def run_connectors(specs: list) -> tuple:
    """Run a brief's connectors at the same time. Failures are recorded, never hidden.

    Results keep the planner's order, so the same plan gives the same item order.
    """
    specs = (specs or [])[:MAX_CONNECTORS_PER_BRIEF]
    if not specs:
        return [], []
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        results = list(pool.map(_run_one_connector, specs))
    items = [item for found, _ in results for item in found]
    return items, [entry for _, entry in results]


def strength_label(ratings: int) -> str:
    """How entrenched an app is, judged by how many people have rated it."""
    if ratings >= 50000:
        return "Dominant"
    if ratings >= 5000:
        return "Established"
    if ratings > 0:
        return "Small"
    return "Unknown"


def _site(url: str) -> str:
    """example.com from https://www.example.com/path, for comparing who owns a site."""
    host = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0].split(":")[0]
    return ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host


def app_store_strength(name: str, product_url: str = ""):
    """Rating, rating count and price of the App Store app for a product.

    Names collide (a restaurant tool and a transcription app can both be "Otter"),
    so an app only counts when its developer's website matches the product's site.
    Without both websites, only an exact name match counts.
    """
    want = normalize(name)
    if len(want) < 3:
        return None
    found = _get("https://itunes.apple.com/search", {"term": name, "entity": "software", "country": "us", "limit": 5})
    product_site = _site(product_url) if product_url.startswith("https://") else ""
    matches = []
    for app in found.get("results", []):
        title = normalize(app.get("trackName", ""))
        named = title == want or title.startswith(want + " ") or title.startswith(want + ":") or \
            re.split(r"[:\-–]", title)[0].strip() == want
        seller_site = _site(app.get("sellerUrl") or "")
        if product_site and seller_site:
            if named and seller_site == product_site:
                matches.append(app)
        elif title == want:
            matches.append(app)
    if matches:
        # A company can have several apps; its main one is the most rated.
        app = max(matches, key=lambda a: int(a.get("userRatingCount") or 0))
        count = int(app.get("userRatingCount") or 0)
        return {
            "app_name": app.get("trackName", ""),
            "app_rating": round(float(app.get("averageUserRating") or 0), 1),
            "app_ratings": count,
            "price": app.get("formattedPrice", ""),
            "app_url": app.get("trackViewUrl", ""),
            "strength": strength_label(count),
        }
    return None


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
                "pay": entry.get("pay") if entry.get("pay") in PAY_KINDS else "none",
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
        "paying": len({person_key(r) for r in rows if r.get("pay", "none") != "none"}),
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
        {k: verified[i].get(k, "") for k in ("id", "quote", "platform", "url", "created", "status", "kind", "theme", "pay")}
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
        pay_ids = [i for i in ids if verified[i].get("pay", "none") != "none"]
        opp["pay_signals"] = {
            "people": stats["paying"],
            "quotes": [
                {k: verified[i].get(k, "") for k in ("quote", "platform", "url", "created", "pay")}
                for i in pay_ids[:3]
            ],
        }
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
        "pay_quotes": sum(r.get("pay", "none") != "none" for r in verified.values()),
        "people": len({person_key(r) for r in verified.values()}),
        "connectors": connector_logs,
        "not_searched": not_searched,
        "limits": limits,
    }
