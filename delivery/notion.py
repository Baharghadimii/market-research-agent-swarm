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
        if match.start() > cursor:
            segments.append({
                "type": "text",
                "text": {"content": line[cursor:match.start()]},
            })
        segments.append({
            "type": "text",
            "text": {"content": match.group(1)},
            "annotations": {"bold": True},
        })
        cursor = match.end()
    if cursor < len(line):
        segments.append({
            "type": "text",
            "text": {"content": line[cursor:]},
        })
    if not segments:
        segments.append({"type": "text", "text": {"content": line}})
    return segments


def _block(block_type: str, text: str, children: list | None = None) -> dict:
    """Build a Notion block. Optionally include nested children blocks."""
    block_body = {"rich_text": _rich_text(text)}
    if children:
        block_body["children"] = children
    return {
        "object": "block",
        "type": block_type,
        block_type: block_body,
    }


def _indent_level(raw_line: str) -> int:
    """Return the indentation level of a bullet line.

    One level = 2 spaces (the markdown convention used by most editors).
    Tabs are normalized to 2 spaces. Returns 0 for non-indented lines.
    """
    expanded = raw_line.expandtabs(2)
    stripped = expanded.lstrip(" ")
    leading = len(expanded) - len(stripped)
    return leading // 2


def _parse_markdown_to_blocks(content: str) -> list:
    """Convert markdown text into Notion block objects, preserving bullet nesting."""
    blocks = []
    # Stack of (indent_level, bullet_block) for currently-open bullet ancestors
    bullet_stack: list = []

    def attach_bullet(level: int, bullet: dict) -> None:
        """Attach a bullet at the given indent level, nesting under its parent if any."""
        # Pop any siblings/cousins at >= this level off the stack
        while bullet_stack and bullet_stack[-1][0] >= level:
            bullet_stack.pop()

        if bullet_stack:
            # Nest under the closest shallower bullet
            parent = bullet_stack[-1][1]
            parent_type = parent["type"]
            parent[parent_type].setdefault("children", []).append(bullet)
        else:
            # Top-level bullet
            blocks.append(bullet)

        bullet_stack.append((level, bullet))

    def reset_bullet_stack() -> None:
        bullet_stack.clear()

    for raw_line in content.split("\n"):
        # Preserve indentation for bullets, but work with stripped line for matching
        stripped = raw_line.strip()
        if not stripped:
            continue

        is_bullet = stripped.startswith("- ") or stripped.startswith("* ")
        is_numbered = bool(re.match(r"^\d+\.\s", stripped))

        if stripped.startswith("### "):
            reset_bullet_stack()
            blocks.append(_block("heading_3", stripped[4:]))
        elif stripped.startswith("## "):
            reset_bullet_stack()
            blocks.append(_block("heading_2", stripped[3:]))
        elif stripped.startswith("# "):
            reset_bullet_stack()
            blocks.append(_block("heading_1", stripped[2:]))
        elif is_bullet:
            level = _indent_level(raw_line)
            bullet = _block("bulleted_list_item", stripped[2:])
            attach_bullet(level, bullet)
        elif is_numbered:
            reset_bullet_stack()
            content_after_num = re.sub(r"^\d+\.\s+", "", stripped)
            blocks.append(_block("numbered_list_item", content_after_num))
        elif stripped.startswith("|") and "---" in stripped:
            continue
        elif stripped.startswith("|") and stripped.endswith("|"):
            reset_bullet_stack()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            clean_cells = [c for c in cells if c]
            if clean_cells:
                blocks.append(_block("bulleted_list_item", " | ".join(clean_cells)))
        else:
            reset_bullet_stack()
            if len(stripped) > 1900:
                for i in range(0, len(stripped), 1900):
                    blocks.append(_block("paragraph", stripped[i:i+1900]))
            else:
                blocks.append(_block("paragraph", stripped))

    return blocks