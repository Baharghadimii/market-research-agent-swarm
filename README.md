# Market Research Agent Swarm

A planner-executor AI system that finds market gaps for any product, service, or business idea — dynamically deciding where to look based on what the thing actually is, with no hardcoded source list.

## How it works

1. **Planner (Sonnet)** reads your prompt and designs the entire research strategy from scratch — what kind of thing is this, and where would real users of it actually complain or wish out loud? Produces 3-6 highly specific research briefs (e.g. "r/speechdelays subreddit," not just "Reddit"). No hardcoded platforms or categories — a tech product and a local coffee shop get completely different sources.
2. **Researchers** read real posts and comments through public APIs: Hacker News, Stack Exchange, GitHub issues, App Store reviews, Discourse forums and YouTube. No LLM is involved. Google (Serper) is used only for leads and competitor names, never as evidence.
3. **Extractor (Flash)** copies out exact quotes. Code rejects any quote that doesn't appear in the fetched text, and filters out people promoting their own products.
4. **Synthesizer (Sonnet)** ranks up to 3 opportunities and must cite verified quotes. Code then computes how many different people and platforms support each one, so a model can't inflate the numbers.
5. **Existing solutions check** looks for products already solving each problem, using Google results and labelled as such.

Reddit, Facebook and Discord can't be read, and every report says so. Each report lists Reddit threads worth reading, found through Google. Whole Reddit or Facebook pages (Cmd+A, Cmd+C) can be pasted in; code strips the page clutter and detects comment authors before the cheapest model reads them. Pasted comments go through the same quote checks but are labelled as pasted, because their origin can't be confirmed, and they cannot raise an idea's ranking. YouTube needs a free `YOUTUBE_API_KEY`; `GITHUB_TOKEN` is optional and raises GitHub's rate limit.

## Using it

- **Live progress** shows what each step is doing while a report runs.
- **Saved reports** stay on the server. Open past reports from the home screen, share a report's link, or delete it.
- **What changed:** a rerun of the same question shows new quotes and ideas that appeared or disappeared since the last run.
- **Dig deeper** on any idea runs a focused second pass on just that idea.
- **Track monthly** reruns a question every 30 days (public sources only, since pasted comments aren't stored).
- **Signs people would pay** flags quotes where people mention what they pay, say they'd pay, or describe switching products.
- **Competitor strength** shows App Store ratings, rating counts and prices for products that already exist.
- **Notion copy:** every finished report, deep dive and tracked run is copied to a Swarm Reports database automatically, which doubles as a backup (needs `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`; set `AUTO_NOTION=off` to stop it). **Download** saves a report as Markdown.
- **Daily spending limit:** runs are refused once the day's spending would pass `DAILY_LIMIT_USD` (default $3, Vancouver time). Reports, spending and trackers are stored in `data/swarm.db`.

## Architecture

```
server.py                      Flask backend, full pipeline lives here
├── run_planner()               Sonnet designs the research strategy
├── run_researchers_parallel()  parallel web search execution
├── summarize_all()             Flash compresses raw results
├── run_synthesizer()           Sonnet makes the final call
tools/
├── evidence.py                 public-API connectors, quote checks, people counts
├── store.py                    saved reports, daily spending, trackers (SQLite)
├── compare.py                  what changed between two runs of a question
├── notion_export.py            send a report to a Notion database
└── search.py                   Serper web search tool
model_config.json               model lists + fallback chains (see below)
static/                         frontend UI

```

## Methodology transparency

Every report includes a `methodology` field — the exact sources, search queries, and signals investigated for that specific run. This makes the research auditable rather than a black box: anyone reading a report can see precisely what was searched and why.

## Models (via OpenRouter)


| Role        | Model         | Why                                      |
| ----------- | ------------- | ---------------------------------------- |
| Planner     | Claude Sonnet | Judgment — designs strategy from scratch |
| Researchers | *(no LLM)*    | Direct web search only                   |
| Summarizer  | Gemini Flash  | Cheap, high-volume compression           |
| Synthesizer | Claude Sonnet | Judgment — ranks & makes the call        |


Model IDs live in `model_config.json`, not hardcoded in `server.py`. A background watcher (`tools/model_watcher.py`) updates that file automatically if a model gets pulled from OpenRouter with no working endpoints — this was added after a 2026-07-17 incident where a deprecated model silently produced empty reports.

## Setup

1. Clone the repo
  ```
  git clone https://github.com/Baharghadimii/market-research-agent-swarm.git
  cd market-research-agent-swarm

  ```
2. Create virtual environment
  ```
  python3 -m venv venv
  source venv/bin/activate

  ```
3. Install dependencies
  ```
  pip install -r requirements.txt

  ```
4. Add your API keys
  ```
  cp .env.example .env
  # Edit .env with your keys

  ```
5. Run
  ```
  python3 server.py

  ```

## Environment variables

```
OPENROUTER_API_KEY=     # from openrouter.ai/keys
SERPER_API_KEY=         # from serper.dev

```

## Usage

POST a prompt to `/research`:

```
curl -X POST http://localhost:5000/research \
  -H "Content-Type: application/json" \
  -d '{"prompt": "AI agent tools for founders"}'

```

Returns a structured JSON report: recommended focus, ranked opportunities (each with signal convergence, wedge, risk, and a validation plan), a watch list, contradictions found across sources, and full methodology.