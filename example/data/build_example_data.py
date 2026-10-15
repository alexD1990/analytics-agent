"""Builds the example databases in this folder: crm.db, shop.db and inventory.db.

The data describes a fictional online store that sells coffee and coffee
equipment. It is deterministic (fixed random seed), so running the script
again gives the same data. It contains deliberate data quality issues, so you
can see how the agent handles them:

  - region and category written with varying case and stray whitespace
  - customers with missing email or region, and customers registered twice
  - credit notes: order lines with negative quantity
  - customers migrated from an old system, whose created_date is the migration
    date, so many of their orders are dated before they were "created"
  - orders with a placeholder date (1970-01-01) and orders without a status
  - orders for customer IDs that do not exist in the CRM
  - order lines for product IDs that do not exist in the catalogue
  - order lines registered twice

Usage:
    python example/data/build_example_data.py

Existing .db files in this folder are replaced.
"""
from __future__ import annotations

import itertools
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 2025

N_CUSTOMERS = 746
N_DUPLICATE_CUSTOMERS = 9
N_ORDERS = 6400
FIRST_ORDER_DATE = date(2024, 1, 1)
LAST_ORDER_DATE = date(2025, 6, 30)
MIGRATION_DATE = date(2024, 9, 1)       # migrated customers got this (or a few days later) as created_date
DISCONTINUED_DATE = date(2024, 10, 31)  # inactive products were sold until this date
PLACEHOLDER_DATE = date(1970, 1, 1)

REGIONS = {
    "London": 20, "South East": 15, "South West": 9, "Midlands": 14, "North West": 12,
    "North East": 7, "Scotland": 10, "Wales": 6, "International": 7,
}

FIRST_NAMES = [
    "Oliver", "Amelia", "George", "Isla", "Harry", "Ava", "Jack", "Mia", "Noah", "Grace",
    "Leo", "Sophie", "Arthur", "Emily", "Oscar", "Lily", "Thomas", "Freya", "James", "Ella",
    "William", "Chloe", "Henry", "Ruby", "Samuel", "Hannah", "Daniel", "Lucy", "Adam", "Zara",
]

LAST_NAMES = [
    "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Patel", "Wright",
    "Robinson", "Thompson", "Evans", "Walker", "White", "Roberts", "Green", "Hall", "Wood", "Jackson",
    "Clarke", "Hughes", "Edwards", "Turner", "Khan", "Murphy", "Campbell", "Stewart", "Morgan", "Bennett",
]

# (name, category, price, active, popularity)
PRODUCTS = [
    ("House Blend 250 g", "Coffee", 89.0, True, 30),
    ("House Blend 1 kg", "Coffee", 299.0, True, 14),
    ("Espresso Roast 250 g", "Coffee", 99.0, True, 26),
    ("Espresso Roast 1 kg", "Coffee", 329.0, True, 12),
    ("Ethiopia Yirgacheffe 250 g", "Coffee", 129.0, True, 16),
    ("Colombia Huila 250 g", "Coffee", 119.0, True, 15),
    ("Kenya AA 250 g", "Coffee", 139.0, True, 9),
    ("Guatemala Antigua 250 g", "Coffee", 119.0, False, 6),
    ("Brazil Santos 250 g", "Coffee", 99.0, False, 5),
    ("Decaf Blend 250 g", "Coffee", 99.0, True, 8),
    ("Paper Filters 100 pcs", "Accessories", 49.0, True, 18),
    ("Ceramic Cup Set", "Accessories", 249.0, True, 4),
    ("Travel Mug", "Accessories", 199.0, True, 6),
    ("Storage Canister", "Accessories", 179.0, True, 5),
    ("Milk Jug 600 ml", "Accessories", 159.0, True, 4),
    ("Hand Grinder", "Equipment", 599.0, True, 5),
    ("Burr Grinder", "Equipment", 1899.0, True, 3),
    ("Pour-Over Dripper", "Equipment", 249.0, True, 6),
    ("French Press 1 l", "Equipment", 349.0, True, 5),
    ("Moka Pot 6 Cups", "Equipment", 399.0, True, 4),
    ("Gooseneck Kettle", "Equipment", 699.0, True, 3),
    ("Coffee Scale", "Equipment", 449.0, True, 3),
    ("Home Espresso Machine", "Equipment", 6990.0, True, 1),
    ("Descaling Tablets", "Maintenance", 129.0, True, 6),
    ("Cleaning Brushes", "Maintenance", 99.0, True, 3),
    ("Water Filter Cartridge", "Maintenance", 149.0, True, 4),
    ("Grinder Cleaning Pellets", "Maintenance", 119.0, True, 2),
]

