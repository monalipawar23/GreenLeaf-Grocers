import pytest
from app import app as flask_app
from database import init_db, get_db

@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    import app as app_module
    original = app_module.db
    app_module.db = lambda: get_db(db_path)
    flask_app.config.update(TESTING=True, SECRET_KEY="test")
    with flask_app.test_client() as client:
        yield client, db_path
    app_module.db = original

def login(client, username="admin", password="admin123"):
    return client.post("/login", data={"username":username,"password":password}, follow_redirects=True)

def test_login_success(client):
    c,_=client
    response=login(c)
    assert b"Store dashboard" in response.data

def test_login_failure(client):
    c,_=client
    response=c.post("/login",data={"username":"admin","password":"wrong"})
    assert b"Incorrect username" in response.data

def test_dashboard_requires_login(client):
    c,_=client
    response=c.get("/dashboard")
    assert response.status_code == 302

def test_cashier_cannot_open_products(client):
    c,_=client
    login(c,"cashier","cashier123")
    assert c.get("/products").status_code == 302

def test_admin_can_open_products(client):
    c,_=client
    login(c)
    assert c.get("/products").status_code == 200

def test_add_product(client):
    c,path=client
    login(c)
    c.post("/products",data={"action":"add","name":"Test Apple","category":"Fruit","unit_price":"10","stock_qty":"20","reorder_level":"5"})
    conn=get_db(path)
    assert conn.execute("SELECT COUNT(*) FROM products WHERE name='Test Apple'").fetchone()[0] == 1
    conn.close()

def test_reject_negative_product_price(client):
    c,path=client
    login(c)
    c.post("/products",data={"action":"add","name":"Bad","category":"X","unit_price":"-1","stock_qty":"2","reorder_level":"1"})
    conn=get_db(path)
    assert conn.execute("SELECT COUNT(*) FROM products WHERE name='Bad'").fetchone()[0] == 0
    conn.close()

def test_billing_deducts_stock(client):
    c,path=client
    login(c)
    conn=get_db(path)
    conn.execute("INSERT INTO products(name,category,unit_price,stock_qty,reorder_level) VALUES('Banana','Fruit',5,10,2)")
    pid=conn.execute("SELECT product_id FROM products WHERE name='Banana'").fetchone()[0]
    conn.commit(); conn.close()
    response=c.post("/billing",data={"product_id[]":[str(pid)],"quantity[]":["3"],"discount":"0"})
    assert response.status_code == 302
    conn=get_db(path)
    assert conn.execute("SELECT stock_qty FROM products WHERE product_id=?",(pid,)).fetchone()[0] == 7
    conn.close()

def test_billing_rejects_insufficient_stock(client):
    c,path=client
    login(c)
    conn=get_db(path)
    conn.execute("INSERT INTO products(name,category,unit_price,stock_qty,reorder_level) VALUES('Pear','Fruit',5,1,2)")
    pid=conn.execute("SELECT product_id FROM products WHERE name='Pear'").fetchone()[0]
    conn.commit(); conn.close()
    response=c.post("/billing",data={"product_id[]":[str(pid)],"quantity[]":["5"],"discount":"0"})
    assert b"Insufficient stock" in response.data
    conn=get_db(path)
    assert conn.execute("SELECT stock_qty FROM products WHERE product_id=?",(pid,)).fetchone()[0] == 1
    conn.close()

def test_report_admin_only(client):
    c,_=client
    login(c)
    assert c.get("/reports").status_code == 200
