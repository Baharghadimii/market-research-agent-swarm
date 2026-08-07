"""
model_watcher.py — Autonomous OpenRouter model health-check + self-repair.

Run this on a schedule (weekly cron recommended) from the repo root:

    cd /root/market-research-agent-swarm && venv/bin/python tools/model_watcher.py

What it does, in order:
  1. Loads model_config.json (the source of truth server.py reads at startup).
  2. For each role (sonnet, flash), tests every model in that role's list
     with a tiny real completion call — the same kind of call that failed
     silently on 2026-07-17 when google/gemini-2.0-flash-lite-001 was
     pulled from OpenRouter.
  3. If at least one model in the chain still works: prunes any dead ones,
     promotes the first working model to the front, and moves on. No
     human approval needed — this can't make things worse, since a dead
     model was never going to serve traffic anyway.
  4. If EVERY model in a role's chain is dead: fetches OpenRouter's live
     model catalog, filters to the same model family (e.g. google/gemini
     *flash*lite* for the "flash" role), enforces a price ceiling
     (price_ceiling_multiplier x baseline_prices from model_config.json —
     currently 2x), test-calls each candidate in price order, and adopts
     the first one that actually responds.
  5. Writes the updated model_config.json, commits + pushes it to GitHub,
     and restarts the PM2-managed server so the fix goes live immediately.
  6. Everything is logged to watcher.log (append-only) as the audit trail,
     since there is no approval gate. If step 4 finds NOTHING usable
     within the price ceiling, it writes a loud ALERT block to the log
     and creates a WATCHER_NEEDS_ATTENTION.txt marker file in the repo
     root — grep for "ALERT" in watcher.log or check for that file to
     see if the watcher is stuck.

This script deliberately never DELETES a role down to zero models — if
nothing new can be found, whatever was still working (even if none were)
is left in place and the alert marker is written instead of leaving the
config empty.
"""

import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone

import requests
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.email_alert import send_alert

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "model_config.json")
LOG_PATH    = os.path.join(REPO_ROOT, "watcher.log")
ALERT_PATH  = os.path.join(REPO_ROOT, "WATCHER_NEEDS_ATTENTION.txt")

sys.path.insert(0, REPO_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, ".env"))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")

# Family filters used to find replacement candidates. Deliberately narrow —
# we only ever want to swap a flash-lite model for another flash-lite
# model, never something wildly different in capability or cost tier.
FAMILY_FILTERS = {
    "flash": lambda mid: "google/gemini" in mid and "flash" in mid and "lite" in mid
                          and "preview" not in mid and "image" not in mid,
    "sonnet": lambda mid: "anthropic/claude" in mid and "sonnet" in mid
                           and "preview" not in mid,
}


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def test_model(model_id: str) -> tuple:
    """Real minimal completion call. Returns (ok: bool, error: str|None)."""
    try:
        client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def fetch_catalog() -> list:
    resp = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"} if OPENROUTER_API_KEY else {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def model_price_per_1m(model_entry: dict) -> dict:
    pricing = model_entry.get("pricing", {})
    try:
        return {
            "input":  float(pricing.get("prompt", "0")) * 1_000_000,
            "output": float(pricing.get("completion", "0")) * 1_000_000,
        }
    except (TypeError, ValueError):
        return {"input": float("inf"), "output": float("inf")}


def discover_candidates(role: str, exclude_ids: set, config: dict) -> list:
    """Return [(model_id, price_dict), ...] sorted cheapest-output-first,
    filtered to the role's family and within the price ceiling."""
    filt = FAMILY_FILTERS[role]
    baseline = config["baseline_prices"][role]
    ceiling_mult = config.get("price_ceiling_multiplier", 2.0)
    ceiling = {"input": baseline["input"] * ceiling_mult, "output": baseline["output"] * ceiling_mult}

    catalog = fetch_catalog()
    candidates = []
    for entry in catalog:
        mid = entry.get("id", "")
        if not mid or mid in exclude_ids or not filt(mid):
            continue
        price = model_price_per_1m(entry)
        if price["input"] <= ceiling["input"] and price["output"] <= ceiling["output"]:
            candidates.append((mid, price))

    candidates.sort(key=lambda c: c[1]["output"])
    log(f"  discovery for '{role}': {len(candidates)} candidates within "
        f"ceiling (input<=${ceiling['input']:.2f}, output<=${ceiling['output']:.2f})")
    return candidates


