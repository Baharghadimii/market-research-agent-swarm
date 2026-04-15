import os
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

    if response.status_code == 200:
        page_id = response.json().get("id")
        page_url = response.json().get("url")

        # Notion API only accepts 100 blocks at a time
        if len(blocks) > 100:
            for i in range(100, len(blocks), 100):
                requests.patch(
                    f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers={
                        "Authorization": f"Bearer {notion_token}",
                        "Content-Type": "application/json",
                        "Notion-Version": "2022-06-28"
                    },
                    json={"children": blocks[i:i+100]}
                )

        print(f"\n✅ Report pushed to Notion!")
        print(f"🔗 {page_url}")
        return page_url
    else:
        print(f"\n❌ Notion push failed: {response.status_code} — {response.text}")
        return ""


def _parse_markdown_to_blocks(content: str) -> list:
    """Convert markdown text into Notion block objects."""
    blocks = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text",
                    "text": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text",
                    "text": {"content": line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text",
                    "text": {"content": line[2:]}}]}})
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text",
                    "text": {"content": line[2:]}}]}})
        elif len(line) > 2 and line[0].isdigit() and line[1] == ".":
            blocks.append({"object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"type": "text",
                    "text": {"content": line[3:]}}]}})
        elif line.startswith("**") and line.endswith("**"):
            blocks.append({"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text",
                    "text": {"content": line.replace("**", "")},
                    "annotations": {"bold": True}}]}})
        elif line.startswith("|") and "---" in line:
            continue
        elif line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            clean_cells = [c for c in cells if c]
            if clean_cells:
                blocks.append({"object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"type": "text",
                        "text": {"content": " | ".join(clean_cells)}}]}})
        else:
            clean = line.replace("**", "")
            if len(clean) > 1900:
                for i in range(0, len(clean), 1900):
                    blocks.append({"object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text",
                            "text": {"content": clean[i:i+1900]}}]}})
            else:
                blocks.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text",
                        "text": {"content": clean}}]}})

    return blocks
