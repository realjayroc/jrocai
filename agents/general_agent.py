"""An agent that answers a task using the configured model router."""

from __future__ import annotations

from omni_router import generate_response


class GeneralAgent:
    def __init__(self, name: str, instructions: str):
        self.name = name
        self.instructions = instructions

    def run(
        self,
        task: str,
        system_prompt: str | None = None,
        provider: str | None = None,
    ) -> str:
        instructions = system_prompt or self.instructions
        return generate_response(task, system_prompt=instructions, provider=provider)
