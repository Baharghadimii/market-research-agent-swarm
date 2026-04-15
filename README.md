# Market Research Agent Swarm

A multi-agent AI system that runs quarterly market research for a Vancouver baby products business and delivers a report to Notion.

## Architecture

```
main.py                        ← runs the full pipeline
├── agents/
│   ├── amazon_scanner.py      ← Gemini Flash  — scans Amazon.ca for gaps
│   ├── reddit_miner.py        ← DeepSeek V3   — mines Vancouver parenting subreddits
│   ├── trends_tracker.py      ← Gemini Flash  — classifies trend lifecycle stages
│   ├── competitor_watcher.py  ← DeepSeek V3   — maps competitive whitespace
│   └── orchestrator.py        ← Claude Sonnet — synthesizes, makes the call
├── skills/                    ← agent instructions (markdown)
├── delivery/
│   ├── notion.py              ← pushes report to Notion
│   └── email.py               ← sends Gmail notification
└── tools/
    └── search.py              ← Serper web search tool
```

## How it works

1. Four data collection agents run independently in sequence (no agent sees another's results)
2. Each returns structured JSON findings
3. The Orchestrator applies a 4-source convergence test — opportunities appearing across multiple agents score higher
4. Final report is pushed to Notion and emailed

## Models (via OpenRouter)

| Agent | Model | Why |
|-------|-------|-----|
| Amazon Scanner | Gemini Flash | Structured extraction only |
| Trends Tracker | Gemini Flash | Mechanical summarization |
| Reddit Miner | DeepSeek V3 | Needs reading comprehension |
| Competitor Watcher | DeepSeek V3 | Needs positioning nuance |
| Orchestrator | Claude Sonnet | Judgment and synthesis |

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/market-research-agent.git
cd market-research-agent

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Add your API keys
cp .env.example .env
# Edit .env with your keys

# 5. Run
python3 main.py
```

## Environment variables

```
OPENROUTER_API_KEY=     # from openrouter.ai/keys
SERPER_API_KEY=         # from serper.dev
NOTION_API_KEY=         # from notion.so/my-integrations
GMAIL_APP_PASSWORD=     # from Google account app passwords
```

## Cron job (quarterly)

```bash
# Runs on Jan 1, Apr 1, Jul 1, Oct 1 at 8am
0 8 1 1,4,7,10 * cd /root/market-research-agent && source venv/bin/activate && python3 main.py >> logs/run.log 2>&1
```
