"""
build_db.py
Creates demo.db — a small e-commerce SQLite database with:
  customers, products, orders, order_items
Populates each table with 20-30 realistic sample rows.
Run once: python build_db.py
"""

import sqlite3
import random
from datetime import date, timedelta

DB_PATH = "demo.db"


def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA foreign_keys = ON;

        DROP TABLE IF EXISTS order_items;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS customers;

        CREATE TABLE customers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            city          TEXT NOT NULL,
            signup_date   TEXT NOT NULL
        );

        CREATE TABLE products (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            category      TEXT NOT NULL,
            price         REAL NOT NULL
        );

        CREATE TABLE orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id   INTEGER NOT NULL,
            order_date    TEXT NOT NULL,
            status        TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE order_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id      INTEGER NOT NULL,
            product_id    INTEGER NOT NULL,
            quantity      INTEGER NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
        """
    )
    conn.commit()


def populate(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rng = random.Random(42)  # deterministic sample data

    cities = [
        "New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
        "Philadelphia", "San Antonio", "San Diego", "Dallas", "Austin",
        "San Francisco", "Seattle", "Denver", "Boston", "Miami",
    ]
    first_names = [
        "Alice", "Bob", "Carlos", "Diana", "Ethan", "Fiona", "George",
        "Hannah", "Ivan", "Julia", "Kevin", "Laura", "Marcus", "Nina",
        "Oscar", "Priya", "Quinn", "Rosa", "Sam", "Tara", "Umar",
        "Vera", "Will", "Xena", "Yusuf", "Zoe", "Amir", "Bella",
    ]
    last_names = [
        "Smith", "Johnson", "Garcia", "Chen", "Patel", "Nguyen", "Kim",
        "Brown", "Davis", "Lopez", "Wilson", "Anderson", "Clark",
        "Rodriguez", "Lewis", "Walker", "Young", "King", "Wright", "Hill",
    ]

    # ---------- customers (28 rows) ----------
    customers = []
    used_emails = set()
    for i in range(28):
        fn = rng.choice(first_names)
        ln = rng.choice(last_names)
        name = f"{fn} {ln}"
        email = f"{fn.lower()}.{ln.lower()}{i}@example.com"
        while email in used_emails:
            email = f"{fn.lower()}.{ln.lower()}{i}{rng.randint(1,999)}@example.com"
        used_emails.add(email)
        city = rng.choice(cities)
        signup = date(2023, 1, 1) + timedelta(days=rng.randint(0, 700))
        customers.append((name, email, city, signup.isoformat()))

    # Force a guaranteed few "New York" customers so test question #1 is meaningful
    customers[0] = ("Alice Smith", "alice.smith.ny@example.com", "New York", "2023-02-14")
    customers[1] = ("Marcus Lewis", "marcus.lewis.ny@example.com", "New York", "2023-05-01")
    customers[2] = ("Priya Patel", "priya.patel.ny@example.com", "New York", "2023-08-19")

    cur.executemany(
        "INSERT INTO customers (name, email, city, signup_date) VALUES (?, ?, ?, ?)",
        customers,
    )

    # ---------- products (24 rows) ----------
    products = [
        ("Wireless Mouse", "Electronics", 19.99),
        ("Mechanical Keyboard", "Electronics", 74.50),
        ("USB-C Hub", "Electronics", 29.99),
        ("27in 4K Monitor", "Electronics", 329.00),
        ("Noise Cancelling Headphones", "Electronics", 199.99),
        ("Bluetooth Speaker", "Electronics", 45.00),
        ("Smartwatch", "Electronics", 149.99),
        ("Webcam 1080p", "Electronics", 39.99),
        ("Portable SSD 1TB", "Electronics", 89.99),
        ("Phone Charger Cable", "Electronics", 9.99),
        ("Yoga Mat", "Fitness", 24.99),
        ("Adjustable Dumbbells", "Fitness", 129.00),
        ("Resistance Bands Set", "Fitness", 15.99),
        ("Foam Roller", "Fitness", 21.50),
        ("Running Shoes", "Fitness", 89.00),
        ("Stainless Water Bottle", "Fitness", 18.00),
        ("Coffee Maker", "Home", 59.99),
        ("Non-stick Pan Set", "Home", 45.99),
        ("Cotton Bed Sheets", "Home", 39.00),
        ("Vacuum Cleaner", "Home", 159.99),
        ("Desk Lamp", "Home", 22.99),
        ("Ceramic Mug Set", "Home", 16.50),
        ("Air Purifier", "Home", 119.00),
        ("Throw Blanket", "Home", 27.99),
    ]
    cur.executemany(
        "INSERT INTO products (name, category, price) VALUES (?, ?, ?)",
        products,
    )

    # ---------- orders (30 rows) ----------
    statuses = ["completed", "shipped", "processing", "cancelled"]
    n_customers = len(customers)
    orders = []
    for i in range(30):
        cust_id = rng.randint(1, n_customers)
        order_date = date(2023, 3, 1) + timedelta(days=rng.randint(0, 500))
        status = rng.choices(statuses, weights=[0.55, 0.2, 0.15, 0.10])[0]
        orders.append((cust_id, order_date.isoformat(), status))

    # Guarantee customer id 3 (Priya Patel) has a couple of orders for test question #2
    orders[0] = (3, "2023-06-10", "completed")
    orders[1] = (3, "2023-09-22", "shipped")

    cur.executemany(
        "INSERT INTO orders (customer_id, order_date, status) VALUES (?, ?, ?)",
        orders,
    )

    # ---------- order_items (~45 rows) ----------
    n_orders = len(orders)
    n_products = len(products)
    order_items = []

    # Make sure order #1 and #2 (customer 3's orders) have known items
    order_items.append((1, 5, 1))   # Noise Cancelling Headphones
    order_items.append((1, 10, 2))  # Phone Charger Cable x2
    order_items.append((2, 12, 1))  # Adjustable Dumbbells

    for i in range(1, n_orders + 1):
        if i in (1, 2):
            continue  # already added above
        n_items = rng.randint(1, 3)
        for _ in range(n_items):
            product_id = rng.randint(1, n_products)
            quantity = rng.randint(1, 4)
            order_items.append((i, product_id, quantity))

    cur.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)",
        order_items,
    )

    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)
    populate(conn)

    # quick sanity check
    cur = conn.cursor()
    for table in ("customers", "products", "orders", "order_items"):
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"{table}: {cur.fetchone()[0]} rows")

    conn.close()
    print(f"\n✅ {DB_PATH} created successfully.")


if __name__ == "__main__":
    main()
