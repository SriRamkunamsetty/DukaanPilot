"""The ADK agent definition. ADK discovers ``root_agent`` in this module."""

from __future__ import annotations

from google.adk.agents import Agent
from google.genai import types

from .config import Settings
from .prompts import BRIEFING_INSTRUCTION, build_instruction
from .tools import ALL_TOOLS

_settings = Settings.from_env()

root_agent = Agent(
    name="dukaanpilot",
    model=_settings.model,
    description=(
        "Proactive operations assistant for small shops: sales, stock, forecasts, "
        "and human-approved supplier reorders, in English, Hindi and Telugu."
    ),
    instruction=build_instruction,
    tools=list(ALL_TOOLS),
    generate_content_config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=1024),
)

# A tool-less agent that only phrases facts the code already computed.
briefing_agent = Agent(
    name="briefing_writer",
    model=_settings.model,
    description="Writes the owner's morning briefing from verified facts.",
    instruction=BRIEFING_INSTRUCTION,
    tools=[],
    generate_content_config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=512),
)
