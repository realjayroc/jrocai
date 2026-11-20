"""The primary task-to-agent workflow."""

from __future__ import annotations

import uuid
from typing import Any

from agents.factory import create_agent
from state_store import StateStore


def run(
    payload: dict[str, Any],
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("workflow input must be an object")
    task = payload.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("workflow input requires a non-empty 'task' string")
    agent_name = payload.get("agent", "general")
    if not isinstance(agent_name, str):
        raise ValueError("'agent' must be a string")
    system_prompt = payload.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError("'system_prompt' must be a string")
    provider = payload.get("provider")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("'provider' must be a string")

    agent = create_agent(agent_name)
    store = state_store or StateStore()
    run_id = str(uuid.uuid4())
    store.start_run(run_id, "main_workflow", payload)
    try:
        output = agent.run(task, system_prompt=system_prompt, provider=provider)
    except Exception as error:
        store.fail_run(run_id, f"{type(error).__name__}: {error}")
        raise
    store.finish_run(run_id, output)
    return {"run_id": run_id, "workflow": "main_workflow", "agent": agent.name, "output": output}
