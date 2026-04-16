import os
from crewai import LLM
from dotenv import load_dotenv

load_dotenv()

# Must be set before CrewAI LLM initialization
os.environ['OPENROUTER_API_KEY'] = os.environ.get('OPENROUTER_API_KEY', '')

BUDGET_MODEL = LLM(
    model="openrouter/google/gemini-2.0-flash-lite-001",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1"
)

SMART_CHEAP_MODEL = LLM(
    model="openrouter/deepseek/deepseek-chat",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1"
)

INSIGHT_MODEL = LLM(
    model="openrouter/anthropic/claude-sonnet-4-5",
    api_key=os.environ.get('OPENROUTER_API_KEY'),
    base_url="https://openrouter.ai/api/v1"
)

def load_skill(skill_name: str) -> str:
    skill_path = os.path.join(os.path.dirname(__file__), "skills", f"{skill_name}.md")
    try:
        with open(skill_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        print(f"⚠️  Skill file not found: {skill_path}")
        return ""