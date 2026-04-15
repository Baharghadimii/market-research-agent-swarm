import time
from crewai import Agent, Task, Crew
from config import SMART_CHEAP_MODEL, load_skill
from tools.search import serper_search


def run() -> str:
    """Run the Reddit Miner agent and return its findings as a string."""

    agent = Agent(
        role="Vancouver Parent Community Analyst",
        goal=(
            "Mine Vancouver and Canadian parenting subreddits for unmet "
            "baby product needs."
        ),
        backstory=load_skill("reddit-miner"),
        tools=[serper_search],
        llm=SMART_CHEAP_MODEL,
        verbose=False
    )

    task = Task(
        description=(
            "Search Reddit in r/vancouver, r/northvancouver, r/beyondthebump, "
            "r/babybumpscanada, r/Mommit, r/NewParents, r/canadianparents. "
            "Find unmet needs around baby gear, sleep aids, feeding, "
            "eco-friendly items. "
            "Return JSON as specified in your skill."
        ),
        expected_output=(
            "JSON with unmet needs, incumbent vulnerabilities, "
            "emerging vocabulary, top 3 opportunities."
        ),
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())
    time.sleep(15)
    return result
