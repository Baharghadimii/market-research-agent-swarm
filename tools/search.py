import os
from dotenv import load_dotenv
load_dotenv()
import requests


def serper_results(query: str) -> list:
    """Search Google via Serper. Returns [{title, snippet, link}].

    These are leads and competitor names only. Snippets are fragments, so
    they are never treated as evidence of what people said.
    """
    response = requests.post(
        "https://google.serper.dev/search",
        headers={
            "X-API-KEY": os.getenv("SERPER_API_KEY"),
            "Content-Type": "application/json"
        },
        json={"q": query, "num": 10},
        timeout=20
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "snippet": r.get("snippet", ""), "link": r.get("link", "")}
        for r in response.json().get("organic", [])
    ]


def serper_search(query: str) -> str:
    """Same search, formatted as text for a prompt."""
    results = serper_results(query)
    if not results:
        return "No results found."
    return "\n".join(f"- {r['title']}: {r['snippet']} ({r['link']})" for r in results)
