"""Stable programmatic and command-line interface for jrocai."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agents.factory import list_agents
from env_config import load_local_env
from omni_router import configured_providers
from state_store import StateStore
from workflows.main_workflow import run as run_main_workflow


def get_manifest() -> dict[str, Any]:
    return {
        "name": "jrocai",
        "contract_version": 1,
        "version": 1,
        "description": "A local AI hub for agent-backed workflows.",
        "workflows": ["main_workflow"],
        "workflow_inputs": {
            "main_workflow": {
                "task": "string (required)",
                "agent": "string (optional; general or planner)",
                "system_prompt": "string (optional)",
                "provider": "string (optional; preferred provider before fallback)",
            },
        },
        "agents": list_agents(),
        "providers": configured_providers(),
    }


def dispatch_workflow(
    workflow: str,
    payload: dict[str, Any],
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    if workflow not in get_manifest()["workflows"]:
        raise ValueError(f"Unknown workflow {workflow!r}; available workflows: main_workflow")
    if not isinstance(payload, dict):
        raise ValueError("workflow payload must be a JSON object")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("workflow payload must contain only JSON-compatible values") from error
    return run_main_workflow(payload, state_store=state_store)


def _interactive() -> int:
    print("jrocai ready. Enter a task, 'manifest', or 'exit'.")
    while True:
        try:
            task = input("jrocai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if task.lower() in {"exit", "quit"}:
            return 0
        if task.lower() == "manifest":
            print(json.dumps(get_manifest(), indent=2))
            continue
        if not task:
            continue
        try:
            result = dispatch_workflow("main_workflow", {"task": task})
            print(result["output"])
        except Exception as error:
            print(f"Workflow failed: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = argparse.ArgumentParser(description="Run jrocai workflows")
    parser.add_argument("--manifest", action="store_true", help="print the runtime manifest")
    parser.add_argument("--workflow", help="workflow name")
    parser.add_argument("--input", help="JSON workflow input")
    arguments = parser.parse_args(argv)
    if arguments.manifest:
        print(json.dumps(get_manifest(), indent=2))
        return 0
    if arguments.workflow:
        if not arguments.input:
            parser.error("--workflow requires --input")
        try:
            result = dispatch_workflow(arguments.workflow, json.loads(arguments.input))
        except (ValueError, json.JSONDecodeError, RuntimeError) as error:
            print(f"Workflow failed: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if arguments.input:
        parser.error("--input requires --workflow")
    return _interactive()


if __name__ == "__main__":
    raise SystemExit(main())
