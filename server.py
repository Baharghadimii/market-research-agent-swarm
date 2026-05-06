"""
server.py — Flask backend for Swarm Research UI

Architecture:
  1. Planner (Claude Sonnet) reads the user prompt and decides:
     - what domain/angle to research
     - which agents to run and with what brief
  2. Executor agents run in parallel (cheap models)
  3. Summarizer compresses each agent output (Gemini Flash)
  4. Synthesizer (Claude Sonnet) produces the final structured JSON report
"""

import os
import json
import time
import threading
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI
from crewai import Agent, Task, Crew
from config import BUDGET_MODEL, SMART_CHEAP_MODEL, INSIGHT_MODEL, load_skill
from tools.search import serper_search

app = Flask(__name__, static_folder="static")

# ── OpenRouter client (for planner + summarizer direct calls) ──
client = OpenAI(
    api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    base_url="https://openrouter.ai/api/v1"
)

SONNET  = "anthropic/claude-sonnet-4-5"
FLASH   = "google/gemini-2.0-flash-lite-001"

# ────────────────────────────────────────────────
# COST TRACKING
# ────────────────────────────────────────────────
COST_PER_1M = {
    SONNET: {"input": 3.00,  "output": 15.00},
    FLASH:  {"input": 0.075, "output": 0.30},
    "deepseek": {"input": 0.14, "output": 0.28},
}

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def estimate_cost(model: str, input_text: str, output_text: str) -> float:
    prices = COST_PER_1M.get(model, {"input": 1.0, "output": 1.0})
    return (
        estimate_tokens(input_text)  * prices["input"] +
        estimate_tokens(output_text) * prices["output"]
    ) / 1_000_000

# ────────────────────────────────────────────────
# STEP 1 — PLANNER
# Reads user prompt → decides which agents to run + what brief each gets
# ────────────────────────────────────────────────
PLANNER_SYSTEM = """You are a research planner for a market gap analysis swarm.

Given a user's research prompt, you decide:
1. The domain name (short, e.g. "Speech Delay Apps")
2. Which of the 4 available agents to run (you can skip agents that aren't relevant)
3. A focused brief for each agent you select

Available agents:
- amazon_scanner: finds gaps in Amazon products — use when there are physical products or apps with reviews
- reddit_miner: mines Reddit communities for unmet needs — always useful
- trends_tracker: analyzes search trend lifecycle — always useful  
- competitor_watcher: maps competitive whitespace — always useful

Return ONLY valid JSON, no preamble, no markdown:
{
  "domain": "short domain name",
  "agents": {
    "amazon_scanner": "specific brief for this agent, or null to skip",
    "reddit_miner": "specific brief for this agent, or null to skip",
    "trends_tracker": "specific brief for this agent, or null to skip",
    "competitor_watcher": "specific brief for this agent, or null to skip"
  }
}

Keep each brief to 2-3 sentences max. Be specific to the user's domain.
If amazon_scanner is not relevant (e.g. pure service/app domain with no physical products), set it to null."""

def run_planner(prompt: str) -> dict:
    """Ask Sonnet to plan the research strategy."""
    response = client.chat.completions.create(
        model=SONNET,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=600,
        temperature=0.3
    )
    raw = response.choices[0].message.content.strip()
    # strip markdown code fences if present
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ────────────────────────────────────────────────
# STEP 2 — DYNAMIC AGENT RUNNER
# Spins up only the agents the planner selected
# ────────────────────────────────────────────────
def run_agent(agent_name: str, brief: str) -> str:
    """Run a single research agent with a custom brief."""

    # Pick model based on agent type
    if agent_name in ("amazon_scanner", "trends_tracker"):
        llm = BUDGET_MODEL
    else:
        llm = SMART_CHEAP_MODEL

    # Load the base skill for this agent
    skill_map = {
        "amazon_scanner":    "amazon-scanner",
        "reddit_miner":      "reddit-miner",
        "trends_tracker":    "trends-tracker",
        "competitor_watcher":"competitor-watcher",
    }
    skill_name = skill_map.get(agent_name, agent_name)
    backstory = load_skill(skill_name)

    agent = Agent(
        role=agent_name.replace("_", " ").title(),
        goal=f"Research market gaps in this specific area: {brief}",
        backstory=backstory,
        tools=[serper_search],
        llm=llm,
        verbose=False
    )

    task = Task(
        description=(
            f"{brief}\n\n"
            "Return your findings as structured JSON as defined in your skill. "
            "Be specific and concise. Focus only on what's relevant to this brief."
        ),
        expected_output="Structured JSON findings relevant to the brief.",
        agent=agent
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())
    time.sleep(10)  # rate limit buffer
    return result