UNKNOWN_PRODUCT_IDS = [9001, 9002, 9003]

CRM_SCHEMA = """
CREATE TABLE customers (
    customer_id  INTEGER PRIMARY KEY,
    name         TEXT,
    email        TEXT,
    region       TEXT,
    created_date DATE
);
"""

SHOP_SCHEMA = """
CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER,
    order_date  DATE,
    status      TEXT
);
CREATE TABLE order_lines (
    line_id    INTEGER PRIMARY KEY,
    order_id   INTEGER,
    product_id INTEGER,
    quantity   INTEGER,
    unit_price REAL
);
"""

INVENTORY_SCHEMA = """
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name       TEXT,
    category   TEXT,
    price      REAL,
    active     INTEGER
);
"""


@dataclass
class Customer:
    customer_id: int
    name: str
    email: str | None
    region: str | None
    created_date: date
    active_from: date   # when the customer actually started ordering
    weight: float       # how often the customer orders, relative to the others


@dataclass
class Product:
    product_id: int
    name: str
    category: str
    price: float
    active: bool
    popularity: int


@dataclass
class Order:
    order_id: int
    customer_id: int
    order_date: date
    status: str | None


@dataclass
class Line:
    order_id: int
    product_id: int
    quantity: int
    unit_price: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def messy(rng: random.Random, value: str, share: float) -> str:
    """Returns the value, or with probability `share` a variant with other case or stray whitespace."""
    if rng.random() >= share:
        return value
    return rng.choice([value.lower(), value.upper(), f" {value}", f"{value} ", f"{value.lower()} "])


# ---------------------------------------------------------------------------
# Clean data
# ---------------------------------------------------------------------------

def build_products() -> list[Product]:
    return [Product(1001 + i, name, category, price, active, popularity)
            for i, (name, category, price, active, popularity) in enumerate(PRODUCTS)]


def build_customers(rng: random.Random) -> list[Customer]:
    region_names, region_weights = list(REGIONS), list(REGIONS.values())
    customers = []
    for customer_id in range(1, N_CUSTOMERS + 1):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        if rng.random() < 0.22:  # migrated from the old system
            active_from = random_date(rng, date(2023, 3, 1), date(2024, 8, 15))
            created_date = MIGRATION_DATE + timedelta(days=rng.randint(0, 14))
        else:
            active_from = random_date(rng, date(2023, 1, 1), date(2025, 5, 31))
            created_date = active_from
        customers.append(Customer(
            customer_id=customer_id,
            name=f"{first} {last}",
            email=f"{first}.{last}{customer_id}@example.com".lower(),
            region=rng.choices(region_names, region_weights)[0],
            created_date=created_date,
            active_from=active_from,
            weight=min(rng.paretovariate(1.5), 30.0),
        ))
    return customers


def build_orders(rng: random.Random, customers: list[Customer]) -> list[Order]:
    cum_weights = list(itertools.accumulate(c.weight for c in customers))
    drafts = []
    for _ in range(N_ORDERS):
        customer = rng.choices(customers, cum_weights=cum_weights)[0]
        start = max(FIRST_ORDER_DATE, customer.active_from)
        status = rng.choices(["COMPLETED", "CANCELLED", "REFUNDED"], [88, 7, 5])[0]
        drafts.append((random_date(rng, start, LAST_ORDER_DATE), customer.customer_id, status))
    drafts.sort()
    return [Order(100001 + i, customer_id, order_date, status)
            for i, (order_date, customer_id, status) in enumerate(drafts)]


def build_lines(rng: random.Random, orders: list[Order], products: list[Product]) -> list[Line]:
    lines = []
    for order in orders:
        available = [p for p in products if p.active or order.order_date <= DISCONTINUED_DATE]
        weights = [p.popularity for p in available]
        n_lines = rng.choices([1, 2, 3, 4], [50, 30, 14, 6])[0]
        chosen: list[Product] = []
        while len(chosen) < n_lines:
            product = rng.choices(available, weights)[0]
            if product not in chosen:
                chosen.append(product)
        for product in chosen:
            quantity = rng.choices([1, 2, 3], [70, 22, 8])[0] if product.category == "Coffee" else 1
            unit_price = product.price if rng.random() > 0.1 else round(product.price * 0.9, 2)
            lines.append(Line(order.order_id, product.product_id, quantity, unit_price))
    return lines


