# GreenLeaf Grocers — Inventory & Sales Management System

A Flask + SQLite retail management application with role-based login, product inventory, multi-item billing, automatic stock deduction, low-stock and expiry alerts, sales analytics, CSV import/export, and PDF bills.

## Requirements
- Python 3.10+
- pip

## Setup (Windows PowerShell)
```powershell
cd GreenLeaf_Grocers
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python seed_data.py
python app.py
```
Open http://127.0.0.1:5000

For macOS/Linux:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python seed_data.py
python app.py
```

## Demo accounts
| Role | Username | Password |
|---|---|---|
| Admin | `admin` | `admin123` |
| Cashier | `cashier` | `cashier123` |

Change the demo passwords and Flask `secret_key` before any real deployment.

## Features
- Admin/cashier authentication with Werkzeug password hashing
- Admin product CRUD and search
- CSV product import (columns: `name,category,unit_price,stock_qty,reorder_level,expiry_date`)
- Multi-item checkout with percentage discount and stock validation
- Automatic stock deduction in SQLite
- PDF bill download
- Daily dashboard metrics and low-stock reminders
- Date-filtered revenue, average bill value, daily trend, category revenue share and top products
- Low-stock, expiry-within-15-days and no-sales-in-30-days reports
- Sales report CSV export

## Demo data
`python seed_data.py` creates 60 products and 650 synthetic sales spanning roughly six months. It is safe to run more than once; it does not duplicate data when products already exist. To reset, stop the app and delete `data/inventory.db`, then rerun the seed script.

## Tests
```bash
pytest -q
```
Tests use a temporary SQLite database.

## Database schema
- `users`: login credentials and role
- `suppliers`: supplier contact details
- `products`: category, price, stock, reorder threshold, expiry
- `sales`: bill header, cashier, totals and discount
- `sale_items`: bill line items and quantity/price

Relationships: one user creates many sales; one sale contains many sale_items; one product can appear in many sale_items; one supplier can supply many products.

## Notes / extension ideas
- Add CSRF protection, audit logs, pagination and stricter permission checks before production use.
- Stock turnover can be added as cost of goods sold divided by average inventory value when purchase-cost data is available.
- Sales forecasting and email alerts are optional future enhancements.
