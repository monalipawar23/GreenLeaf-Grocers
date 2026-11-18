"""GreenLeaf Grocers - billing & inventory app.

Drop-in replacement for the original app.py: same routes, same template variables,
same database schema (database.py and templates/ stay untouched).
"""
import csv
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from io import BytesIO, StringIO

import pandas as pd
import plotly.express as px
from flask import (Flask, flash, make_response, redirect, render_template, request,
                   send_file, session, url_for)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash

from database import get_db, init_db

# --------------------------------------------------------------------------- config
STORE_NAME = os.environ.get("STORE_NAME", "GreenLeaf Grocers")
EXPIRY_WARN_DAYS = 15
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

app = Flask(__name__)
import os

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "greenleaf-local-development-key"
)
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)  # sessions reset on restart; set SECRET_KEY in production
    logging.warning("SECRET_KEY not set - using a temporary key. Set SECRET_KEY before deploying.")
app.config.update(
    SECRET_KEY=_secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # 2 MB upload limit
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = app.logger
init_db()


# --------------------------------------------------------------------------- helpers
def db():
    conn = get_db()
    conn.execute("PRAGMA foreign_keys = ON")  # make FK rules (e.g. products used in sales) actually apply
    return conn


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def safe_cell(value):
    """Stop spreadsheet formula injection in CSV exports."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def default_range():
    today = date.today()
    return (today - timedelta(days=30)).isoformat(), today.isoformat()


def read_range():
    start, end = default_range()
    start, end = request.args.get("start", start), request.args.get("end", end)
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end:
            raise ValueError
        return start, end, True
    except ValueError:
        return (*default_range(), False)


def is_safe_next(target):
    return bool(target) and target.startswith("/") and not target.startswith("//")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access is required.", "danger")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapped


_failed_logins = defaultdict(list)  # (username, ip) -> timestamps; per-process throttle


def _recent_failures(key):
    now = time.time()
    _failed_logins[key] = [t for t in _failed_logins[key] if now - t < LOCKOUT_SECONDS]
    return len(_failed_logins[key])


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def _error_page(code, title, message):
    return make_response(
        f"<!doctype html><title>{title}</title><body style='font-family:system-ui;max-width:32rem;"
        f"margin:15vh auto;padding:0 1rem'><h1>{title}</h1><p>{message}</p>"
        f"<p><a href='/'>Back to the app</a></p></body>", code)


@app.errorhandler(404)
def not_found(_e):
    return _error_page(404, "Page not found", "That page doesn't exist or was moved.")


@app.errorhandler(413)
def too_large(_e):
    return _error_page(413, "File too large", "Uploads are limited to 2 MB.")


@app.errorhandler(500)
def server_error(_e):
    return _error_page(500, "Something went wrong", "The error was logged. Try again or contact the administrator.")


@app.get("/healthz")
def healthz():
    with closing(db()) as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# --------------------------------------------------------------------------- auth
@app.route("/")
def home():
    return redirect(url_for("dashboard") if session.get("user_id") else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = (username.lower(), request.remote_addr)
        if _recent_failures(key) >= MAX_LOGIN_ATTEMPTS:
            flash(f"Too many failed attempts. Try again in {LOCKOUT_SECONDS // 60} minutes.", "danger")
            log.warning("Login locked out for %s from %s", username, request.remote_addr)
            return render_template("login.html"), 429
        with closing(db()) as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            _failed_logins.pop(key, None)
            session.clear()  # new session on login prevents session fixation
            session.permanent = True
            session.update(user_id=user["user_id"], username=user["username"], role=user["role"])
            log.info("Login: %s", username)
            nxt = request.args.get("next", "")
            return redirect(nxt if is_safe_next(nxt) else url_for("dashboard"))
        _failed_logins[key].append(time.time())
        log.warning("Failed login for %s from %s", username, request.remote_addr)
        flash("Incorrect username or password.", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- dashboard
@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today().isoformat()
    with closing(db()) as conn:
        revenue = conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM sales WHERE sale_date=?", (today,)).fetchone()[0]
        bills = conn.execute("SELECT COUNT(*) FROM sales WHERE sale_date=?", (today,)).fetchone()[0]
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        low = conn.execute("SELECT COUNT(*) FROM products WHERE stock_qty <= reorder_level").fetchone()[0]
        recent = conn.execute("""SELECT s.*, u.username FROM sales s JOIN users u ON s.user_id=u.user_id
                                 ORDER BY sale_id DESC LIMIT 8""").fetchall()
        low_items = conn.execute("SELECT * FROM products WHERE stock_qty <= reorder_level "
                                 "ORDER BY stock_qty LIMIT 6").fetchall()
    return render_template("dashboard.html", revenue=revenue, bills=bills, products=products,
                           low=low, recent=recent, low_items=low_items)


# --------------------------------------------------------------------------- products
def clean_product(data):
    """Validate product fields from a form or CSV row. Raises ValueError with a readable reason."""
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    if not name or not category:
        raise ValueError("name and category are required")
    try:
        price = float(data["unit_price"])
        qty = int(data["stock_qty"])
        reorder = int(data.get("reorder_level") or 5)
    except (KeyError, TypeError, ValueError):
        raise ValueError("price, stock and reorder level must be numbers")
    if price < 0 or qty < 0 or reorder < 0:
        raise ValueError("price, stock and reorder level cannot be negative")
    expiry = (data.get("expiry_date") or "").strip() or None
    if expiry:
        try:
            date.fromisoformat(expiry)
        except ValueError:
            raise ValueError("expiry date must be YYYY-MM-DD")
    return name, category, price, qty, reorder, expiry


@app.route("/products", methods=["GET", "POST"])
@admin_required
def products():
    if request.method == "POST":
        with closing(db()) as conn:
            try:
                name, category, price, qty, reorder, expiry = clean_product(request.form)
                if request.form.get("action") == "update":
                    cur = conn.execute("""UPDATE products SET name=?,category=?,unit_price=?,stock_qty=?,
                                          reorder_level=?,expiry_date=? WHERE product_id=?""",
                                       (name, category, price, qty, reorder, expiry, int(request.form["product_id"])))
                    if cur.rowcount == 0:
                        raise ValueError("product not found")
                else:
                    conn.execute("""INSERT INTO products(name,category,unit_price,stock_qty,reorder_level,expiry_date)
                                    VALUES(?,?,?,?,?,?)""", (name, category, price, qty, reorder, expiry))
                conn.commit()
                log.info("Product saved by %s: %s", session["username"], name)
                flash("Product saved.", "success")
            except (ValueError, KeyError) as e:
                conn.rollback()
                flash(f"Couldn't save the product: {e}. Price, stock and reorder level cannot be negative.", "danger")
        return redirect(url_for("products"))
    search = request.args.get("q", "").strip()
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM products WHERE name LIKE ? OR category LIKE ? ORDER BY name",
                            (f"%{search}%", f"%{search}%")).fetchall()
    return render_template("products.html", products=rows, search=search)


@app.post("/products/delete/<int:pid>")
@admin_required
def delete_product(pid):
    with closing(db()) as conn:
        if conn.execute("SELECT 1 FROM sale_items WHERE product_id=? LIMIT 1", (pid,)).fetchone():
            flash("Cannot delete a product already used in a sale. Set its stock to 0 instead.", "danger")
        else:
            conn.execute("DELETE FROM products WHERE product_id=?", (pid,))
            conn.commit()
            log.info("Product %s deleted by %s", pid, session["username"])
            flash("Product deleted.", "success")
    return redirect(url_for("products"))


@app.route("/products/import", methods=["POST"])
@admin_required
def import_products():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".csv"):
        flash("Please select a CSV file.", "danger")
        return redirect(url_for("products"))
    errors, rows = [], []
    try:
        reader = csv.DictReader(StringIO(upload.stream.read().decode("utf-8-sig")))
        required = {"name", "category", "unit_price", "stock_qty"}
        if not reader.fieldnames or not required <= set(reader.fieldnames):
            raise ValueError("missing columns. Required: name, category, unit_price, stock_qty "
                             "(optional: reorder_level, expiry_date)")
        for line, row in enumerate(reader, start=2):
            try:
                rows.append(clean_product(row))
            except ValueError as e:
                errors.append(f"row {line}: {e}")
    except (ValueError, UnicodeDecodeError, csv.Error) as e:
        flash(f"CSV import failed: {e}", "danger")
        return redirect(url_for("products"))
    if errors:  # all-or-nothing: never half-import a file
        more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
        flash("Nothing was imported. " + "; ".join(errors[:5]) + more, "danger")
        return redirect(url_for("products"))
    with closing(db()) as conn:
        try:
            conn.executemany("""INSERT INTO products(name,category,unit_price,stock_qty,reorder_level,expiry_date)
                                VALUES(?,?,?,?,?,?)""", rows)
            conn.commit()
            log.info("%s imported %d products", session["username"], len(rows))
            flash(f"Imported {len(rows)} products.", "success")
        except Exception:
            conn.rollback()
            log.exception("CSV import failed")
            flash("CSV import failed while saving. Nothing was imported.", "danger")
    return redirect(url_for("products"))


# --------------------------------------------------------------------------- billing
class BillingError(Exception):
    pass


def create_sale(conn, form):
    ids, quantities = form.getlist("product_id[]"), form.getlist("quantity[]")
    if not ids or len(ids) != len(quantities):
        raise BillingError("Add at least one item to the bill.")
    wanted = {}
    for pid_text, qty_text in zip(ids, quantities):
        pid, qty = int(pid_text), int(qty_text)
        if qty <= 0:
            raise BillingError("Quantity must be at least 1.")
        wanted[pid] = wanted.get(pid, 0) + qty  # same product added twice -> one line, one stock check
    discount_pct = Decimal(form.get("discount", "0") or "0")
    if not Decimal(0) <= discount_pct <= Decimal(100):
        raise BillingError("Discount must be between 0 and 100.")

    conn.execute("BEGIN IMMEDIATE")  # lock before reading stock so two bills can't oversell
    today = date.today().isoformat()
    lines, subtotal = [], Decimal(0)
    for pid, qty in wanted.items():
        product = conn.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
        if not product:
            raise BillingError("A selected product no longer exists.")
        if product["expiry_date"] and product["expiry_date"] < today:
            raise BillingError(f"{product['name']} expired on {product['expiry_date']} and can't be sold.")
        if product["stock_qty"] < qty:
            raise BillingError(f"Insufficient stock for {product['name']} (available: {product['stock_qty']}).")
        lines.append((product, qty))
        subtotal += money(product["unit_price"]) * qty
    discount = money(subtotal * discount_pct / 100)
    total = subtotal - discount

    cur = conn.execute("""INSERT INTO sales(sale_date,sale_time,user_id,subtotal,discount,total_amount)
                          VALUES(?,?,?,?,?,?)""",
                       (today, datetime.now().strftime("%H:%M:%S"), session["user_id"],
                        float(subtotal), float(discount), float(total)))
    sale_id = cur.lastrowid
    for product, qty in lines:
        conn.execute("INSERT INTO sale_items(sale_id,product_id,quantity,price) VALUES(?,?,?,?)",
                     (sale_id, product["product_id"], qty, product["unit_price"]))
        updated = conn.execute("UPDATE products SET stock_qty=stock_qty-? WHERE product_id=? AND stock_qty>=?",
                               (qty, product["product_id"], qty))
        if updated.rowcount == 0:
            raise BillingError(f"Insufficient stock for {product['name']}.")
    conn.commit()
    log.info("Bill #%s by %s total=%s", sale_id, session["username"], total)
    return sale_id


@app.route("/billing", methods=["GET", "POST"])
@login_required
def billing():
    with closing(db()) as conn:
        if request.method == "POST":
            try:
                return redirect(url_for("bill_pdf", sale_id=create_sale(conn, request.form)))
            except BillingError as e:
                conn.rollback()
                flash(str(e), "danger")
            except (ValueError, TypeError, KeyError, InvalidOperation):
                conn.rollback()
                flash("Invalid billing details.", "danger")
        products_list = conn.execute("SELECT * FROM products WHERE stock_qty > 0 AND "
                                     "(expiry_date IS NULL OR expiry_date >= ?) ORDER BY name",
                                     (date.today().isoformat(),)).fetchall()
    return render_template("billing.html", products=products_list)


def build_bill_pdf(sale, items):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"Bill #{sale['sale_id']}")
    base = getSampleStyleSheet()["Normal"]
    green = colors.HexColor("#1f4d3a")
    title = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=20, textColor=green, leading=24)
    small = ParagraphStyle("s", parent=base, fontSize=9, textColor=colors.HexColor("#555555"))
    story = [Paragraph(STORE_NAME, title),
             Paragraph(f"Bill #{sale['sale_id']} &nbsp;|&nbsp; {sale['sale_date']} {sale['sale_time']} "
                       f"&nbsp;|&nbsp; Cashier: {sale['username']}", small), Spacer(1, 8 * mm)]
    rows = [["#", "Product", "Qty", "Price", "Amount"]]
    for i, it in enumerate(items, 1):
        rows.append([i, it["name"][:45], it["quantity"], f"{it['price']:.2f}", f"{it['line_total']:.2f}"])
    table = Table(rows, repeatRows=1, colWidths=[10 * mm, 90 * mm, 18 * mm, 28 * mm, 28 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), green), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f2")]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, green),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    totals = Table([["Subtotal", f"{sale['subtotal']:.2f}"], ["Discount", f"- {sale['discount']:.2f}"],
                    ["Total", f"{sale['total_amount']:.2f}"]], colWidths=[130 * mm, 44 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"), ("FONTSIZE", (0, 2), (-1, 2), 12),
                                ("LINEABOVE", (0, 2), (-1, 2), 0.6, green)]))
    story += [table, Spacer(1, 5 * mm), totals, Spacer(1, 10 * mm),
              Paragraph("Thank you for shopping with us!", small)]
    doc.build(story)
    buffer.seek(0)
    return buffer


@app.route("/bill/<int:sale_id>.pdf")
@login_required
def bill_pdf(sale_id):
    with closing(db()) as conn:
        sale = conn.execute("""SELECT s.*, u.username FROM sales s JOIN users u ON s.user_id=u.user_id
                               WHERE sale_id=?""", (sale_id,)).fetchone()
        items = conn.execute("""SELECT p.name, si.quantity, si.price, si.quantity*si.price AS line_total
                                FROM sale_items si JOIN products p ON p.product_id=si.product_id
                                WHERE si.sale_id=?""", (sale_id,)).fetchall() if sale else []
    # cashiers may only open their own bills
    if not sale or (session.get("role") != "admin" and sale["user_id"] != session["user_id"]):
        flash("Bill not found.", "danger")
        return redirect(url_for("billing"))
    return send_file(build_bill_pdf(sale, items), as_attachment=True,
                     download_name=f"greenleaf_bill_{sale_id}.pdf", mimetype="application/pdf")


# --------------------------------------------------------------------------- reports
@app.route("/reports")
@admin_required
def reports():
    start, end, valid = read_range()
    if not valid:
        flash("Choose a valid date range.", "danger")
    today = date.today()
    window = (start, end)
    with closing(db()) as conn:
        sales = pd.read_sql_query("SELECT * FROM sales WHERE sale_date BETWEEN ? AND ?", conn, params=window)
        top = pd.read_sql_query("""SELECT p.name, SUM(si.quantity) AS quantity, SUM(si.quantity*si.price) AS revenue
                                   FROM sale_items si JOIN products p ON p.product_id=si.product_id
                                   JOIN sales s ON s.sale_id=si.sale_id WHERE s.sale_date BETWEEN ? AND ?
                                   GROUP BY p.product_id ORDER BY quantity DESC LIMIT 10""", conn, params=window)
        categories = pd.read_sql_query("""SELECT p.category, SUM(si.quantity*si.price) AS revenue
                                          FROM sale_items si JOIN products p ON p.product_id=si.product_id
                                          JOIN sales s ON s.sale_id=si.sale_id WHERE s.sale_date BETWEEN ? AND ?
                                          GROUP BY p.category ORDER BY revenue DESC""", conn, params=window)
        low = conn.execute("SELECT * FROM products WHERE stock_qty <= reorder_level ORDER BY stock_qty").fetchall()
        expiry = conn.execute("""SELECT * FROM products WHERE expiry_date IS NOT NULL
                                 AND expiry_date BETWEEN ? AND ? ORDER BY expiry_date""",
                              (today.isoformat(), (today + timedelta(days=EXPIRY_WARN_DAYS)).isoformat())).fetchall()
        # fixed: the original counted all-time sales; this uses the last 30 days only
        slow = conn.execute("""SELECT p.* FROM products p WHERE NOT EXISTS (
                                   SELECT 1 FROM sale_items si JOIN sales s ON s.sale_id=si.sale_id
                                   WHERE si.product_id=p.product_id AND s.sale_date >= ?)
                               ORDER BY p.stock_qty DESC""", ((today - timedelta(days=30)).isoformat(),)).fetchall()
    revenue = float(sales["total_amount"].sum()) if not sales.empty else 0
    bill_count = len(sales)
    avg = revenue / bill_count if bill_count else 0
    daily = (sales.groupby("sale_date", as_index=False)["total_amount"].sum()
             if not sales.empty else pd.DataFrame(columns=["sale_date", "total_amount"]))
    daily_chart = px.line(daily, x="sale_date", y="total_amount", markers=True,
                          title="Daily Revenue").to_html(full_html=False, include_plotlyjs="cdn")
    category_chart = (px.pie(categories, names="category", values="revenue", title="Category-wise Revenue")
                      .to_html(full_html=False, include_plotlyjs=False) if not categories.empty else "")
    return render_template("reports.html", start=start, end=end, revenue=revenue, bills=bill_count, avg=avg,
                           top=top.to_dict("records"), low=low, expiry=expiry, slow=slow,
                           daily_chart=daily_chart, category_chart=category_chart)


@app.route("/reports/export.csv")
@admin_required
def export_csv():
    start, end, valid = read_range()
    if not valid:
        flash("Choose a valid date range.", "danger")
        return redirect(url_for("reports"))
    with closing(db()) as conn:
        rows = conn.execute("""SELECT s.sale_id,s.sale_date,s.sale_time,u.username,s.subtotal,s.discount,s.total_amount
                               FROM sales s JOIN users u ON s.user_id=u.user_id
                               WHERE s.sale_date BETWEEN ? AND ? ORDER BY s.sale_date, s.sale_id""",
                            (start, end)).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["sale_id", "sale_date", "sale_time", "cashier", "subtotal", "discount", "total_amount"])
    writer.writerows([(r[0], r[1], r[2], safe_cell(r[3]), r[4], r[5], r[6]) for r in rows])
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=greenleaf_sales_{start}_to_{end}.csv"
    response.headers["Content-Type"] = "text/csv"
    return response


if __name__ == "__main__":
    import threading
    import webbrowser

    def open_browser():
        webbrowser.open_new("http://127.0.0.1:5000")

    threading.Timer(1.5, open_browser).start()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False
    )
