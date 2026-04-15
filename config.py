import os
from crewai import LLM
from dotenv import load_dotenv

load_dotenv()

# ----------------------------
# Model definitions via OpenRouter
# Change model strings here — affects all agents
# Pricing: https://openrouter.ai/models
# ----------------------------

BUDGET_MODEL = LLM(
    model="openrouter/google/gemini-flash-1-5",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

SMART_CHEAP_MODEL = LLM(
    model="openrouter/deepseek/deepseek-chat",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

INSIGHT_MODEL = LLM(
    model="openrouter/anthropic/claude-sonnet-4-6",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1"
)

# ----------------------------
# Skill loader
# ----------------------------
def load_skill(skill_name: str) -> str:
    """Load a skill markdown file from the skills/ directory."""
    skill_path = os.path.join(os.path.dirname(__file__), "skills", f"{skill_name}.md")
    try:
        with open(skill_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        print(f"⚠️  Skill file not found: {skill_path}")
        return ""
