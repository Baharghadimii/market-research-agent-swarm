from dotenv import load_dotenv
load_dotenv()

import time
import json
import os
import re
from openai import OpenAI  # OpenRouter uses OpenAI-compatible SDK

import agents.amazon_scanner as amazon_scanner
import agents.reddit_miner as reddit_miner
import agents.trends_tracker as trends_tracker
import agents.competitor_watcher as competitor_watcher
import agents.orchestrator as orchestrator
from delivery import notion, email

# ----------------------------
# Cost config (OpenRouter prices per 1M tokens, as of May 2026)
# Update these if prices change: https://openrouter.ai/models
# ----------------------------
COST_PER_1M = {
    "gemini-flash":  {"input": 0.075, "output": 0.30},   # Gemini 2.0 Flash Lite
    "deepseek":      {"input": 0.14,  "output": 0.28},   # DeepSeek Chat
    "gemini-flash-summarizer": {"input": 0.075, "output": 0.30},  # same model, tracked separately
    "claude-sonnet": {"input": 3.00,  "output": 15.00},  # Claude Sonnet 4.5
}

# Rough token estimator: ~4 chars per token
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def estimate_cost(model_key: str, input_text: str, output_text: str) -> float:
    prices = COST_PER_1M[model_key]
    input_tokens  = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000

# ----------------------------
# Summarizer: compresses each agent's raw JSON into ~150 word signal summary
# Uses cheap Gemini Flash so Sonnet gets clean, dense input
# ----------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

summarizer_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)

SUMMARIZER_MODEL = "google/gemini-2.0-flash-lite-001"

SUMMARIZER_PROMPT = """You are a signal extractor. You receive raw JSON findings from a market research agent.
Your job: compress it into a 120-150 word plain-English summary of ONLY the strongest signals.

Rules:
- Lead with the top 2-3 opportunities by name
- Include the core evidence for each (1 sentence max per signal)
- Note any surprising contradictions or anomalies
- Do NOT repeat methodology, structure, or categories
- Do NOT use JSON — plain paragraphs only
- Be ruthlessly concise. The reader already knows the framework.

Output only the summary. No preamble, no label."""

def summarize_agent_output(agent_name: str, raw_output: str) -> tuple[str, float]:
    """Compress agent JSON output to ~150 words using cheap Gemini Flash."""
    input_text = f"Agent: {agent_name}\n\nRaw findings:\n{raw_output}"

    response = summarizer_client.chat.completions.create(
        model=SUMMARIZER_MODEL,
        messages=[
            {"role": "system", "content": SUMMARIZER_PROMPT},
            {"role": "user",   "content": input_text}
        ],
        max_tokens=250,
        temperature=0.2
    )

    summary = response.choices[0].message.content.strip()
    cost = estimate_cost("gemini-flash-summarizer", input_text, summary)
    return summary, cost

# ----------------------------
# Cost tracker
# ----------------------------
costs = {}

# ----------------------------
# Phase 1: Data collection
# Agents run independently — no agent sees another's results (zero bias)
# ----------------------------

print("\n🔍 Running Amazon Scanner... [gemini-flash]\n")
t0 = time.time()
amazon_raw = amazon_scanner.run()
costs["amazon_scanner"] = estimate_cost("gemini-flash", "amazon scan task", amazon_raw)
print(f"✅ Amazon Scanner done! ({time.time()-t0:.1f}s)\n")

print("🔍 Running Reddit Miner... [deepseek]\n")
t0 = time.time()
reddit_raw = reddit_miner.run()
costs["reddit_miner"] = estimate_cost("deepseek", "reddit mine task", reddit_raw)
print(f"✅ Reddit Miner done! ({time.time()-t0:.1f}s)\n")

print("🔍 Running Trends Tracker... [gemini-flash]\n")
t0 = time.time()
trends_raw = trends_tracker.run()
costs["trends_tracker"] = estimate_cost("gemini-flash", "trends track task", trends_raw)
print(f"✅ Trends Tracker done! ({time.time()-t0:.1f}s)\n")

print("🔍 Running Competitor Watcher... [deepseek]\n")
t0 = time.time()
competitor_raw = competitor_watcher.run()
costs["competitor_watcher"] = estimate_cost("deepseek", "competitor watch task", competitor_raw)
print(f"✅ Competitor Watcher done! ({time.time()-t0:.1f}s)\n")

# ----------------------------
# Phase 1.5: Summarization
# Compress each agent's raw output before feeding to Sonnet
# This is the cost optimization step — Gemini Flash does the compression
# ----------------------------

print("⚡ Summarizing agent outputs... [gemini-flash x4]\n")

amazon_summary,     cost_sum_amazon     = summarize_agent_output("Amazon Scanner",      amazon_raw)
reddit_summary,     cost_sum_reddit     = summarize_agent_output("Reddit Miner",        reddit_raw)
trends_summary,     cost_sum_trends     = summarize_agent_output("Trends Tracker",      trends_raw)
competitor_summary, cost_sum_competitor = summarize_agent_output("Competitor Watcher",  competitor_raw)

costs["summarizer"] = cost_sum_amazon + cost_sum_reddit + cost_sum_trends + cost_sum_competitor

# Log compression ratio so you can see the savings
raw_total     = len(amazon_raw) + len(reddit_raw) + len(trends_raw) + len(competitor_raw)
summary_total = len(amazon_summary) + len(reddit_summary) + len(trends_summary) + len(competitor_summary)
compression   = (1 - summary_total / max(raw_total, 1)) * 100
print(f"✅ Summarization done! Compressed {raw_total:,} → {summary_total:,} chars ({compression:.0f}% reduction)\n")

# ----------------------------
# Phase 2: Orchestration
# Sonnet reads the 4 compressed summaries, applies convergence test, makes the call
# ----------------------------

print("🧠 Running Orchestrator... [claude-sonnet]\n")
t0 = time.time()

# Build orchestrator input so we can measure it for cost tracking
orchestrator_input = (
    f"=== AMAZON FINDINGS ===\n{amazon_summary}\n\n"
    f"=== REDDIT FINDINGS ===\n{reddit_summary}\n\n"
    f"=== TRENDS FINDINGS ===\n{trends_summary}\n\n"
    f"=== COMPETITOR FINDINGS ===\n{competitor_summary}"
)

final_report = orchestrator.run(
    amazon=amazon_summary,
    reddit=reddit_summary,
    trends=trends_summary,
    competitor=competitor_summary
)

costs["orchestrator"] = estimate_cost("claude-sonnet", orchestrator_input, str(final_report))
print(f"✅ Orchestrator done! ({time.time()-t0:.1f}s)\n")

# ----------------------------
# Cost summary
# ----------------------------
total_cost = sum(costs.values())

print("\n========== 💰 COST BREAKDOWN ==========")
for agent, cost in costs.items():
    print(f"  {agent:<25} ${cost:.4f}")
print(f"  {'─'*35}")
print(f"  {'TOTAL':<25} ${total_cost:.4f}")
print(f"  {'Quarterly (x4 runs)':<25} ${total_cost * 4:.4f}")
print("========================================\n")

# ----------------------------
# Phase 3: Delivery
# ----------------------------

print("\n========== FINAL MARKET RESEARCH REPORT ==========")
print(final_report)

notion_url = notion.push(final_report)
email.send(final_report, notion_url or "https://notion.so")