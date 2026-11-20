"""Persistent product, inventory, repair, asset, sales, and pricing operations."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from database import database_connection, database_path


PRODUCT_CATEGORIES = {"digital_app", "physical_product", "repair_service", "drama_asset"}
TICKET_STATUSES = {"open", "in_progress", "waiting_for_parts", "completed", "cancelled"}
TICKET_TRANSITIONS = {
    "open": {"in_progress", "cancelled"},
    "in_progress": {"waiting_for_parts", "completed", "cancelled"},
    "waiting_for_parts": {"in_progress", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
_SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, field: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _integer(value: Any, field: str, minimum: int = 0, maximum: int = 2_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


class BusinessCommand:
    """Business records backed by the same SQLite file as workflow state."""

    def __init__(self, db_path: str | None = None):
        self.db_path = database_path(db_path)
        with database_connection(self.db_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS business_products (
                    sku TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL,
                    base_price_cents INTEGER NOT NULL CHECK (base_price_cents >= 0),
                    current_price_cents INTEGER NOT NULL CHECK (current_price_cents >= 0),
                    min_price_cents INTEGER NOT NULL CHECK (min_price_cents >= 0),
                    max_price_cents INTEGER NOT NULL CHECK (max_price_cents >= min_price_cents),
                    stock_quantity INTEGER CHECK (stock_quantity IS NULL OR stock_quantity >= 0),
                    license_required INTEGER NOT NULL CHECK (license_required IN (0, 1)),
                    license_days INTEGER CHECK (license_days IS NULL OR license_days > 0),
                    activation_limit INTEGER NOT NULL CHECK (activation_limit > 0),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS business_sales (
                    sale_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    customer_id TEXT,
                    sku TEXT NOT NULL REFERENCES business_products(sku),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
                    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(order_id, sku)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS repair_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    device TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    estimate_cents INTEGER CHECK (estimate_cents IS NULL OR estimate_cents >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS drama_assets (
                    asset_id TEXT PRIMARY KEY,
                    series TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pricing_history (
                    change_id TEXT PRIMARY KEY,
                    sku TEXT NOT NULL REFERENCES business_products(sku),
                    demand_score REAL NOT NULL,
                    old_price_cents INTEGER NOT NULL,
                    new_price_cents INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _product(self, connection, sku: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM business_products WHERE sku = ?", (sku,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown product SKU: {sku}")
        return dict(row)

    def create_product(
        self,
        sku: str,
        name: str,
        category: str,
        price_cents: int,
        currency: str = "USD",
        stock_quantity: int | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        description: str = "",
        license_required: bool | None = None,
        license_days: int | None = 365,
        activation_limit: int = 1,
    ) -> dict[str, Any]:
        sku = _text(sku, "sku", 64).upper()
        if not _SKU_PATTERN.fullmatch(sku):
            raise ValueError("sku may contain only letters, digits, dot, underscore, and hyphen")
        name = _text(name, "name", 200)
        if not isinstance(category, str) or category not in PRODUCT_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(sorted(PRODUCT_CATEGORIES))}")
        currency = _text(currency, "currency", 3).upper()
        if not _CURRENCY_PATTERN.fullmatch(currency):
            raise ValueError("currency must be a three-letter ISO currency code")
        base_price = _integer(price_cents, "price_cents")
        minimum = base_price if min_price_cents is None else _integer(min_price_cents, "min_price_cents")
        maximum = base_price if max_price_cents is None else _integer(max_price_cents, "max_price_cents")
        if minimum > base_price or base_price > maximum:
            raise ValueError("price_cents must be between min_price_cents and max_price_cents")
        if stock_quantity is not None:
            stock_quantity = _integer(stock_quantity, "stock_quantity")
        if license_required is None:
            license_required = category == "digital_app"
        if not isinstance(license_required, bool):
            raise ValueError("license_required must be a boolean")
        if license_days is not None:
            license_days = _integer(license_days, "license_days", minimum=1, maximum=36500)
        activation_limit = _integer(activation_limit, "activation_limit", minimum=1, maximum=10000)
        description = description.strip() if isinstance(description, str) else None
        if description is None or len(description) > 4000:
            raise ValueError("description must be a string of at most 4000 characters")

        now = _now()
        with database_connection(self.db_path) as connection:
            connection.execute(
                """INSERT INTO business_products
                   (sku, name, category, description, currency, base_price_cents,
                    current_price_cents, min_price_cents, max_price_cents, stock_quantity,
                    license_required, license_days, activation_limit, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sku, name, category, description, currency, base_price, base_price,
                 minimum, maximum, stock_quantity, int(license_required), license_days,
                 activation_limit, now, now),
            )
            return self._product(connection, sku)

    def get_product(self, sku: str) -> dict[str, Any]:
        with database_connection(self.db_path) as connection:
            return self._product(connection, _text(sku, "sku", 64).upper())

    def list_products(self, active_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(active_only, bool):
            raise ValueError("active_only must be a boolean")
        limit = _integer(limit, "limit", minimum=1, maximum=100)
        query = "SELECT * FROM business_products"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY sku LIMIT ?"
        with database_connection(self.db_path) as connection:
            return [dict(row) for row in connection.execute(query, (limit,)).fetchall()]

    def set_product_active(self, sku: str, active: bool) -> dict[str, Any]:
        if not isinstance(active, bool):
            raise ValueError("active must be a boolean")
        sku = _text(sku, "sku", 64).upper()
        with database_connection(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE business_products SET active = ?, updated_at = ? WHERE sku = ?",
                (int(active), _now(), sku),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown product SKU: {sku}")
            return self._product(connection, sku)

    def update_inventory(self, sku: str, delta: int) -> dict[str, Any]:
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValueError("delta must be a non-zero integer")
        sku = _text(sku, "sku", 64).upper()
        with database_connection(self.db_path) as connection:
            product = self._product(connection, sku)
            current = product["stock_quantity"]
            if current is None:
                raise ValueError(f"Product {sku} does not track physical inventory")
            updated = current + delta
            if updated < 0:
                raise ValueError("inventory cannot become negative")
            connection.execute(
                "UPDATE business_products SET stock_quantity = ?, updated_at = ? WHERE sku = ?",
                (updated, _now(), sku),
            )
            return self._product(connection, sku)

    def _record_sale(
        self,
        connection,
        order_id: str,
        sku: str,
        quantity: int,
        unit_price_cents: int | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        product = self._product(connection, sku)
        if not product["active"]:
            raise ValueError(f"Product {sku} is inactive")
        if unit_price_cents is None:
            unit_price_cents = product["current_price_cents"]
        unit_price_cents = _integer(unit_price_cents, "unit_price_cents")
        stock = product["stock_quantity"]
        if stock is not None and stock < quantity:
            raise ValueError(f"Insufficient inventory for {sku}")
        sale_id = str(uuid.uuid4())
        now = _now()
        connection.execute(
            """INSERT INTO business_sales
               (sale_id, order_id, customer_id, sku, quantity, unit_price_cents,
                total_cents, currency, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sale_id, order_id, customer_id, sku, quantity, unit_price_cents,
             unit_price_cents * quantity, product["currency"], now),
        )
        if stock is not None:
            connection.execute(
                "UPDATE business_products SET stock_quantity = ?, updated_at = ? WHERE sku = ?",
                (stock - quantity, now, sku),
            )
        return {
            "sale_id": sale_id, "order_id": order_id, "customer_id": customer_id,
            "sku": sku, "quantity": quantity, "unit_price_cents": unit_price_cents,
            "total_cents": unit_price_cents * quantity, "currency": product["currency"],
            "created_at": now,
        }

    def record_sale(
        self,
        order_id: str,
        sku: str,
        quantity: int,
        unit_price_cents: int | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        order_id = _text(order_id, "order_id", 160)
        sku = _text(sku, "sku", 64).upper()
        quantity = _integer(quantity, "quantity", minimum=1, maximum=10000)
        if customer_id is not None:
            customer_id = _text(customer_id, "customer_id", 200)
        with database_connection(self.db_path) as connection:
            return self._record_sale(
                connection, order_id, sku, quantity, unit_price_cents, customer_id
            )

    def list_sales(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = _integer(limit, "limit", minimum=1, maximum=100)
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM business_sales ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def revenue_summary(self) -> list[dict[str, Any]]:
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """SELECT currency, SUM(total_cents) AS revenue_cents,
                          SUM(quantity) AS units_sold, COUNT(DISTINCT order_id) AS order_count
                   FROM business_sales GROUP BY currency ORDER BY currency LIMIT 100"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_repair_ticket(
        self,
        customer_id: str,
        device: str,
        description: str,
        estimate_cents: int | None = None,
    ) -> dict[str, Any]:
        customer_id = _text(customer_id, "customer_id", 200)
        device = _text(device, "device", 200)
        description = _text(description, "description", 4000)
        if estimate_cents is not None:
            estimate_cents = _integer(estimate_cents, "estimate_cents")
        ticket_id = str(uuid.uuid4())
        now = _now()
        with database_connection(self.db_path) as connection:
            connection.execute(
                """INSERT INTO repair_tickets
                   (ticket_id, customer_id, device, description, status, estimate_cents,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
                (ticket_id, customer_id, device, description, estimate_cents, now, now),
            )
            return dict(connection.execute(
                "SELECT * FROM repair_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone())

    def update_repair_ticket(
        self,
        ticket_id: str,
        status: str,
        estimate_cents: int | None = None,
    ) -> dict[str, Any]:
        ticket_id = _text(ticket_id, "ticket_id", 80)
        if not isinstance(status, str) or status not in TICKET_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(TICKET_STATUSES))}")
        if estimate_cents is not None:
            estimate_cents = _integer(estimate_cents, "estimate_cents")
        with database_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM repair_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown repair ticket: {ticket_id}")
            current_status = row["status"]
            if status != current_status and status not in TICKET_TRANSITIONS[current_status]:
                raise ValueError(f"Invalid repair ticket transition: {current_status} -> {status}")
            next_estimate = estimate_cents if estimate_cents is not None else row["estimate_cents"]
            connection.execute(
                """UPDATE repair_tickets SET status = ?, estimate_cents = ?, updated_at = ?
                   WHERE ticket_id = ?""",
                (status, next_estimate, _now(), ticket_id),
            )
            return dict(connection.execute(
                "SELECT * FROM repair_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone())

    def list_repair_tickets(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if status is not None and (not isinstance(status, str) or status not in TICKET_STATUSES):
            raise ValueError(f"status must be one of: {', '.join(sorted(TICKET_STATUSES))}")
        limit = _integer(limit, "limit", minimum=1, maximum=100)
        query = "SELECT * FROM repair_tickets"
        parameters = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters += (limit,)
        with database_connection(self.db_path) as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def add_drama_asset(
        self,
        series: str,
        asset_type: str,
        title: str,
        uri: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        series = _text(series, "series", 200)
        asset_type = _text(asset_type, "asset_type", 100)
        title = _text(title, "title", 300)
        uri = _text(uri, "uri", 2048)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("metadata must contain only JSON-compatible values") from error
        if len(metadata_json.encode("utf-8")) > 16 * 1024:
            raise ValueError("metadata must not exceed 16384 UTF-8 bytes")
        asset_id = str(uuid.uuid4())
        now = _now()
        with database_connection(self.db_path) as connection:
            connection.execute(
                """INSERT INTO drama_assets
                   (asset_id, series, asset_type, title, uri, status, metadata_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (asset_id, series, asset_type, title, uri, metadata_json, now, now),
            )
            return self._asset_dict(connection.execute(
                "SELECT * FROM drama_assets WHERE asset_id = ?", (asset_id,)
            ).fetchone())

    @staticmethod
    def _asset_dict(row) -> dict[str, Any]:
        asset = dict(row)
        asset["metadata"] = json.loads(asset.pop("metadata_json"))
        return asset

    def list_drama_assets(self, series: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = _integer(limit, "limit", minimum=1, maximum=100)
        query = "SELECT * FROM drama_assets"
        parameters = ()
        if series is not None:
            series = _text(series, "series", 200)
            query += " WHERE series = ?"
            parameters = (series,)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters += (limit,)
        with database_connection(self.db_path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._asset_dict(row) for row in rows]

    def calculate_price(self, sku: str, demand_score: float = 50) -> dict[str, Any]:
        if isinstance(demand_score, bool) or not isinstance(demand_score, (int, float)):
            raise ValueError("demand_score must be a number from 0 to 100")
        demand_score = float(demand_score)
        if not math.isfinite(demand_score) or not 0 <= demand_score <= 100:
            raise ValueError("demand_score must be a finite number from 0 to 100")
        sku = _text(sku, "sku", 64).upper()
        with database_connection(self.db_path) as connection:
            product = self._product(connection, sku)
        scarcity_adjustment = 0.0
        stock = product["stock_quantity"]
        if stock is not None:
            if stock <= 5:
                scarcity_adjustment = 0.10
            elif stock > 100:
                scarcity_adjustment = -0.05
        demand_adjustment = (demand_score - 50) / 200
        multiplier = max(0.5, 1 + demand_adjustment + scarcity_adjustment)
        proposed = round(product["base_price_cents"] * multiplier)
        price = min(product["max_price_cents"], max(product["min_price_cents"], proposed))
        return {
            "sku": sku,
            "currency": product["currency"],
            "base_price_cents": product["base_price_cents"],
            "current_price_cents": product["current_price_cents"],
            "calculated_price_cents": price,
            "demand_score": demand_score,
            "inventory_quantity": stock,
            "min_price_cents": product["min_price_cents"],
            "max_price_cents": product["max_price_cents"],
        }

    def apply_price(self, sku: str, demand_score: float = 50) -> dict[str, Any]:
        quote = self.calculate_price(sku, demand_score)
        now = _now()
        with database_connection(self.db_path) as connection:
            product = self._product(connection, quote["sku"])
            connection.execute(
                """UPDATE business_products SET current_price_cents = ?, updated_at = ?
                   WHERE sku = ?""",
                (quote["calculated_price_cents"], now, quote["sku"]),
            )
            connection.execute(
                """INSERT INTO pricing_history
                   (change_id, sku, demand_score, old_price_cents, new_price_cents, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), quote["sku"], quote["demand_score"],
                 product["current_price_cents"], quote["calculated_price_cents"], now),
            )
            return self._product(connection, quote["sku"])

    def list_pricing_history(self, sku: str, limit: int = 100) -> list[dict[str, Any]]:
        sku = _text(sku, "sku", 64).upper()
        limit = _integer(limit, "limit", minimum=1, maximum=100)
        with database_connection(self.db_path) as connection:
            rows = connection.execute(
                """SELECT * FROM pricing_history WHERE sku = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (sku, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage jrocai business data")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("products", help="list the active product catalog")
    commands.add_parser("sales", help="list recent sales")
    commands.add_parser("revenue", help="summarize recorded sales by currency")
    product = commands.add_parser("product-add", help="add a catalog product")
    product.add_argument("--sku", required=True)
    product.add_argument("--name", required=True)
    product.add_argument("--category", required=True, choices=sorted(PRODUCT_CATEGORIES))
    product.add_argument("--price-cents", required=True, type=int)
    product.add_argument("--currency", default="USD")
    product.add_argument("--stock-quantity", type=int)
    product.add_argument("--min-price-cents", type=int)
    product.add_argument("--max-price-cents", type=int)
    product.add_argument("--description", default="")
    license_choice = product.add_mutually_exclusive_group()
    license_choice.add_argument("--license-required", dest="license_required", action="store_true")
    license_choice.add_argument("--no-license", dest="license_required", action="store_false")
    product.set_defaults(license_required=None)

    inventory = commands.add_parser("inventory", help="adjust tracked stock")
    inventory.add_argument("--sku", required=True)
    inventory.add_argument("--delta", required=True, type=int)
    ticket = commands.add_parser("ticket-add", help="open a repair ticket")
    ticket.add_argument("--customer-id", required=True)
    ticket.add_argument("--device", required=True)
    ticket.add_argument("--description", required=True)
    ticket.add_argument("--estimate-cents", type=int)
    update_ticket = commands.add_parser("ticket-update", help="advance a repair ticket")
    update_ticket.add_argument("--ticket-id", required=True)
    update_ticket.add_argument("--status", required=True, choices=sorted(TICKET_STATUSES))
    update_ticket.add_argument("--estimate-cents", type=int)
    commands.add_parser("tickets", help="list repair tickets")

    asset = commands.add_parser("asset-add", help="register a drama-series asset")
    asset.add_argument("--series", required=True)
    asset.add_argument("--asset-type", required=True)
    asset.add_argument("--title", required=True)
    asset.add_argument("--uri", required=True)
    asset.add_argument("--metadata", default="{}")
    commands.add_parser("assets", help="list drama-series assets")
    pricing = commands.add_parser("price", help="calculate and persist a dynamic price")
    pricing.add_argument("--sku", required=True)
    pricing.add_argument("--demand-score", type=float, default=50)

    arguments = parser.parse_args(argv)
    try:
        from env_config import load_local_env
        load_local_env()
        business = BusinessCommand()
        if arguments.command == "products":
            result = business.list_products()
        elif arguments.command == "sales":
            result = business.list_sales()
        elif arguments.command == "revenue":
            result = business.revenue_summary()
        elif arguments.command == "product-add":
            result = business.create_product(
                sku=arguments.sku,
                name=arguments.name,
                category=arguments.category,
                price_cents=arguments.price_cents,
                currency=arguments.currency,
                stock_quantity=arguments.stock_quantity,
                min_price_cents=arguments.min_price_cents,
                max_price_cents=arguments.max_price_cents,
                description=arguments.description,
                license_required=arguments.license_required,
            )
        elif arguments.command == "inventory":
            result = business.update_inventory(arguments.sku, arguments.delta)
        elif arguments.command == "ticket-add":
            result = business.create_repair_ticket(
                arguments.customer_id, arguments.device, arguments.description,
                arguments.estimate_cents,
            )
        elif arguments.command == "ticket-update":
            result = business.update_repair_ticket(
                arguments.ticket_id, arguments.status, arguments.estimate_cents
            )
        elif arguments.command == "tickets":
            result = business.list_repair_tickets()
        elif arguments.command == "asset-add":
            metadata = json.loads(arguments.metadata)
            result = business.add_drama_asset(
                arguments.series, arguments.asset_type, arguments.title,
                arguments.uri, metadata,
            )
        elif arguments.command == "assets":
            result = business.list_drama_assets()
        else:
            result = business.apply_price(arguments.sku, arguments.demand_score)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, RuntimeError) as error:
        print(f"Business operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