def run_agents_parallel(agent_briefs: dict) -> dict:
    """Run selected agents in parallel using threads."""
    results = {}
    errors  = {}
    threads = []

    def run_one(name, brief):
        try:
            results[name] = run_agent(name, brief)
        except Exception as e:
            errors[name] = str(e)
            results[name] = f"Agent failed: {e}"

    for name, brief in agent_briefs.items():
        if brief:  # skip nulled agents
            t = threading.Thread(target=run_one, args=(name, brief))
            threads.append(t)
            t.start()

    for t in threads:
        t.join()

    return results

# ────────────────────────────────────────────────
# STEP 3 — SUMMARIZER
# Compresses each agent's raw output before Sonnet sees it
# ────────────────────────────────────────────────
SUMMARIZER_PROMPT = """You are a signal extractor. Compress this agent's findings into 120-150 words of plain English.
Lead with the top 2-3 opportunities by name. Include core evidence (1 sentence per signal).
Note any surprising findings. No JSON, no structure — plain paragraphs only.
Output only the summary. No preamble."""

def summarize_one(agent_name: str, raw: str) -> str:
    response = client.chat.completions.create(
        model=FLASH,
        messages=[
            {"role": "system", "content": SUMMARIZER_PROMPT},
            {"role": "user",   "content": f"Agent: {agent_name}\n\n{raw}"}
        ],
        max_tokens=250,
        temperature=0.2
    )
    return response.choices[0].message.content.strip()

def summarize_all(agent_results: dict) -> dict:
    """Summarize all agent outputs in parallel."""
    summaries = {}
    threads   = []

    def summarize_one_thread(name, raw):
        summaries[name] = summarize_one(name, raw)

    for name, raw in agent_results.items():
        t = threading.Thread(target=summarize_one_thread, args=(name, raw))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return summaries

# ────────────────────────────────────────────────
# STEP 4 — SYNTHESIZER
# Sonnet reads compressed summaries → produces structured JSON report
# ────────────────────────────────────────────────
SYNTHESIZER_SYSTEM = """You are a market research synthesizer. You receive compressed findings from multiple research agents.

Apply the 4-source convergence test:
- 4 sources = lead with it
- 3 sources = strong opportunity  
- 2 sources = worth testing cheaply
- 1 source  = watch list only

Return ONLY valid JSON, no preamble, no markdown fences:
{
  "domain": "Research domain name",
  "the_call": {
    "recommended_focus": "one sentence",
    "for_whom": "specific tribe",
    "why_now": "one sentence on timing",
    "validation_step": "one concrete action this week"
  },
  "opportunities": [
    {
      "name": "Opportunity name",
      "what": "product/angle in one sentence",
      "who": "specific tribe",
      "signal_convergence": "X/4 sources — name them",
      "wedge": "why this beats incumbents",
      "capital_to_validate": "Low/Medium/High — what it takes",
      "timing": "why window is open",
      "risk": "most likely reason this fails",
      "validation_plan": ["step 1", "step 2", "step 3"]
    }
  ],
  "watch_list": ["item 1", "item 2"],
  "contradictions": ["contradiction 1", "contradiction 2"],
  "excluded": ["what was excluded and why"]
}

Make the call. Don't hedge. Max 3 opportunities."""

def run_synthesizer(domain: str, summaries: dict, prompt: str) -> dict:
    """Sonnet synthesizes all compressed findings into final JSON report."""

    # Build context block
    context = f"User research prompt: {prompt}\nDomain: {domain}\n\n"
    for name, summary in summaries.items():
        label = name.replace("_", " ").title()
        context += f"=== {label} ===\n{summary}\n\n"

    response = client.chat.completions.create(
        model=SONNET,
        messages=[
            {"role": "system", "content": SYNTHESIZER_SYSTEM},
            {"role": "user",   "content": context}
        ],
        max_tokens=2000,
        temperature=0.4
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw), context

# ────────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/research", methods=["POST"])
def research():
    data   = request.get_json()
    prompt = (data or {}).get("prompt", "").strip()

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    total_cost = 0.0

    try:
        # ── Step 1: Plan ──
        plan = run_planner(prompt)
        total_cost += estimate_cost(SONNET, prompt, json.dumps(plan))

        domain       = plan.get("domain", "Market Research")
        agent_briefs = {k: v for k, v in plan.get("agents", {}).items() if v}

        if not agent_briefs:
            return jsonify({"error": "Planner returned no agents to run"}), 500

        # ── Step 2: Run agents in parallel ──
        agent_results = run_agents_parallel(agent_briefs)

        # ── Step 3: Summarize in parallel ──
        summaries = summarize_all(agent_results)
        for name, summary in summaries.items():
            total_cost += estimate_cost(FLASH, agent_results[name], summary)

        # ── Step 4: Synthesize ──
        report, synth_input = run_synthesizer(domain, summaries, prompt)
        total_cost += estimate_cost(SONNET, synth_input, json.dumps(report))

        report["cost_usd"] = round(total_cost, 4)
        return jsonify(report)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON parse error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Debug=False for production on VPS
    # Use 0.0.0.0 so it's accessible from outside the VPS
    app.run(host="0.0.0.0", port=5000, debug=False)