import os
import requests
from crewai.tools import tool


@tool("Web Search")
def serper_search(query: str) -> str:
    """Search the web using Serper Google Search API."""
    response = requests.post(
        "https://google.serper.dev/search",
        headers={
            "X-API-KEY": os.getenv("SERPER_API_KEY"),
            "Content-Type": "application/json"
        },
        json={"q": query, "num": 10}
    )
    if response.status_code != 200:
        return f"Search failed: {response.status_code}"

    results = response.json().get("organic", [])
    output = []
    for r in results:
        output.append(f"- {r.get('title')}: {r.get('snippet')} ({r.get('link')})")
    return "\n".join(output) if output else "No results found."
