import os
import re
import requests
from datetime import datetime


def push(report_content: str) -> str:
    """Push a markdown report to Notion and return the page URL."""

    notion_token = os.getenv("NOTION_API_KEY")
    today = datetime.now().strftime("%B %d, %Y")

    blocks = _parse_markdown_to_blocks(str(report_content))

    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {notion_token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        },
        json={
            "parent": {"page_id": "334d2a0ae0c98147be47ddc3ee8ea682"},
            "icon": {"emoji": "📊"},
            "properties": {
                "title": [{"text": {"content": f"Market Research Report — {today}"}}]
            },
            "children": blocks[:100]
        }
    )

    if response.status_code != 200:
        print(f"\n❌ Notion push failed: {response.status_code} — {response.text}")
        return ""

    page_id = response.json().get("id")
    page_url = response.json().get("url")

    # Notion API only accepts 100 blocks at a time
    if len(blocks) > 100:
        for i in range(100, len(blocks), 100):
            patch_resp = requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers={
                    "Authorization": f"Bearer {notion_token}",
                    "Content-Type": "application/json",
                    "Notion-Version": "2022-06-28"
                },
                json={"children": blocks[i:i+100]}
            )
            if patch_resp.status_code != 200:
                print(f"⚠️  Partial push: blocks {i}+ failed ({patch_resp.status_code}). "
                      f"Page exists but is incomplete: {page_url}")
                return page_url

    print(f"\n✅ Report pushed to Notion!")
    print(f"🔗 {page_url}")
    return page_url


# Matches **bold text** segments. Non-greedy so it doesn't span unrelated pairs.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _rich_text(line: str) -> list:
    """Split a line into Notion rich_text segments, honoring **bold** markers."""
    segments = []
    cursor = 0
    for match in _BOLD_RE.finditer(line):
        # Plain text before the bold span
        if match.start() > cursor:
            segments.append({
                "type": "text",
                "text": {"content": line[cursor:match.start()]},
            })
        # The bold span itself
        segments.append({
            "type": "text",
            "text": {"content": match.group(1)},
            "annotations": {"bold": True},
        })
        cursor = match.end()
    # Trailing plain text
    if cursor < len(line):
        segments.append({
            "type": "text",
            "text": {"content": line[cursor:]},
        })
    # Edge case: empty line — Notion requires at least one segment
    if not segments:
        segments.append({"type": "text", "text": {"content": line}})
    return segments


def _block(block_type: str, line: str) -> dict:
    """Build a Notion block of the given type with rich_text from the line."""
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _rich_text(line)},
    }


def _parse_markdown_to_blocks(content: str) -> list:
    """Convert markdown text into Notion block objects."""
    blocks = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        elif line.startswith("### "):
            blocks.append(_block("heading_3", line[4:]))
        elif line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_block("heading_1", line[2:]))
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append(_block("bulleted_list_item", line[2:]))
        elif re.match(r"^\d+\.\s", line):
            # Strip the leading "N. " prefix (handles multi-digit too)
            content_after_num = re.sub(r"^\d+\.\s+", "", line)
            blocks.append(_block("numbered_list_item", content_after_num))
        elif line.startswith("|") and "---" in line:
            continue  # table separator row, skip
        elif line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            clean_cells = [c for c in cells if c]
            if clean_cells:
                blocks.append(_block("bulleted_list_item", " | ".join(clean_cells)))
        else:
            # Plain paragraph — chunk if over the per-block limit
            if len(line) > 1900:
                for i in range(0, len(line), 1900):
                    blocks.append(_block("paragraph", line[i:i+1900]))
            else:
                blocks.append(_block("paragraph", line))

    return blocks