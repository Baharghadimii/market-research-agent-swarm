import os
from dotenv import load_dotenv
load_dotenv()
import requests


def serper_results(query: str, past_year: bool = False) -> list:
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
        json={"q": query, "num": 10, **({"tbs": "qdr:y"} if past_year else {})},
        timeout=20
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "snippet": r.get("snippet", ""), "link": r.get("link", ""),
         "date": r.get("date", "")}
        for r in response.json().get("organic", [])
    ]


def serper_search(query: str) -> str:
    """Same search, formatted as text for a prompt."""
    results = serper_results(query)
    if not results:
        return "No results found."
    return "\n".join(f"- {r['title']}: {r['snippet']} ({r['link']})" for r in results)
