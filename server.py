"""
server.py — Flask backend for Swarm Research UI

True planner-executor architecture:
  1. Planner (Sonnet) reads the prompt and dynamically designs the entire
     research strategy from scratch — what sources, what queries, what signals.
     NO hardcoded source types, platforms, or categories.
  2. Researchers execute briefs using direct web search (no CrewAI needed).
  3. Summarizer (Flash) compresses each researcher's output.
  4. Synthesizer (Sonnet) produces the final structured JSON report.
"""

import os
import json
import time
import threading
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI
from tools.search import serper_search

app = Flask(__name__, static_folder="static")

# ── OpenRouter client ──
client = OpenAI(
    api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    base_url="https://openrouter.ai/api/v1"
)

SONNET = "anthropic/claude-sonnet-4-5"
FLASH  = "google/gemini-2.0-flash-lite-001"

# Bounded for cost predictability
MAX_RESEARCHERS = 6
MIN_RESEARCHERS = 3

# ────────────────────────────────────────────────
# COST TRACKING
# ────────────────────────────────────────────────
COST_PER_1M = {
    SONNET: {"input": 3.00,  "output": 15.00},
    FLASH:  {"input": 0.075, "output": 0.30},
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
# Reads prompt → designs entire research strategy from scratch.
# No hardcoded categories, platforms, or source types.
# ────────────────────────────────────────────────
PLANNER_SYSTEM = f"""You are a master research planner for a market gap analysis swarm.

Given ANY user prompt — an app, product, service, physical place, B2B tool, anything —
you design the entire research strategy from scratch every single time.

Think carefully:
1. What exactly IS this thing? (mobile app? physical product? B2B SaaS? local service?)
2. Where do REAL users of this thing express frustration, wishes, complaints?
   Think beyond obvious places — specific subreddits, niche forums, professional communities,
   app store reviews, Facebook groups, specialized review sites, YouTube comments, etc.
3. Where do competitors exist and how can we observe their weaknesses?
4. What specific signal would prove a market gap exists here?

Produce {MIN_RESEARCHERS}-{MAX_RESEARCHERS} research briefs.
Each brief must investigate a COMPLETELY DIFFERENT angle and source.
No two briefs should overlap in source type or signal being sought.

Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "domain": "Short domain name (e.g. Speech Delay Apps, Vancouver Coffee Shops, B2B Kafka Tools)",
  "thinking": "2-3 sentences explaining WHY you chose these specific sources for this specific prompt",
  "researchers": [
    {{
      "id": "unique-short-id",
      "source": "Very specific source (e.g. r/speechdelays and r/toddlers, NOT just Reddit)",
      "queries": ["specific query 1", "specific query 2"],
      "looking_for": "Exact signal that indicates a gap (complaints, wishes, workarounds)",
      "expected_output": "What kind of data to surface (quotes, product names, patterns)"
    }}
  ]
}}

Examples of GOOD sources: "r/speechdelays subreddit", "App Store 1-3 star reviews of Speech Blubs",
"BabyCenter speech delay forum threads", "SLP professional Facebook groups",
"YouTube comments on speech therapy videos"

Examples of BAD sources: "Reddit", "App stores", "Social media", "Forums"

Be ruthlessly specific. The researchers can only search for what you tell them to search for."""

def run_planner(prompt: str):
    response = client.chat.completions.create(
        model=SONNET,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user",   "content": prompt}
        ],
        max_tokens=1500,
        temperature=0.4
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    plan = json.loads(raw)

    # Enforce bounds
    researchers = plan.get("researchers", [])[:MAX_RESEARCHERS]
    if len(researchers) < MIN_RESEARCHERS:
        raise ValueError(f"Planner returned only {len(researchers)} researchers (min {MIN_RESEARCHERS})")
    plan["researchers"] = researchers

    cost = estimate_cost(SONNET, PLANNER_SYSTEM + prompt, raw)
    return plan, cost

# ────────────────────────────────────────────────
# STEP 2 — RESEARCHER
# Executes one brief using direct web search calls.
# No CrewAI, no LLM — just search and return raw results.
# The intelligence is in the planner (what to search) and
# synthesizer (what it means). Researchers just fetch data.
# ────────────────────────────────────────────────
def run_researcher(brief: dict) -> str:
    """Execute one research brief using direct search calls."""
    all_results = []

    for query in brief.get("queries", []):
        try:
            result = serper_search.run(query)
            all_results.append(f"Query: {query}\n{result}")
            time.sleep(1)  # small rate limit buffer between queries
        except Exception as e:
            all_results.append(f"Query: {query}\nSearch failed: {e}")

    source = brief.get("source", "unknown source")
    looking_for = brief.get("looking_for", "")
    header = f"Source investigated: {source}\nLooking for: {looking_for}\n\n"
    return header + "\n\n---\n\n".join(all_results)

def run_researchers_parallel(researchers: list) -> dict:
    """Run all researcher briefs in parallel using threads."""
    results = {}
    lock    = threading.Lock()
    threads = []

    def run_one(brief):
        rid = brief.get("id", f"r-{id(brief)}")
        try:
            output = run_researcher(brief)
            with lock:
                results[rid] = {"brief": brief, "output": output}
        except Exception as e:
            with lock:
                results[rid] = {
                    "brief":  brief,
                    "output": f"Researcher failed: {e}",
                    "error":  str(e)
                }

    for brief in researchers:
        t = threading.Thread(target=run_one, args=(brief,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return results

# ────────────────────────────────────────────────
# STEP 3 — SUMMARIZER
# Flash compresses raw search results into signal-dense summaries.
# This is where the cheap LLM earns its keep —
# turning walls of search snippets into 150 words of signal.
# ────────────────────────────────────────────────
SUMMARIZER_PROMPT = """You compress raw web search results into 120-150 words of plain English signal.

Your job:
- Extract the strongest evidence of market gaps (complaints, wishes, workarounds)
- Name specific products, communities, or patterns you see
- Note if the source had thin data (be honest)
- Surface anything surprising or contradictory

No JSON, no headers, no bullets — flowing paragraphs only.
Output only the summary. No preamble."""

def summarize_one(rid: str, raw: str, source: str):
    response = client.chat.completions.create(
        model=FLASH,
        messages=[
            {"role": "system", "content": SUMMARIZER_PROMPT},
            {"role": "user",   "content": f"Source: {source}\n\nRaw search results:\n{raw}"}
        ],
        max_tokens=300,
        temperature=0.2
    )
    summary = response.choices[0].message.content.strip()
    cost    = estimate_cost(FLASH, raw, summary)
    return summary, cost

def summarize_all(researcher_results: dict):
    """Summarize all researcher outputs in parallel."""
    summaries  = {}
    total_cost = 0.0
    threads    = []
    lock       = threading.Lock()

    def summarize_thread(rid, raw, source):
        nonlocal total_cost
        summary, cost = summarize_one(rid, raw, source)
        with lock:
            summaries[rid] = summary
            total_cost += cost

    for rid, data in researcher_results.items():
        source = data["brief"].get("source", rid)
        t = threading.Thread(
            target=summarize_thread,
            args=(rid, data["output"], source)
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return summaries, total_cost

# ────────────────────────────────────────────────
# STEP 4 — SYNTHESIZER
# Sonnet reads all compressed summaries → final structured JSON report.
# This is the decision-maker. It applies convergence test and makes the call.
# ────────────────────────────────────────────────
SYNTHESIZER_SYSTEM = """You are a market research synthesizer and decision-maker.

You receive compressed findings from multiple researchers, each investigating a different source.
Your job is NOT to summarize — it's to MAKE THE CALL.

Apply the convergence test:
- Signal appears in most sources → lead with it, high confidence
- Signal appears in some sources → strong opportunity, recommend pursuing
- Signal in two sources → worth testing cheaply
- Signal in one source only → watch list, not action item

Rank opportunities by: signal convergence → tribe specificity → wedge clarity → capital efficiency → timing.

Make the call. Don't hedge. If data is thin, say so plainly and recommend what to validate first.

Return ONLY valid JSON, no preamble, no markdown fences:
{
  "domain": "Research domain name",
  "the_call": {
    "recommended_focus": "one sentence — what to build/pursue",
    "for_whom": "specific tribe, not broad demographic",
    "why_now": "one sentence on timing",
    "validation_step": "one concrete action this week"
  },
  "opportunities": [
    {
      "name": "Opportunity name",
      "what": "product/angle in one sentence",
      "who": "specific tribe",
      "signal_convergence": "X/N sources — name the sources briefly",
      "wedge": "why this beats incumbents for this audience",
      "capital_to_validate": "Low/Medium/High — what it takes",
      "timing": "why this window is open",
      "risk": "most likely reason this fails",
      "validation_plan": ["step 1", "step 2", "step 3"]
    }
  ],
  "watch_list": ["brief item — why watching not acting"],
  "contradictions": ["where sources disagreed and how you resolved it"],
  "excluded": ["what you saw but didn't recommend and why"]
}

Max 3 opportunities. Every word earns its place."""

def run_synthesizer(domain: str, summaries: dict, researcher_results: dict, prompt: str):
    context = f"User prompt: {prompt}\nDomain: {domain}\nSources investigated: {len(summaries)}\n\n"
    for rid, summary in summaries.items():
        source = researcher_results[rid]["brief"].get("source", rid)
        context += f"=== {source} ===\n{summary}\n\n"

    response = client.chat.completions.create(
        model=SONNET,
        messages=[
            {"role": "system", "content": SYNTHESIZER_SYSTEM},
            {"role": "user",   "content": context}
        ],
        max_tokens=2500,
        temperature=0.4
    )
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    report = json.loads(raw)
    cost   = estimate_cost(SONNET, SYNTHESIZER_SYSTEM + context, raw)
    return report, cost

# ────────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/research", methods=["POST"])
def research():
    body   = request.get_json()
    prompt = (body or {}).get("prompt", "").strip()

    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    total_cost = 0.0

    try:
        # ── Step 1: Planner designs strategy from scratch ──
        plan, plan_cost = run_planner(prompt)
        total_cost += plan_cost

        domain      = plan.get("domain", "Market Research")
        researchers = plan.get("researchers", [])

        # ── Step 2: Researchers fetch data in parallel ──
        researcher_results = run_researchers_parallel(researchers)

        # ── Step 3: Summarize in parallel ──
        summaries, sum_cost = summarize_all(researcher_results)
        total_cost += sum_cost

        # ── Step 4: Synthesizer makes the call ──
        report, synth_cost = run_synthesizer(domain, summaries, researcher_results, prompt)
        total_cost += synth_cost

        report["cost_usd"]          = round(total_cost, 4)
        report["sources_used"]      = len(researchers)
        report["planner_thinking"]  = plan.get("thinking", "")
        return jsonify(report)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON parse error: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)