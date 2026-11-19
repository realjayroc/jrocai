"""Signed local licenses, activation tracking, and authenticated order fulfillment."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from typing import Any

from business_command import BusinessCommand, _integer, _text, _now
from database import database_connection


_TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_SIGNATURE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_MAX_WEBHOOK_BYTES = 256 * 1024
_MAX_ORDER_LINES = 50
_MAX_LINE_QUANTITY = 100


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not _TOKEN_PART.fullmatch(value):
        raise ValueError("license key contains invalid encoding")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("license key contains invalid encoding") from error
    if _b64encode(decoded) != value:
        raise ValueError("license key contains non-canonical encoding")
    return decoded


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


class LicenseEngine:
    def __init__(
        self,
        db_path: str | None = None,
        secret: str | bytes | None = None,
        business: BusinessCommand | None = None,
    ):
        self.business = business or BusinessCommand(db_path)
        if secret is None:
            secret = os.environ.get("JROCAI_LICENSE_SECRET")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise RuntimeError("JROCAI_LICENSE_SECRET must contain at least 32 bytes")
        self._secret = secret
        self.db_path = self.business.db_path
        with database_connection(self.db_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS licenses (
                    license_id TEXT PRIMARY KEY,
                    sku TEXT NOT NULL REFERENCES business_products(sku),
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    activation_limit INTEGER NOT NULL CHECK (activation_limit > 0),
                    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS license_activations (
                    license_id TEXT NOT NULL REFERENCES licenses(license_id),
                    machine_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (license_id, machine_fingerprint)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS order_fulfillments (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    body_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _signed_key(self, claims: dict[str, Any]) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        encoded = _b64encode(payload)
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"JRL1.{encoded}.{_b64encode(signature)}"

    def _claims_from_row(self, row) -> dict[str, Any]:
        return {
            "v": 1,
            "id": row["license_id"],
            "sku": row["sku"],
            "iat": row["issued_at"],
            "exp": row["expires_at"],
            "max_activations": row["activation_limit"],
        }

    def _issue(
        self,
        connection,
        sku: str,
        validity_days: int | None = None,
        activation_limit: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        product = self.business._product(connection, sku)
        if not product["active"]:
            raise ValueError(f"Product {sku} is inactive")
        if not product["license_required"]:
            raise ValueError(f"Product {sku} does not require a license")
        days = product["license_days"] if validity_days is None else _integer(
            validity_days, "validity_days", minimum=1, maximum=36500
        )
        activations = product["activation_limit"] if activation_limit is None else _integer(
            activation_limit, "activation_limit", minimum=1, maximum=10000
        )
        issued_at = int(time.time())
        claims = {
            "v": 1,
            "id": uuid.uuid4().hex,
            "sku": sku,
            "iat": issued_at,
            "exp": issued_at + days * 86400 if days is not None else None,
            "max_activations": activations,
        }
        connection.execute(
            """INSERT INTO licenses
               (license_id, sku, issued_at, expires_at, activation_limit, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (claims["id"], sku, claims["iat"], claims["exp"], activations, _now()),
        )
        return claims, self._signed_key(claims)

    def generate_license(
        self,
        sku: str,
        validity_days: int | None = None,
        activation_limit: int | None = None,
    ) -> str:
        sku = _text(sku, "sku", 64).upper()
        with database_connection(self.db_path) as connection:
            _, key = self._issue(connection, sku, validity_days, activation_limit)
            return key

    def _decode_key(self, key: str) -> dict[str, Any]:
        if not isinstance(key, str) or len(key) > 4096:
            raise ValueError("license key must be a string of at most 4096 characters")
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "JRL1":
            raise ValueError("license key has an unsupported format")
        encoded, signature = parts[1], parts[2]
        if not _TOKEN_PART.fullmatch(encoded) or not _TOKEN_PART.fullmatch(signature):
            raise ValueError("license key contains invalid encoding")
        supplied_signature = _b64decode(signature)
        expected_signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("license key signature is invalid")
        try:
            claims = json.loads(_b64decode(encoded), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("license key payload is invalid") from error
        if not isinstance(claims, dict) or set(claims) != {
            "v", "id", "sku", "iat", "exp", "max_activations"
        }:
            raise ValueError("license key claims are invalid")
        if claims["v"] != 1 or isinstance(claims["v"], bool):
            raise ValueError("license key version is unsupported")
        if not isinstance(claims["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", claims["id"]):
            raise ValueError("license key identifier is invalid")
        if not isinstance(claims["sku"], str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,63}", claims["sku"]):
            raise ValueError("license key product is invalid")
        if isinstance(claims["iat"], bool) or not isinstance(claims["iat"], int) or claims["iat"] > int(time.time()) + 300:
            raise ValueError("license key issue time is invalid")
        if claims["exp"] is not None and (
            isinstance(claims["exp"], bool) or not isinstance(claims["exp"], int)
        ):
            raise ValueError("license key expiration is invalid")
        if claims["exp"] is not None and claims["exp"] <= int(time.time()):
            raise ValueError("license key has expired")
        if claims["exp"] is not None and claims["exp"] <= claims["iat"]:
            raise ValueError("license key expiration is invalid")
        if isinstance(claims["max_activations"], bool) or not isinstance(claims["max_activations"], int) or claims["max_activations"] < 1:
            raise ValueError("license key activation limit is invalid")
        return claims

    def validate_license(
        self,
        key: str,
        online: bool = False,
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(online, bool):
            raise ValueError("online must be a boolean")
        if machine_id is not None:
            machine_id = _text(machine_id, "machine_id", 512)
        claims = self._decode_key(key)
        if not online:
            return {**claims, "valid": True, "validation": "offline"}
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM licenses WHERE license_id = ?", (claims["id"],)
            ).fetchone()
            if row is None or row["status"] != "active" or self._claims_from_row(row) != claims:
                raise ValueError("license is not active in the online registry")
            if machine_id is not None:
                fingerprint = self._machine_fingerprint(claims["id"], machine_id)
                activation = connection.execute(
                    """SELECT 1 FROM license_activations
                       WHERE license_id = ? AND machine_fingerprint = ?""",
                    (claims["id"], fingerprint),
                ).fetchone()
                if activation is None:
                    raise ValueError("license is not activated for this machine")
        return {**claims, "valid": True, "validation": "online"}

    def _machine_fingerprint(self, license_id: str, machine_id: str) -> str:
        message = b"machine\0" + license_id.encode("ascii") + b"\0" + machine_id.encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def activate_license(self, key: str, machine_id: str) -> dict[str, Any]:
        claims = self._decode_key(key)
        machine_id = _text(machine_id, "machine_id", 512)
        fingerprint = self._machine_fingerprint(claims["id"], machine_id)
        with database_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM licenses WHERE license_id = ?", (claims["id"],)
            ).fetchone()
            if row is None or row["status"] != "active" or self._claims_from_row(row) != claims:
                raise ValueError("license is not active in the online registry")
            existing = connection.execute(
                """SELECT 1 FROM license_activations
                   WHERE license_id = ? AND machine_fingerprint = ?""",
                (claims["id"], fingerprint),
            ).fetchone()
            if existing is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM license_activations WHERE license_id = ?",
                    (claims["id"],),
                ).fetchone()[0]
                if count >= row["activation_limit"]:
                    raise ValueError("license activation limit has been reached")
                connection.execute(
                    """INSERT INTO license_activations
                       (license_id, machine_fingerprint, created_at) VALUES (?, ?, ?)""",
                    (claims["id"], fingerprint, _now()),
                )
        return {**claims, "valid": True, "validation": "online", "activated": True}

    def revoke_license(self, key: str) -> None:
        claims = self._decode_key(key)
        with database_connection(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE licenses SET status = 'revoked' WHERE license_id = ? AND status = 'active'",
                (claims["id"],),
            )
            if cursor.rowcount != 1:
                raise ValueError("license is not active in the online registry")

    def license_key(self, license_id: str, connection=None) -> str:
        license_id = _text(license_id, "license_id", 32)
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM licenses WHERE license_id = ?", (license_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown license: {license_id}")
            return self._signed_key(self._claims_from_row(row))
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM licenses WHERE license_id = ?", (license_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown license: {license_id}")
            return self._signed_key(self._claims_from_row(row))


