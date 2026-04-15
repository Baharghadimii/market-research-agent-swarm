import time
from crewai import Agent, Task, Crew
from config import BUDGET_MODEL, load_skill
from tools.search import serper_search


def run() -> str:
    """Run the Amazon Scanner agent and return its findings as a string."""

    agent = Agent(
        role="Amazon Baby Products Analyst",
        goal=(
            "Find high-demand baby products on Amazon.ca with complaints signaling "
            "market gaps for Vancouver families."
        ),
        backstory=load_skill("amazon-scanner"),
        tools=[serper_search],
        llm=BUDGET_MODEL,
        verbose=False
    )

    task = Task(
        description=(
            "Search Amazon.ca for bestselling baby products: carriers, strollers, "
            "sleep aids, feeding gear, eco-friendly items. "
            "Find top 3 market gaps Canadian parents complain about. "
            "Return JSON as specified in your skill."
        ),
        expected_output=(
            "JSON with settling signals, price cliffs, feature gaps "
            "for top 3 opportunities."
        ),
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())
    time.sleep(15)
    return result
