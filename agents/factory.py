"""Factory and catalog for the built-in agents."""

from __future__ import annotations

from agents.general_agent import GeneralAgent


AGENT_INSTRUCTIONS = {
    "general": "You are a helpful, accurate assistant. Complete the user's task directly.",
    "planner": (
        "You are a practical planning assistant. Turn the requested task into a "
        "clear, ordered plan with concrete actions and completion criteria."
    ),
}


def list_agents() -> list[dict[str, str]]:
    return [
        {"name": name, "description": instructions}
        for name, instructions in AGENT_INSTRUCTIONS.items()
    ]


def create_agent(name: str = "general") -> GeneralAgent:
    normalized = name.strip().lower() if isinstance(name, str) else ""
    try:
        instructions = AGENT_INSTRUCTIONS[normalized]
    except KeyError as error:
        raise ValueError(
            f"Unknown agent {name!r}; available agents: {', '.join(AGENT_INSTRUCTIONS)}"
        ) from error
    return GeneralAgent(normalized, instructions)
