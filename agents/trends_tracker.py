import time
from crewai import Agent, Task, Crew
from config import BUDGET_MODEL, load_skill
from tools.search import serper_search


def run() -> str:
    """Run the Trends Tracker agent and return its findings as a string."""

    agent = Agent(
        role="Canadian Baby Products Trend Analyst",
        goal=(
            "Identify rising baby product trends in Canada and BC "
            "with lifecycle stage classification."
        ),
        backstory=load_skill("trends-tracker"),
        tools=[serper_search],
        llm=BUDGET_MODEL,
        verbose=False
    )

    task = Task(
        description=(
            "Research baby product trends in Canada/BC for 2025-2026. "
            "Classify each category by lifecycle stage "
            "(whisper/climb/peak/decline/mature). "
            "Identify adjacent opportunities and false trends. "
            "Return JSON as specified in your skill."
        ),
        expected_output=(
            "JSON with lifecycle assessments, adjacent opportunities, "
            "false trends flagged, timing calls."
        ),
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())
    time.sleep(15)
    return result
