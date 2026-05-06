import os
from crewai import LLM
from dotenv import load_dotenv
load_dotenv()

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