class OrderFulfillmentService:
    """Processes payment-provider events authenticated with a per-install secret."""

    def __init__(
        self,
        business: BusinessCommand | None = None,
        license_engine: LicenseEngine | None = None,
        webhook_secret: str | bytes | None = None,
    ):
        self.business = business or BusinessCommand()
        self.license_engine = license_engine or LicenseEngine(
            db_path=str(self.business.db_path), business=self.business
        )
        if webhook_secret is None:
            webhook_secret = os.environ.get("JROCAI_WEBHOOK_SECRET")
        if isinstance(webhook_secret, str):
            webhook_secret = webhook_secret.encode("utf-8")
        if not isinstance(webhook_secret, bytes) or len(webhook_secret) < 32:
            raise RuntimeError("JROCAI_WEBHOOK_SECRET must contain at least 32 bytes")
        self._webhook_secret = webhook_secret

    @staticmethod
    def signature_for(raw_body: bytes, secret: str | bytes) -> str:
        """Create a provider signature for integration tests and local senders."""
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        return "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    def _verify_signature(self, raw_body: bytes, signature: str) -> None:
        match = _SIGNATURE.fullmatch(signature) if isinstance(signature, str) else None
        if not match:
            raise ValueError("signature must use the sha256=<hex> format")
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(match.group(1).lower(), expected):
            raise ValueError("webhook signature is invalid")

    def _validate_event(self, event: Any) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError("webhook body must be a JSON object")
        required = {"event_id", "order_id", "customer_id", "status", "currency", "amount_cents", "items"}
        if set(event) != required:
            missing = sorted(required - set(event))
            extra = sorted(set(event) - required)
            raise ValueError(f"webhook fields mismatch; missing={missing}, extra={extra}")
        event["event_id"] = _text(event["event_id"], "event_id", 160)
        event["order_id"] = _text(event["order_id"], "order_id", 160)
        event["customer_id"] = _text(event["customer_id"], "customer_id", 200)
        if event["status"] != "paid":
            raise ValueError("only paid orders can be fulfilled")
        currency = _text(event["currency"], "currency", 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter ISO currency code")
        event["currency"] = currency
        event["amount_cents"] = _integer(event["amount_cents"], "amount_cents")
        items = event["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= _MAX_ORDER_LINES:
            raise ValueError(f"items must contain between 1 and {_MAX_ORDER_LINES} entries")
        normalized_items = []
        seen_skus = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"sku", "quantity", "unit_price_cents"}:
                raise ValueError("each item must contain exactly sku, quantity, and unit_price_cents")
            sku = _text(item["sku"], "sku", 64).upper()
            if sku in seen_skus:
                raise ValueError(f"order contains duplicate product SKU: {sku}")
            seen_skus.add(sku)
            normalized_items.append({
                "sku": sku,
                "quantity": _integer(item["quantity"], "quantity", minimum=1, maximum=_MAX_LINE_QUANTITY),
                "unit_price_cents": _integer(item["unit_price_cents"], "unit_price_cents"),
            })
        event["items"] = normalized_items
        return event

    def _response_for(self, connection, result: dict[str, Any]) -> dict[str, Any]:
        sales = []
        for sale_id in result["sale_ids"]:
            row = connection.execute(
                "SELECT * FROM business_sales WHERE sale_id = ?", (sale_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Fulfillment record references missing sale {sale_id}")
            sales.append(dict(row))
        licenses = []
        for license_id in result["license_ids"]:
            row = connection.execute(
                "SELECT sku FROM licenses WHERE license_id = ?", (license_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Fulfillment record references missing license {license_id}")
            licenses.append({"sku": row["sku"], "key": self.license_engine.license_key(license_id, connection)})
        return {"status": "fulfilled", "order_id": result["order_id"], "sales": sales, "licenses": licenses}

    def process_signed_event(self, raw_body: bytes, signature: str) -> dict[str, Any]:
        if not isinstance(raw_body, bytes) or not 1 <= len(raw_body) <= _MAX_WEBHOOK_BYTES:
            raise ValueError(f"webhook body must contain 1 to {_MAX_WEBHOOK_BYTES} bytes")
        self._verify_signature(raw_body, signature)
        try:
            event = json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number {value}")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("webhook body must be valid UTF-8 JSON") from error
        event = self._validate_event(event)
        body_hash = hashlib.sha256(raw_body).hexdigest()
        with database_connection(self.business.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM order_fulfillments WHERE event_id = ?", (event["event_id"],)
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["body_hash"], body_hash):
                    raise ValueError("event_id was already used with a different payload")
                return self._response_for(connection, json.loads(existing["result_json"]))
            existing_order = connection.execute(
                "SELECT event_id FROM order_fulfillments WHERE order_id = ?", (event["order_id"],)
            ).fetchone()
            if existing_order is not None:
                raise ValueError("order_id has already been fulfilled")

            expected_total = 0
            products = []
            for item in event["items"]:
                product = self.business._product(connection, item["sku"])
                if not product["active"]:
                    raise ValueError(f"Product {item['sku']} is inactive")
                if product["currency"] != event["currency"]:
                    raise ValueError(f"Currency mismatch for product {item['sku']}")
                expected_total += item["quantity"] * item["unit_price_cents"]
                products.append(product)
            if expected_total != event["amount_cents"]:
                raise ValueError("amount_cents does not match the order line items")

            sale_ids = []
            license_ids = []
            for item, product in zip(event["items"], products):
                sale = self.business._record_sale(
                    connection,
                    event["order_id"],
                    item["sku"],
                    item["quantity"],
                    item["unit_price_cents"],
                    event["customer_id"],
                )
                sale_ids.append(sale["sale_id"])
                if product["license_required"]:
                    for _ in range(item["quantity"]):
                        claims, _ = self.license_engine._issue(connection, item["sku"])
                        license_ids.append(claims["id"])
            result = {
                "order_id": event["order_id"],
                "sale_ids": sale_ids,
                "license_ids": license_ids,
            }
            connection.execute(
                """INSERT INTO order_fulfillments
                   (event_id, order_id, body_hash, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event["event_id"], event["order_id"], body_hash,
                 json.dumps(result, separators=(",", ":")), _now()),
            )
            return self._response_for(connection, result)
