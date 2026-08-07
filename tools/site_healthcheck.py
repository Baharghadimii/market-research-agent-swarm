"""
site_healthcheck.py — Direct functional test of the Swarm Research API.

Run this on a schedule (daily cron recommended) from the repo root:

    cd /root/market-research-agent-swarm && venv/bin/python tools/site_healthcheck.py

Unlike model_watcher.py (which checks whether individual OpenRouter models
are still available), this tests the actual live product end-to-end by
hitting the real /research endpoint with a test prompt and checking whether
a real report comes back — the same thing a human clicking "Run Research"
in the browser would notice failing.

On failure, sends a real email alert via Gmail SMTP (not just a draft),
so it doesn't depend on Cowork, mobile push notifications (not yet
available on the Pro plan as of 2026-07), or the desktop app being open.
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, ".env"))

from tools.email_alert import send_alert as _send_alert

API_URL     = "http://127.0.0.1:5000/research"
TEST_PROMPT = "Automated health check: gaps in baby sleep products for the Canadian market"
TIMEOUT_SEC = 240  # full pipeline (planner -> research -> summarize -> synthesize) can take a couple minutes

LOG_PATH = os.path.join(REPO_ROOT, "healthcheck.log")


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def send_alert(subject: str, body: str):
    ok, detail = _send_alert(subject, body)
    log(f"Alert email {'sent successfully' if ok else 'FAILED: ' + detail}")


def check() -> tuple:
    """Hits the real /research endpoint and judges whether the response is
    a genuine, useful report. Returns (healthy: bool, detail: str)."""
    start = time.time()
    try:
        resp = requests.post(API_URL, json={"prompt": TEST_PROMPT}, timeout=TIMEOUT_SEC)
    except requests.exceptions.Timeout:
        return False, f"Request timed out after {TIMEOUT_SEC}s — server may be hung or stuck"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection failed — server may be down or not listening on port 5000: {e}"
    except Exception as e:
        return False, f"Unexpected request error: {type(e).__name__}: {e}"

    elapsed = round(time.time() - start, 1)

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} after {elapsed}s: {resp.text[:500]}"

    try:
        data = resp.json()
    except Exception as e:
        return False, f"Response was not valid JSON after {elapsed}s: {e} — body: {resp.text[:500]}"

    if "error" in data:
        return False, f"API returned an error after {elapsed}s: {data['error']}"

    the_call = data.get("the_call", {})
    focus = (the_call.get("recommended_focus") or "").lower()
    if not focus or "cannot make a call" in focus or "zero source data" in focus:
        return False, f"Report came back but with no real signal after {elapsed}s: {the_call}"

    return True, (f"Healthy — report generated in {elapsed}s, "
                  f"{data.get('sources_used', '?')} sources used, cost ${data.get('cost_usd', '?')}")


def main():
    log("=" * 60)
    log("site_healthcheck run starting")
    healthy, detail = check()

    if healthy:
        log(f"OK: {detail}")
    else:
        log(f"BROKEN: {detail}")
        send_alert(
            subject="Swarm Research is broken",
            body=(
                f"The daily health check for Swarm Research "
                f"(http://209.250.232.171:5000/) failed.\n\n"
                f"Detail:\n{detail}\n\n"
                f"Check on the VPS:\n"
                f"  pm2 logs swarm-research\n"
                f"  cat server.log\n\n"
                f"— automated health check, tools/site_healthcheck.py"
            ),
        )

    log("site_healthcheck run complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
