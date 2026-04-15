from dotenv import load_dotenv
load_dotenv()

import agents.amazon_scanner as amazon_scanner
import agents.reddit_miner as reddit_miner
import agents.trends_tracker as trends_tracker
import agents.competitor_watcher as competitor_watcher
import agents.orchestrator as orchestrator
from delivery import notion, email

# ----------------------------
# Phase 1: Data collection
# Agents run independently — no agent sees another's results (zero bias)
# ----------------------------

print("\n🔍 Running Amazon Scanner...  [gemini-flash]\n")
amazon_result = amazon_scanner.run()
print("✅ Amazon Scanner done!\n")

print("🔍 Running Reddit Miner...  [deepseek]\n")
reddit_result = reddit_miner.run()
print("✅ Reddit Miner done!\n")

print("🔍 Running Trends Tracker...  [gemini-flash]\n")
trends_result = trends_tracker.run()
print("✅ Trends Tracker done!\n")

print("🔍 Running Competitor Watcher...  [deepseek]\n")
competitor_result = competitor_watcher.run()
print("✅ Competitor Watcher done!\n")

# ----------------------------
# Phase 2: Orchestration
# Sonnet reads all 4 findings, applies convergence test, makes the call
# ----------------------------

print("🧠 Running Orchestrator...  [claude-sonnet]\n")
final_report = orchestrator.run(
    amazon=amazon_result,
    reddit=reddit_result,
    trends=trends_result,
    competitor=competitor_result
)
print("✅ Orchestrator done!\n")

# ----------------------------
# Phase 3: Delivery
# ----------------------------

print("\n\n========== FINAL MARKET RESEARCH REPORT ==========")
print(final_report)

notion_url = notion.push(final_report)
email.send(final_report, notion_url or "https://notion.so")
