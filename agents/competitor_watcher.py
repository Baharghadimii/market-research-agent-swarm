import time
from crewai import Agent, Task, Crew
from config import SMART_CHEAP_MODEL, load_skill
from tools.search import serper_search


def run() -> str:
    """Run the Competitor Watcher agent and return its findings as a string."""

    agent = Agent(
        role="Vancouver Baby Products Competitive Analyst",
        goal=(
            "Map competitive whitespace in Vancouver baby products — "
            "find the angles no one is taking."
        ),
        backstory=load_skill("competitor-watcher"),
        tools=[serper_search],
        llm=SMART_CHEAP_MODEL,
        verbose=False
    )

    task = Task(
        description=(
            "Research Vancouver baby product competitors and Canadian "
            "Etsy/DTC sellers. "
            "Map positioning across audience, price tier, emotional "
            "positioning, channel, and form factor. "
            "Identify consensus assumptions and whitespace. "
            "Return JSON as specified in your skill."
        ),
        expected_output=(
            "JSON with competitor landscape, consensus assumptions, "
            "whitespace map, top 3 positioning opportunities."
        ),
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())
    time.sleep(15)
    return result