# ---------------------------------------------------------------------------
# Deliberate data quality issues
# ---------------------------------------------------------------------------

def add_data_quality_issues(rng: random.Random, customers: list[Customer],
                            orders: list[Order], lines: list[Line]) -> None:
    lines_by_order: dict[int, list[Line]] = {}
    for line in lines:
        lines_by_order.setdefault(line.order_id, []).append(line)

    # Credit notes on some completed orders, for a product that is already on the order.
    completed = [o for o in orders if o.status == "COMPLETED"]
    for order in rng.sample(completed, 120):
        original = rng.choice(lines_by_order[order.order_id])
        lines.append(Line(order.order_id, original.product_id, -1, original.unit_price))

    # Order lines registered twice.
    for line in rng.sample(lines, 15):
        lines.append(Line(line.order_id, line.product_id, line.quantity, line.unit_price))

    # Order lines for products that do not exist in the catalogue.
    for order in rng.sample(orders, 8):
        lines.append(Line(order.order_id, rng.choice(UNKNOWN_PRODUCT_IDS), 1, rng.choice([79.0, 149.0])))

    # Orders with a placeholder date, without a status, or for customers that do not exist in the CRM.
    affected = rng.sample(orders, 6 + 9 + 12)
    for order in affected[:6]:
        order.order_date = PLACEHOLDER_DATE
    for order in affected[6:15]:
        order.status = None
    for order in affected[15:]:
        order.customer_id = rng.randint(90001, 90060)

    # Customers registered twice: same name and region, the email with other case,
    # a later created_date, and one of the orders moved to the new ID.
    orders_by_customer: dict[int, list[Order]] = {}
    for order in orders:
        orders_by_customer.setdefault(order.customer_id, []).append(order)
    candidates = [c for c in customers if len(orders_by_customer.get(c.customer_id, [])) >= 2]
    for i, original in enumerate(rng.sample(candidates, N_DUPLICATE_CUSTOMERS)):
        duplicate = Customer(
            customer_id=N_CUSTOMERS + 1 + i,
            name=original.name,
            email=original.email.capitalize(),
            region=original.region,
            created_date=original.created_date + timedelta(days=rng.randint(30, 200)),
            active_from=original.active_from,
            weight=0.0,
        )
        customers.append(duplicate)
        rng.choice(orders_by_customer[original.customer_id]).customer_id = duplicate.customer_id

    # Missing email and region.
    originals = customers[:N_CUSTOMERS]
    for customer in rng.sample(originals, 18):
        customer.email = None
    for customer in rng.sample(originals, 11):
        customer.region = None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_database(path: Path, schema: str, tables: dict[str, list[tuple]]) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            con.executescript(schema)
            for table, rows in tables.items():
                placeholders = ", ".join("?" * len(rows[0]))
                con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    finally:
        con.close()


def main() -> None:
    rng = random.Random(SEED)
    products = build_products()
    customers = build_customers(rng)
    orders = build_orders(rng, customers)
    lines = build_lines(rng, orders, products)
    add_data_quality_issues(rng, customers, orders, lines)
    lines.sort(key=lambda line: line.order_id)  # stable: added lines end up after the original lines

    customer_rows = [(c.customer_id, c.name, c.email,
                      None if c.region is None else messy(rng, c.region, 0.12),
                      c.created_date.isoformat())
                     for c in customers]
    product_rows = [(p.product_id, p.name, messy(rng, p.category, 0.3), p.price, int(p.active))
                    for p in products]
    order_rows = [(o.order_id, o.customer_id, o.order_date.isoformat(), o.status) for o in orders]
    line_rows = [(line_id, l.order_id, l.product_id, l.quantity, l.unit_price)
                 for line_id, l in enumerate(lines, 1)]

    write_database(HERE / "crm.db", CRM_SCHEMA, {"customers": customer_rows})
    write_database(HERE / "shop.db", SHOP_SCHEMA, {"orders": order_rows, "order_lines": line_rows})
    write_database(HERE / "inventory.db", INVENTORY_SCHEMA, {"products": product_rows})

    print(f"crm.db:       {len(customer_rows)} customers")
    print(f"shop.db:      {len(order_rows)} orders, {len(line_rows)} order lines")
    print(f"inventory.db: {len(product_rows)} products")


if __name__ == "__main__":
    main()