def git_commit_and_push(message: str) -> bool:
    try:
        subprocess.run(["git", "add", "model_config.json"], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=REPO_ROOT, check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  git commit/push FAILED: {e}")
        return False


def restart_server() -> bool:
    try:
        subprocess.run(["pm2", "restart", "swarm-research"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"  pm2 restart FAILED: {e}")
        return False


def check_role(role: str, config: dict) -> bool:
    """Returns True if config was changed for this role."""
    key = f"{role}_models"
    models = config[key]
    changed = False

    working = []
    for m in models:
        ok, err = test_model(m["id"])
        if ok:
            working.append(m)
            log(f"  [{role}] {m['id']} — OK")
        else:
            log(f"  [{role}] {m['id']} — DEAD ({err})")
            changed = True

    if working:
        if changed:
            config[key] = working
            config["watcher"]["history"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "action": "pruned dead model(s), kept working ones",
                "remaining": [m["id"] for m in working],
            })
            log(f"  [{role}] chain still has {len(working)} working model(s) — pruned dead ones, no discovery needed")
        return changed

    # Every model in the chain is dead — need to discover a replacement.
    log(f"  [{role}] ALL models dead — starting discovery")
    exclude_ids = {m["id"] for m in models}
    try:
        candidates = discover_candidates(role, exclude_ids, config)
    except Exception as e:
        log(f"  [{role}] ALERT: catalog discovery failed: {type(e).__name__}: {e}")
        candidates = []

    for mid, price in candidates:
        ok, err = test_model(mid)
        log(f"  [{role}] testing candidate {mid} (${price['input']:.2f}/${price['output']:.2f} per 1M) — "
            f"{'OK' if ok else 'FAILED: ' + str(err)}")
        if ok:
            config[key] = [{"id": mid, "input": round(price["input"], 4), "output": round(price["output"], 4)}]
            config["watcher"]["history"].append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "action": f"auto-replaced dead chain with {mid}",
                "dead_models": list(exclude_ids),
                "price": price,
            })
            log(f"  [{role}] ADOPTED {mid} as new primary model")
            return True

    # Nothing worked. Do not wipe the config — leave dead entries in place
    # (server.py's own fallback logic will still error clearly) and raise
    # a loud, impossible-to-miss alert: log line, marker file, AND email,
    # since this is the one case where autonomous self-repair genuinely
    # can't proceed and a human needs to step in.
    alert = (f"ALERT: role '{role}' has ZERO working models and discovery found "
              f"no usable replacement within the price ceiling. Manual intervention needed.")
    log(f"  {alert}")
    with open(ALERT_PATH, "a") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {alert}\n")

    dead_list = "\n".join(f"  - {m['id']}" for m in models)
    ok, detail = send_alert(
        subject=f"Swarm Research: '{role}' models are ALL dead, watcher couldn't self-heal",
        body=(
            f"model_watcher.py could not find any working replacement for the '{role}' "
            f"role within the price ceiling ({config.get('price_ceiling_multiplier', 2.0)}x "
            f"baseline).\n\nAll models in the current chain failed:\n{dead_list}\n\n"
            f"This needs manual attention — either raise the price ceiling in "
            f"model_config.json, or manually pick a replacement model on OpenRouter "
            f"and add it to the '{role}_models' list.\n\n"
            f"— tools/model_watcher.py, running on the VPS"
        ),
    )
    log(f"  alert email {'sent' if ok else 'FAILED: ' + detail}")
    return False


def main():
    log("=" * 60)
    log("model_watcher run starting")
    config = load_config()
    config.setdefault("watcher", {"last_checked_utc": None, "last_action": None, "history": []})

    any_change = False
    for role in ("sonnet", "flash"):
        log(f"Checking role: {role}")
        if check_role(role, config):
            any_change = True

    config["watcher"]["last_checked_utc"] = datetime.now(timezone.utc).isoformat()

    if any_change:
        config["watcher"]["last_action"] = "config updated — see history"
        save_config(config)
        msg = f"watcher: auto-update model_config.json ({datetime.now(timezone.utc).date()})"
        if git_commit_and_push(msg):
            log("  pushed model_config.json to GitHub")
            if restart_server():
                log("  restarted swarm-research via pm2")
            else:
                log("  ALERT: config was updated but pm2 restart failed — server may still be running old models")
        else:
            log("  ALERT: config was updated locally but git push failed — change is NOT live")
    else:
        config["watcher"]["last_action"] = "no changes needed — all models healthy"
        save_config(config)
        log("No changes needed — all configured models are healthy")

    log("model_watcher run complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
