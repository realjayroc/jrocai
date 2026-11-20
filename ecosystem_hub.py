"""Discover bridge-v1 repositories and expose a loopback-only workflow/business API."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from business_command import BusinessCommand
from env_config import load_local_env
from jroc_bridge import dispatch_workflow, get_manifest
from license_engine import LicenseEngine, OrderFulfillmentService


MAX_REPOSITORIES = 64
MAX_MANIFEST_BYTES = 128 * 1024
MAX_DISPATCH_INPUT_BYTES = 16 * 1024
MAX_BRIDGE_OUTPUT_BYTES = 128 * 1024
MAX_API_BODY_BYTES = 64 * 1024
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
BRIDGE_TIMEOUT_SECONDS = 45
MANIFEST_TIMEOUT_SECONDS = 5
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request must contain valid UTF-8 JSON") from error


def _validate_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"{label} must contain only JSON-compatible values") from error
    if len(encoded.encode("utf-8")) > MAX_DISPATCH_INPUT_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_DISPATCH_INPUT_BYTES}-byte limit")
    return value


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("bridge manifest must be a JSON object")
    if value.get("contract_version") != 1 or isinstance(value.get("contract_version"), bool):
        raise ValueError("bridge manifest must declare contract_version 1")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
        raise ValueError("bridge manifest must declare a non-empty name")
    workflows = value.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise ValueError("bridge manifest workflows must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in workflows):
        raise ValueError("bridge workflow names must be non-empty strings")
    if len(workflows) != len(set(workflows)):
        raise ValueError("bridge workflow names must be unique")
    raw_agents = value.get("agents", [])
    if not isinstance(raw_agents, list):
        raise ValueError("bridge manifest agents must be a list when provided")
    agents = []
    for item in raw_agents:
        agent = item.get("name") if isinstance(item, dict) else item
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 128:
            raise ValueError("bridge agent names must be non-empty strings")
        agents.append(agent)
    if len(agents) != len(set(agents)):
        raise ValueError("bridge agent names must be unique")
    description = value.get("description", "")
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError("bridge description must be a string of at most 1000 characters")
    return {
        "contract_version": 1,
        "name": name.strip(),
        "description": description,
        "workflows": list(workflows),
        "agents": agents,
    }


def _split_path_setting(value: str | None) -> list[Path]:
    if not value or not value.strip():
        return []
    cleaned = value.strip()
    if cleaned.startswith("["):
        parsed = _parse_json(cleaned.encode("utf-8"))
        if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
            raise ValueError("repository path JSON settings must be arrays of strings")
        return [Path(item.strip().strip('"')) for item in parsed if item.strip()]
    parts = [part.strip().strip('"') for part in cleaned.split(os.pathsep)]
    return [Path(part) for part in parts if part]


def _candidate_repositories(
    repo_paths: list[Path] | None = None,
    repo_roots: list[Path] | None = None,
) -> list[Path]:
    own_root = Path(__file__).resolve().parent
    candidates = [own_root]
    direct_paths = repo_paths if repo_paths is not None else _split_path_setting(
        os.environ.get("JROC_REPO_PATHS")
    )
    roots = repo_roots if repo_roots is not None else _split_path_setting(
        os.environ.get("JROC_REPO_ROOTS")
    )
    candidates.extend(direct_paths)
    for root in roots:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"configured repository root is not a directory: {resolved_root}")
        if (resolved_root / "jroc_bridge.py").is_file():
            candidates.append(resolved_root)
        else:
            try:
                children = sorted(
                    (path for path in resolved_root.iterdir() if path.is_dir()),
                    key=lambda path: path.name.casefold(),
                )
            except OSError as error:
                raise ValueError(f"cannot scan configured repository root {resolved_root}: {error}") from error
            candidates.extend(path for path in children if (path / "jroc_bridge.py").is_file())
            if len(candidates) > MAX_REPOSITORIES:
                raise ValueError(f"repository discovery exceeds the {MAX_REPOSITORIES}-repository limit")
    resolved = []
    seen = set()
    for path in candidates:
        try:
            candidate = path.expanduser().resolve(strict=True)
        except OSError:
            candidate = path.expanduser().resolve()
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            resolved.append(candidate)
    if len(resolved) > MAX_REPOSITORIES:
        raise ValueError(f"repository discovery exceeds the {MAX_REPOSITORIES}-repository limit")
    return resolved


@dataclass(frozen=True)
class RepositoryBridge:
    repo_id: str
    path: Path
    manifest: dict[str, Any]
    local: bool

    def public_data(self) -> dict[str, Any]:
        return {
            "id": self.repo_id,
            "path": str(self.path),
            **self.manifest,
        }


def _subprocess_environment() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    return {name: os.environ[name] for name in keep if name in os.environ}


def _forwarded_environment() -> dict[str, str]:
    names = [name.strip() for name in os.environ.get("JROC_HUB_FORWARD_ENV", "").split(",")]
    forwarded = {}
    for name in names:
        if not name:
            continue
        if not _ENV_NAME.fullmatch(name) or name == "JROCAI_HUB_API_TOKEN":
            raise ValueError(f"invalid or prohibited environment forwarding name: {name}")
        if name in os.environ:
            forwarded[name] = os.environ[name]
    return forwarded


def _run_isolated_bridge(
    repository: Path,
    arguments: list[str],
    timeout: int,
    output_limit: int,
) -> bytes:
    script = repository / "jroc_bridge.py"
    if not script.is_file():
        raise ValueError(f"repository has no jroc_bridge.py: {repository}")
    environment = _subprocess_environment()
    if "--workflow" in arguments:
        environment.update(_forwarded_environment())
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(script), *arguments],
        cwd=str(repository),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    oversized = threading.Event()

    def read_output():
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                return
            if len(output) + len(chunk) > output_limit:
                oversized.set()
                process.kill()
                return
            output.extend(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        reader.join(timeout=1)
        process.stdout.close()
        raise RuntimeError(f"bridge command timed out after {timeout} seconds") from error
    reader.join(timeout=2)
    try:
        if reader.is_alive():
            process.kill()
            reader.join(timeout=1)
            raise RuntimeError("bridge command did not close its output stream")
    finally:
        process.stdout.close()
    if oversized.is_set():
        raise RuntimeError(f"bridge output exceeds the {output_limit}-byte limit")
    if return_code != 0:
        raise RuntimeError(f"bridge command failed with exit code {return_code}")
    return bytes(output)


class EcosystemHub:
    def __init__(
        self,
        db_path: str | None = None,
        repo_paths: list[Path] | None = None,
        repo_roots: list[Path] | None = None,
    ):
        self.business = BusinessCommand(db_path)
        self.license_engine: LicenseEngine | None = None
        self.order_fulfillment: OrderFulfillmentService | None = None
        self.repositories: dict[str, RepositoryBridge] = {}
        self.discovery_errors: list[dict[str, str]] = []
        seen_ids = set()
        for path in _candidate_repositories(repo_paths, repo_roots):
            if not (path / "jroc_bridge.py").is_file():
                self.discovery_errors.append({
                    "path": str(path), "error": "repository has no jroc_bridge.py"
                })
                continue
            try:
                if path == Path(__file__).resolve().parent:
                    manifest_data = get_manifest()
                    manifest = _validate_manifest(manifest_data)
                else:
                    raw = _run_isolated_bridge(
                        path, ["--manifest"], MANIFEST_TIMEOUT_SECONDS, MAX_MANIFEST_BYTES
                    )
                    manifest = _validate_manifest(_parse_json(raw))
                repo_id = path.name
                if not _REPO_ID.fullmatch(repo_id):
                    raise ValueError("repository directory name is not a valid repository id")
                if repo_id.casefold() in seen_ids:
                    raise ValueError(f"duplicate repository id {repo_id!r}")
                seen_ids.add(repo_id.casefold())
                self.repositories[repo_id] = RepositoryBridge(
                    repo_id=repo_id,
                    path=path,
                    manifest=manifest,
                    local=path == Path(__file__).resolve().parent,
                )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                self.discovery_errors.append({"path": str(path), "error": str(error)[:500]})

    def list_repositories(self) -> dict[str, Any]:
        return {
            "repositories": [
                repository.public_data() for repository in self.repositories.values()
            ],
            "errors": list(self.discovery_errors),
        }

    def dispatch(self, repo_id: str, workflow: str, payload: dict[str, Any]) -> Any:
        if not isinstance(repo_id, str) or not repo_id:
            raise ValueError("repo must be a non-empty repository id")
        if not isinstance(workflow, str) or not workflow:
            raise ValueError("workflow must be a non-empty workflow name")
        payload = _validate_json_object(payload, "payload")
        repository = self.repositories.get(repo_id)
        if repository is None:
            raise KeyError(f"unknown configured repository: {repo_id}")
        if workflow not in repository.manifest["workflows"]:
            raise ValueError(
                f"workflow {workflow!r} is not advertised by repository {repo_id!r}"
            )
        if repository.local:
            result = dispatch_workflow(workflow, payload)
        else:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            raw = _run_isolated_bridge(
                repository.path,
                ["--workflow", workflow, "--input", encoded],
                BRIDGE_TIMEOUT_SECONDS,
                MAX_BRIDGE_OUTPUT_BYTES,
            )
            result = _parse_json(raw)
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as error:
            raise RuntimeError("workflow returned a non-JSON-compatible result") from error
        return result

    def dispatch_agent(self, repo_id: str, agent: str, payload: dict[str, Any]) -> Any:
        repository = self.repositories.get(repo_id)
        if repository is None:
            raise KeyError(f"unknown configured repository: {repo_id}")
        if not isinstance(agent, str) or agent not in repository.manifest["agents"]:
            raise ValueError(f"agent {agent!r} is not advertised by repository {repo_id!r}")
        if "main_workflow" not in repository.manifest["workflows"]:
            raise ValueError(f"repository {repo_id!r} has no main_workflow agent entry point")
        payload = _validate_json_object(payload, "payload")
        if "agent" in payload:
            raise ValueError("agent must be selected by the agent dispatch route")
        return self.dispatch(
            repo_id, "main_workflow", {**payload, "agent": agent}
        )

    def _license_service(self) -> LicenseEngine:
        if self.license_engine is None:
            self.license_engine = LicenseEngine(
                db_path=str(self.business.db_path), business=self.business
            )
        return self.license_engine

    def _order_service(self) -> OrderFulfillmentService:
        if self.order_fulfillment is None:
            self.order_fulfillment = OrderFulfillmentService(
                business=self.business, license_engine=self._license_service()
            )
        return self.order_fulfillment


def _api_token(value: str | None = None) -> str:
    if value is not None:
        token = value
    else:
        token = os.environ.get("JROCAI_HUB_API_TOKEN") or None
    if token is None:
        return secrets.token_urlsafe(32)
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise ValueError("JROCAI_HUB_API_TOKEN must contain 32 to 512 characters")
    return token


def _expect_fields(
    data: dict[str, Any],
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    missing = required - data.keys()
    extra = data.keys() - required - optional
    if missing or extra:
        raise ValueError(
            f"request fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )


class LoopbackHTTPServer(HTTPServer):
    allow_reuse_address = True


def create_api_server(
    hub: EcosystemHub,
    port: int = 8765,
    token: str | None = None,
    host: str = "127.0.0.1",
) -> tuple[LoopbackHTTPServer, str]:
    if host != "127.0.0.1":
        raise ValueError("the hub API may bind only to 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    api_token = _api_token(token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JrocaiHub/1"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format_string: str, *args):
            sys.stderr.write("jrocai hub: " + format_string % args + "\n")

        def _write(self, status: int, result: Any):
            body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > MAX_API_RESPONSE_BYTES:
                status = 500
                body = b'{"error":"API response exceeds the configured size limit"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorize(self):
            try:
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self._write(403, {"error": "loopback clients only"})
                    return False
            except ValueError:
                self._write(403, {"error": "invalid client address"})
                return False
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied, api_token):
                self._write(401, {"error": "bearer token is missing or invalid"})
                return False
            return True

        def _request_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("transfer-encoded request bodies are not supported")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            length_value = self.headers.get("Content-Length")
            if not length_value or not length_value.isascii() or not length_value.isdigit():
                raise ValueError("Content-Length must be a positive integer")
            length = int(length_value)
            if not 1 <= length <= MAX_API_BODY_BYTES:
                raise ValueError(f"request body must contain 1 to {MAX_API_BODY_BYTES} bytes")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("request body ended before Content-Length bytes were received")
            return body

        def _json_body(self) -> dict[str, Any]:
            value = _parse_json(self._request_body())
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _handle_error(self, error: Exception):
            if isinstance(error, KeyError):
                self._write(404, {"error": str(error)})
            elif isinstance(error, ValueError):
                self._write(400, {"error": str(error)})
            elif isinstance(error, sqlite3.Error):
                self._write(500, {"error": "database operation failed"})
            elif isinstance(error, RuntimeError):
                self._write(502, {"error": str(error)[:1000]})
            else:
                self._write(500, {"error": "internal hub error"})

        def do_GET(self):
            if not self._authorize():
                return
            path = urllib.parse.urlsplit(self.path)
            if path.query:
                self._write(400, {"error": "query parameters are not supported"})
                return
            try:
                if path.path == "/api/v1/workflows":
                    self._write(200, hub.list_repositories())
                elif path.path == "/api/v1/business/products":
                    self._write(200, {"products": hub.business.list_products()})
                elif path.path == "/api/v1/business/sales":
                    self._write(200, {"sales": hub.business.list_sales()})
                elif path.path == "/api/v1/business/revenue":
                    self._write(200, {"revenue": hub.business.revenue_summary()})
                elif path.path == "/api/v1/business/repair-tickets":
                    self._write(200, {"tickets": hub.business.list_repair_tickets()})
                elif path.path == "/api/v1/business/drama-assets":
                    self._write(200, {"assets": hub.business.list_drama_assets()})
                else:
                    self._write(404, {"error": "unknown API route"})
            except (ValueError, KeyError, RuntimeError, sqlite3.Error) as error:
                self._handle_error(error)

        def do_POST(self):
            if not self._authorize():
                return
            path = urllib.parse.urlsplit(self.path)
            if path.query:
                self._write(400, {"error": "query parameters are not supported"})
                return
            try:
                if path.path == "/api/v1/webhooks/orders":
                    raw_body = self._request_body()
                    result = hub._order_service().process_signed_event(
                        raw_body, self.headers.get("X-Jroc-Signature", "")
                    )
                    self._write(200, result)
                    return
                data = self._json_body()
                if path.path == "/api/v1/workflows/dispatch":
                    _expect_fields(data, {"repo", "workflow", "payload"})
                    result = hub.dispatch(data["repo"], data["workflow"], data["payload"])
                    self._write(200, result)
                elif path.path == "/api/v1/agents/dispatch":
                    _expect_fields(data, {"repo", "agent", "payload"})
                    result = hub.dispatch_agent(data["repo"], data["agent"], data["payload"])
                    self._write(200, result)
                elif path.path == "/api/v1/business/products":
                    required = {"sku", "name", "category", "price_cents"}
                    optional = {
                        "currency", "stock_quantity", "min_price_cents", "max_price_cents",
                        "description", "license_required", "license_days", "activation_limit",
                    }
                    _expect_fields(data, required, optional)
                    self._write(201, hub.business.create_product(**data))
                elif path.path == "/api/v1/business/inventory":
                    _expect_fields(data, {"sku", "delta"})
                    self._write(200, hub.business.update_inventory(data["sku"], data["delta"]))
                elif path.path == "/api/v1/business/repair-tickets":
                    _expect_fields(
                        data, {"customer_id", "device", "description"}, {"estimate_cents"}
                    )
                    self._write(201, hub.business.create_repair_ticket(**data))
                elif path.path == "/api/v1/business/repair-tickets/update":
                    _expect_fields(
                        data, {"ticket_id", "status"}, {"estimate_cents"}
                    )
                    self._write(200, hub.business.update_repair_ticket(**data))
                elif path.path == "/api/v1/business/drama-assets":
                    _expect_fields(
                        data, {"series", "asset_type", "title", "uri"}, {"metadata"}
                    )
                    self._write(201, hub.business.add_drama_asset(**data))
                elif path.path == "/api/v1/business/pricing":
                    _expect_fields(data, {"sku"}, {"demand_score"})
                    self._write(
                        200,
                        hub.business.apply_price(data["sku"], data.get("demand_score", 50)),
                    )
                elif path.path == "/api/v1/licenses/validate":
                    _expect_fields(data, {"key"}, {"online", "machine_id"})
                    self._write(
                        200,
                        hub._license_service().validate_license(
                            data["key"], data.get("online", False), data.get("machine_id")
                        ),
                    )
                elif path.path == "/api/v1/licenses/activate":
                    _expect_fields(data, {"key", "machine_id"})
                    self._write(
                        200,
                        hub._license_service().activate_license(data["key"], data["machine_id"]),
                    )
                elif path.path == "/api/v1/licenses/revoke":
                    _expect_fields(data, {"key"})
                    hub._license_service().revoke_license(data["key"])
                    self._write(200, {"revoked": True})
                else:
                    self._write(404, {"error": "unknown API route"})
            except (ValueError, KeyError, RuntimeError, sqlite3.Error) as error:
                self._handle_error(error)

        def do_PUT(self):
            if not self._authorize():
                return
            self._write(405, {"error": "method not allowed"})

        def do_DELETE(self):
            if not self._authorize():
                return
            self._write(405, {"error": "method not allowed"})

    server = LoopbackHTTPServer((host, port), Handler)
    return server, api_token


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _interactive(hub: EcosystemHub) -> int:
    print("Jrocai ecosystem hub")
    print("Commands: list, dispatch, agent, business, serve, exit")
    while True:
        try:
            command = input("hub> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if command in {"exit", "quit"}:
            return 0
        if command == "list":
            _print_json(hub.list_repositories())
        elif command == "dispatch":
            _print_json(hub.list_repositories())
            try:
                repo = input("Repository id> ").strip()
                repository = hub.repositories.get(repo)
                if repository is None:
                    raise ValueError(f"unknown repository: {repo}")
                print("Workflows: " + ", ".join(repository.manifest["workflows"]))
                workflow = input("Workflow> ").strip()
                payload_text = input('Payload JSON (for example {"task":"..."}): ')
                payload = _parse_json(payload_text.encode("utf-8"))
                _print_json(hub.dispatch(repo, workflow, payload))
            except (ValueError, KeyError, RuntimeError) as error:
                print(f"Dispatch failed: {error}", file=sys.stderr)
        elif command == "agent":
            _print_json(hub.list_repositories())
            try:
                repo = input("Repository id> ").strip()
                repository = hub.repositories.get(repo)
                if repository is None:
                    raise ValueError(f"unknown repository: {repo}")
                if not repository.manifest["agents"]:
                    raise ValueError(f"repository {repo!r} does not advertise agents")
                print("Agents: " + ", ".join(repository.manifest["agents"]))
                agent = input("Agent> ").strip()
                payload_text = input('Payload JSON (for example {"task":"..."}): ')
                payload = _parse_json(payload_text.encode("utf-8"))
                _print_json(hub.dispatch_agent(repo, agent, payload))
            except (ValueError, KeyError, RuntimeError) as error:
                print(f"Agent dispatch failed: {error}", file=sys.stderr)
        elif command == "business":
            print("Products:")
            _print_json(hub.business.list_products())
            print("Sales:")
            _print_json(hub.business.list_sales())
            print("Revenue:")
            _print_json(hub.business.revenue_summary())
            print("Repair tickets:")
            _print_json(hub.business.list_repair_tickets())
            print("Drama assets:")
            _print_json(hub.business.list_drama_assets())
            print("Use `python business_command.py --help` for business write operations.")
        elif command == "serve":
            try:
                port_text = input("Loopback API port [8765]> ").strip()
                port = int(port_text) if port_text else 8765
                server, token = create_api_server(hub, port=port)
                print(f"Listening on http://127.0.0.1:{server.server_port}")
                print(f"Bearer token: {token}")
                print("Press Ctrl+C to stop.")
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    print()
                finally:
                    server.server_close()
            except (ValueError, OSError) as error:
                print(f"Could not start API: {error}", file=sys.stderr)
        elif command:
            print("Unknown command. Choose list, dispatch, agent, business, serve, or exit.")


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = argparse.ArgumentParser(description="Jrocai repository and business hub")
    parser.add_argument("--list", action="store_true", help="list configured bridge repositories")
    parser.add_argument("--serve", action="store_true", help="start the loopback-only API")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dispatch", nargs=2, metavar=("REPO", "WORKFLOW"))
    parser.add_argument("--agent", nargs=2, metavar=("REPO", "AGENT"))
    parser.add_argument("--input", help="JSON workflow payload for --dispatch")
    arguments = parser.parse_args(argv)
    try:
        hub = EcosystemHub()
        if arguments.list:
            _print_json(hub.list_repositories())
            return 0
        if arguments.dispatch:
            if arguments.agent:
                parser.error("--dispatch and --agent cannot be used together")
            if not arguments.input:
                parser.error("--dispatch requires --input")
            payload = _parse_json(arguments.input.encode("utf-8"))
            _print_json(hub.dispatch(*arguments.dispatch, payload))
            return 0
        if arguments.agent:
            if not arguments.input:
                parser.error("--agent requires --input")
            payload = _parse_json(arguments.input.encode("utf-8"))
            _print_json(hub.dispatch_agent(*arguments.agent, payload))
            return 0
        if arguments.input:
            parser.error("--input requires --dispatch")
        if arguments.serve:
            server, token = create_api_server(hub, port=arguments.port)
            print(f"Listening on http://127.0.0.1:{server.server_port}")
            print(f"Bearer token: {token}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print()
            finally:
                server.server_close()
            return 0
        return _interactive(hub)
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"Hub error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
