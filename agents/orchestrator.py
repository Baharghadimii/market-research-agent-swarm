from crewai import Agent, Task, Crew
from config import INSIGHT_MODEL, load_skill


def run(amazon: str, reddit: str, trends: str, competitor: str) -> str:
    """
    Run the Orchestrator agent.
    Receives findings from all 4 agents, applies the 4-source convergence
    test, and returns the final market research report.
    """

    agent = Agent(
        role="Market Research Orchestrator",
        goal=(
            "Synthesize all research findings into a decisive, actionable "
            "market research report."
        ),
        backstory=load_skill("orchestrator"),
        llm=INSIGHT_MODEL,
        verbose=True
    )

    task = Task(
        description=(
            f"You have received independent research from 4 agents. "
            f"Apply the 4-source convergence test, contradiction detector, "
            f"and opportunity ranking criteria from your skill to produce "
            f"the final report.\n\n"
            f"Context: Vancouver baby products business, "
            f"$10,000 CAD starting capital.\n\n"
            f"=== AMAZON FINDINGS ===\n{amazon}\n\n"
            f"=== REDDIT FINDINGS ===\n{reddit}\n\n"
            f"=== TRENDS FINDINGS ===\n{trends}\n\n"
            f"=== COMPETITOR FINDINGS ===\n{competitor}\n\n"
            "Produce the full report using the exact output format from "
            "your skill. Make the call. Don't hedge."
        ),
        expected_output=(
            "A complete market research report in the exact markdown "
            "structure defined in the skill: The Call, Top 3 Opportunities, "
            "Watch List, Contradictions, Excluded findings, "
            "Methodology notes."
        ),
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=True)
    return str(crew.kickoff())
