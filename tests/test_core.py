from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import omni_router
from jroc_bridge import dispatch_workflow, get_manifest
from state_store import StateStore


class RouterTests(unittest.TestCase):
    def test_rate_limit_fails_over_to_gemini(self):
        gemini_response = {
            "candidates": [{
                "content": {"parts": [{"text": "fallback answer"}]},
            }],
        }
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-test",
            "GOOGLE_API_KEY": "gemini-test",
            "JROCAI_PROVIDERS": "openrouter,gemini",
        }, clear=True), patch.object(
            omni_router,
            "_request_json",
            side_effect=[
                omni_router.ProviderError("openrouter", "HTTP 429", status=429),
                gemini_response,
            ],
        ) as request:
            result = omni_router.generate_response("hello")

        self.assertEqual(result, "fallback answer")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["openrouter", "gemini"])

    def test_manifest_reports_configured_state_without_credentials(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "secret-value"}, clear=True):
            manifest = get_manifest()
        providers = {provider["name"]: provider for provider in manifest["providers"]}
        self.assertTrue(providers["groq"]["configured"])
        self.assertNotIn("secret-value", str(manifest))
        self.assertEqual(manifest["contract_version"], 1)
        self.assertTrue(manifest["name"])
        self.assertEqual(manifest["workflows"], ["main_workflow"])
        self.assertEqual(len(manifest["workflows"]), len(set(manifest["workflows"])))
        self.assertEqual(manifest["workflow_inputs"]["main_workflow"]["task"], "string (required)")


class WorkflowTests(unittest.TestCase):
    def test_dispatch_persists_successful_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.sqlite3"))
            with patch("agents.general_agent.generate_response", return_value="done"):
                result = dispatch_workflow("main_workflow", {"task": "do work"}, state_store=store)
            record = store.get_run(result["run_id"])

        self.assertEqual(result["output"], "done")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["input"], {"task": "do work"})
        self.assertEqual(record["output"], "done")
        json.dumps(result)

    def test_failed_execution_is_persisted_and_propagated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.sqlite3"))
            with patch("agents.general_agent.generate_response", side_effect=RuntimeError("provider down")):
                with self.assertRaisesRegex(RuntimeError, "provider down"):
                    dispatch_workflow("main_workflow", {"task": "do work"}, state_store=store)
            with store._connection() as connection:
                run_id = connection.execute("SELECT run_id FROM workflow_runs").fetchone()["run_id"]
            record = store.get_run(run_id)

        self.assertEqual(record["status"], "failed")
        self.assertIn("provider down", record["error"])

    def test_invalid_workflow_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "task"):
            dispatch_workflow("main_workflow", {})
        with self.assertRaisesRegex(ValueError, "Unknown workflow"):
            dispatch_workflow("missing", {"task": "do work"})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            dispatch_workflow("main_workflow", ["not", "an", "object"])
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            dispatch_workflow("main_workflow", {"task": "do work", "other": object()})


if __name__ == "__main__":
    unittest.main()
