# Market Research Agent Swarm

A planner-executor AI system that finds market gaps for any product, service, or business idea — dynamically deciding where to look based on what the thing actually is, with no hardcoded source list.

## How it works

1. **Planner (Sonnet)** reads your prompt and designs the entire research strategy from scratch — what kind of thing is this, and where would real users of it actually complain or wish out loud? Produces 3-6 highly specific research briefs (e.g. "r/speechdelays subreddit," not just "Reddit"). No hardcoded platforms or categories — a tech product and a local coffee shop get completely different sources.
2. **Researchers** read real posts and comments through public APIs: Hacker News, Stack Exchange, GitHub issues, App Store reviews, Discourse forums and YouTube. No LLM is involved. Google (Serper) is used only for leads and competitor names, never as evidence.
3. **Extractor (Flash)** copies out exact quotes. Code rejects any quote that doesn't appear in the fetched text, and filters out people promoting their own products.
4. **Synthesizer (Sonnet)** ranks up to 3 opportunities and must cite verified quotes. Code then computes how many different people and platforms support each one, so a model can't inflate the numbers.
5. **Existing solutions check** looks for products already solving each problem, using Google results and labelled as such.

Reddit, Facebook and Discord can't be read, and every report says so. Each report lists Reddit threads worth reading, found through Google. Whole Reddit or Facebook pages (Cmd+A, Cmd+C) can be pasted in; code strips the page clutter and detects comment authors before the cheapest model reads them. Pasted comments go through the same quote checks but are labelled as pasted, because their origin can't be confirmed, and they cannot raise an idea's ranking. YouTube needs a free `YOUTUBE_API_KEY`; `GITHUB_TOKEN` is optional and raises GitHub's rate limit.

## Architecture

```
server.py                      Flask backend, full pipeline lives here
├── run_planner()               Sonnet designs the research strategy
├── run_researchers_parallel()  parallel web search execution
├── summarize_all()             Flash compresses raw results
├── run_synthesizer()           Sonnet makes the final call
tools/
├── evidence.py                 public-API connectors, quote checks, people counts
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