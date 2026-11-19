from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from business_command import BusinessCommand
from ecosystem_hub import EcosystemHub, _validate_manifest, create_api_server
from env_config import load_local_env
from license_engine import LicenseEngine, OrderFulfillmentService


TEST_LICENSE_SECRET = bytes(range(32))
TEST_WEBHOOK_SECRET = bytes(range(32, 64))


class BusinessCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "jroc_core.db")
        self.business = BusinessCommand(self.db_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_persists_catalog_inventory_repair_assets_and_dynamic_price(self):
        product = self.business.create_product(
            "APP-1", "Desktop App", "digital_app", 1000, stock_quantity=3,
            min_price_cents=700, max_price_cents=1500,
        )
        self.assertEqual(product["current_price_cents"], 1000)
        quote = self.business.calculate_price("APP-1", demand_score=100)
        self.assertEqual(quote["calculated_price_cents"], 1350)
        updated = self.business.apply_price("APP-1", demand_score=100)
        self.assertEqual(updated["current_price_cents"], 1350)
        self.business.update_inventory("APP-1", 2)
        sale = self.business.record_sale("ORDER-1", "APP-1", 1)
        self.assertEqual(sale["total_cents"], 1350)

        ticket = self.business.create_repair_ticket(
            "customer-7", "Laptop", "Replace a damaged display", estimate_cents=8900
        )
        ticket = self.business.update_repair_ticket(ticket["ticket_id"], "in_progress")
        self.assertEqual(ticket["status"], "in_progress")
        asset = self.business.add_drama_asset(
            "Series A", "script", "Episode 1", "assets/episode-1.txt",
            metadata={"season": 1, "episode": 1},
        )

        reopened = BusinessCommand(self.db_path)
        self.assertEqual(reopened.get_product("APP-1")["stock_quantity"], 4)
        self.assertEqual(reopened.list_sales()[0]["order_id"], "ORDER-1")
        self.assertEqual(reopened.list_repair_tickets()[0]["ticket_id"], ticket["ticket_id"])
        self.assertEqual(reopened.list_drama_assets()[0]["asset_id"], asset["asset_id"])
        self.assertEqual(reopened.list_pricing_history("APP-1")[0]["new_price_cents"], 1350)

    def test_rejects_bad_inputs_and_invalid_ticket_transitions(self):
        with self.assertRaisesRegex(ValueError, "category"):
            self.business.create_product("BAD", "Bad", [], 100)
        self.business.create_product("APP", "App", "digital_app", 100)
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            self.business.add_drama_asset("Series", "script", "Episode", "file", {"bad": object()})
        ticket = self.business.create_repair_ticket("customer", "device", "issue")
        ticket = self.business.update_repair_ticket(ticket["ticket_id"], "in_progress")
        ticket = self.business.update_repair_ticket(ticket["ticket_id"], "completed")
        with self.assertRaisesRegex(ValueError, "transition"):
            self.business.update_repair_ticket(ticket["ticket_id"], "in_progress")

    def test_local_env_loader_preserves_process_values(self):
        env_file = Path(self.directory.name) / ".env"
        env_file.write_text("BUSINESS_TEST_VALUE=from-file\n", encoding="utf-8")
        with patch.dict(os.environ, {"BUSINESS_TEST_VALUE": "from-process"}, clear=False):
            self.assertTrue(load_local_env(env_file))
            self.assertEqual(os.environ["BUSINESS_TEST_VALUE"], "from-process")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BUSINESS_TEST_VALUE", None)
            load_local_env(env_file)
            self.assertEqual(os.environ["BUSINESS_TEST_VALUE"], "from-file")


class LicenseAndFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.business = BusinessCommand(os.path.join(self.directory.name, "jroc_core.db"))
        self.business.create_product(
            "APP", "Licensed App", "digital_app", 2500, currency="USD",
            activation_limit=1, license_days=30,
        )
        self.engine = LicenseEngine(
            business=self.business, secret=TEST_LICENSE_SECRET
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_offline_validation_activation_limit_and_revocation(self):
        key = self.engine.generate_license("APP")
        claims = self.engine.validate_license(key)
        self.assertEqual(claims["validation"], "offline")
        self.engine.activate_license(key, "machine-one")
        self.engine.activate_license(key, "machine-one")
        with self.assertRaisesRegex(ValueError, "activation limit"):
            self.engine.activate_license(key, "machine-two")
        self.assertEqual(
            self.engine.validate_license(key, online=True, machine_id="machine-one")["sku"],
            "APP",
        )
        key_parts = key.split(".")
        signature = key_parts[2]
        key_parts[2] = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = ".".join(key_parts)
        with self.assertRaisesRegex(ValueError, "signature"):
            self.engine.validate_license(tampered)
        self.engine.revoke_license(key)
        with self.assertRaisesRegex(ValueError, "not active"):
            self.engine.validate_license(key, online=True)
        self.assertTrue(self.engine.validate_license(key)["valid"])

    def test_signed_order_fulfillment_is_atomic_and_replay_safe(self):
        service = OrderFulfillmentService(
            business=self.business,
            license_engine=self.engine,
            webhook_secret=TEST_WEBHOOK_SECRET,
        )
        event = {
            "event_id": "evt-001",
            "order_id": "order-001",
            "customer_id": "customer-1",
            "status": "paid",
            "currency": "USD",
            "amount_cents": 5000,
            "items": [{"sku": "APP", "quantity": 2, "unit_price_cents": 2500}],
        }
        raw_body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        signature = service.signature_for(raw_body, TEST_WEBHOOK_SECRET)

        replay = service.process_signed_event(raw_body, signature)
        result = service.process_signed_event(raw_body, signature)
        self.assertEqual(result, replay)
        self.assertEqual(len(result["licenses"]), 2)
        self.assertEqual(len(self.business.list_sales()), 1)
        self.assertEqual(len(self.business.list_sales()[0]), 9)

        with self.assertRaisesRegex(ValueError, "signature"):
            service.process_signed_event(raw_body, "sha256=" + "0" * 64)
        changed = dict(event, amount_cents=1)
        changed_body = json.dumps(changed, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "different payload"):
            service.process_signed_event(
                changed_body,
                service.signature_for(changed_body, TEST_WEBHOOK_SECRET),
            )

    def test_unpaid_or_mismatched_order_does_not_change_inventory(self):
        service = OrderFulfillmentService(
            business=self.business,
            license_engine=self.engine,
            webhook_secret=TEST_WEBHOOK_SECRET,
        )
        event = {
            "event_id": "evt-fail",
            "order_id": "order-fail",
            "customer_id": "customer-1",
            "status": "paid",
            "currency": "USD",
            "amount_cents": 10,
            "items": [{"sku": "APP", "quantity": 1, "unit_price_cents": 2500}],
        }
        raw_body = json.dumps(event).encode("utf-8")
        signature = service.signature_for(raw_body, TEST_WEBHOOK_SECRET)
        with self.assertRaisesRegex(ValueError, "does not match"):
            service.process_signed_event(raw_body, signature)
        self.assertEqual(self.business.list_sales(), [])
        self.assertEqual(self.business.get_product("APP")["stock_quantity"], None)


class EcosystemHubTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hub = EcosystemHub(db_path=os.path.join(self.directory.name, "hub.db"))

    def tearDown(self):
        self.directory.cleanup()

    def test_contract_manifest_validation_and_local_dispatch(self):
        local = next(repo for repo in self.hub.repositories.values() if repo.local)
        self.assertEqual(local.manifest["workflows"], ["main_workflow"])
        with patch("ecosystem_hub.dispatch_workflow", return_value={"output": "ok"}):
            self.assertEqual(
                self.hub.dispatch(local.repo_id, "main_workflow", {"task": "test"}),
                {"output": "ok"},
            )
            self.assertEqual(
                self.hub.dispatch_agent(local.repo_id, "planner", {"task": "plan"}),
                {"output": "ok"},
            )
        with self.assertRaisesRegex(ValueError, "not advertised"):
            self.hub.dispatch(local.repo_id, "not_real", {})
        with self.assertRaisesRegex(ValueError, "not advertised"):
            self.hub.dispatch_agent(local.repo_id, "unknown", {"task": "plan"})
        with self.assertRaisesRegex(ValueError, "unique"):
            _validate_manifest({
                "contract_version": 1, "name": "invalid", "workflows": ["same", "same"]
            })

    def test_discovers_and_dispatches_configured_sibling_bridge_in_isolated_process(self):
        sibling = Path(self.directory.name) / "sibling_app"
        sibling.mkdir()
        (sibling / "jroc_bridge.py").write_text(
            """import json, sys
n = sys.argv.index("--manifest") if "--manifest" in sys.argv else -1
if n >= 0:
    print(json.dumps({"contract_version": 1, "name": "sibling", "workflows": ["echo"]}))
else:
    workflow = sys.argv[sys.argv.index("--workflow") + 1]
    payload = json.loads(sys.argv[sys.argv.index("--input") + 1])
    print(json.dumps({"workflow": workflow, "payload": payload}))
""",
            encoding="utf-8",
        )
        hub = EcosystemHub(
            db_path=os.path.join(self.directory.name, "sibling-hub.db"),
            repo_paths=[sibling],
            repo_roots=[],
        )
        self.assertIn("sibling_app", hub.repositories)
        result = hub.dispatch("sibling_app", "echo", {"message": "hello"})
        self.assertEqual(result, {"workflow": "echo", "payload": {"message": "hello"}})

    def test_api_is_loopback_only_authenticated_and_validates_body(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_api_server(self.hub, host="0.0.0.0", token="t" * 32)
        server, token = create_api_server(self.hub, port=0, token="t" * 32)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(base_url + "/api/v1/business/products")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 401)

            request = urllib.request.Request(
                base_url + "/api/v1/business/products",
                data=b"{}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 400)

            request = urllib.request.Request(
                base_url + "/api/v1/workflows",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.loads(response.read())
            self.assertEqual(data["repositories"][0]["contract_version"], 1)

            body = json.dumps({
                "sku": "HTTP-APP",
                "name": "API Product",
                "category": "digital_app",
                "price_cents": 1000,
                "min_price_cents": 800,
                "max_price_cents": 1200,
            }).encode("utf-8")
            request = urllib.request.Request(
                base_url + "/api/v1/business/products",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                created = json.loads(response.read())
            self.assertEqual(created["sku"], "HTTP-APP")

            agent_body = json.dumps({
                "repo": next(repo.repo_id for repo in self.hub.repositories.values() if repo.local),
                "agent": "planner",
                "payload": {"task": "plan"},
            }).encode("utf-8")
            request = urllib.request.Request(
                base_url + "/api/v1/agents/dispatch",
                data=agent_body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with patch("ecosystem_hub.dispatch_workflow", return_value={"output": "plan"}):
                with urllib.request.urlopen(request, timeout=3) as response:
                    agent_result = json.loads(response.read())
            self.assertEqual(agent_result, {"output": "plan"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
