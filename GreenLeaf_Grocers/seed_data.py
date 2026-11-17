"""Generate demo data: 60 products and 600+ sale records across six months."""
import random
from datetime import date, timedelta, datetime
from werkzeug.security import generate_password_hash
from database import get_db, init_db

random.seed(42)
NAMES = [
    ("Apple", "Fruits", 120), ("Banana", "Fruits", 55), ("Orange", "Fruits", 90),
    ("Tomato", "Vegetables", 40), ("Potato", "Vegetables", 35), ("Onion", "Vegetables", 45),
    ("Spinach", "Vegetables", 30), ("Carrot", "Vegetables", 50), ("Milk", "Dairy", 62),
    ("Cheese", "Dairy", 180), ("Yogurt", "Dairy", 45), ("Butter", "Dairy", 95),
    ("Rice", "Grains", 75), ("Wheat Flour", "Grains", 48), ("Oats", "Grains", 110),
    ("Bread", "Bakery", 40), ("Cookies", "Bakery", 65), ("Tea", "Beverages", 150),
    ("Coffee", "Beverages", 220), ("Juice", "Beverages", 85)
]

def seed(db_path=None):
    init_db(db_path)
    conn = get_db(db_path)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0:
        conn.close()
        return
    conn.execute("INSERT INTO suppliers(name,phone,email) VALUES('Fresh Farms','9876543210','fresh@example.com')")
    conn.execute("INSERT INTO suppliers(name,phone,email) VALUES('Daily Supplies','9876501234','daily@example.com')")
    for i in range(60):
        name, category, price = NAMES[i % len(NAMES)]
        suffix = f" {i//len(NAMES)+1}" if i >= len(NAMES) else ""
        expiry = date.today() + timedelta(days=random.randint(-5, 120))
        conn.execute("""INSERT INTO products(name,category,unit_price,stock_qty,reorder_level,expiry_date,supplier_id)
                        VALUES(?,?,?,?,?,?,?)""",
                     (name + suffix, category, round(price * random.uniform(.85, 1.2), 2),
                      random.randint(8, 100), random.randint(5, 15), expiry.isoformat(), 1 if i%2==0 else 2))
    admin = conn.execute("SELECT user_id FROM users WHERE username='admin'").fetchone()[0]
    cashier = conn.execute("SELECT user_id FROM users WHERE username='cashier'").fetchone()[0]
    product_ids = [r[0] for r in conn.execute("SELECT product_id FROM products").fetchall()]
    start = date.today() - timedelta(days=180)
    for _ in range(650):
        day = start + timedelta(days=random.randint(0, 180))
        uid = random.choice([admin, cashier])
        chosen = random.sample(product_ids, random.randint(1, 4))
        sale_rows, subtotal = [], 0
        for pid in chosen:
            product = conn.execute("SELECT unit_price FROM products WHERE product_id=?", (pid,)).fetchone()
            qty = random.randint(1, 4)
            price = float(product[0])
            sale_rows.append((pid, qty, price))
            subtotal += qty * price
        discount = round(subtotal * random.choice([0, 0, 0, .05, .10]), 2)
        cur = conn.execute("""INSERT INTO sales(sale_date,sale_time,user_id,subtotal,discount,total_amount)
                              VALUES(?,?,?,?,?,?)""",
                           (day.isoformat(), f"{random.randint(8,21):02d}:{random.randint(0,59):02d}:00",
                            uid, round(subtotal,2), discount, round(subtotal-discount,2)))
        for pid, qty, price in sale_rows:
            conn.execute("INSERT INTO sale_items(sale_id,product_id,quantity,price) VALUES(?,?,?,?)",
                         (cur.lastrowid, pid, qty, price))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    seed()
    print("Demo data created: 60 products and 650 sales.")
