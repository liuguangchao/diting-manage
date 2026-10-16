"""
外贸履约系统 v1.0 — 后端服务
Python Flask + SQLite

启动: python3 server.py
访问: http://localhost:5000
"""
import sqlite3, json, os, uuid, time, mimetypes
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
DB_PATH = os.path.join(os.path.dirname(__file__), "fulfillment.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════
#  DB HELPERS
# ═══════════════════════════════════════════════════════════
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            po_number TEXT NOT NULL,
            buyer_id TEXT,
            currency TEXT DEFAULT 'USD',
            incoterms TEXT,
            incoterms_place TEXT,
            payment_method TEXT,
            total_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'confirmed',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS order_lines (
            line_id TEXT PRIMARY KEY,
            order_id TEXT REFERENCES orders(order_id) ON DELETE CASCADE,
            sku TEXT, product_name TEXT, description TEXT,
            quantity INTEGER DEFAULT 0, shipped_qty INTEGER DEFAULT 0,
            unit_price REAL DEFAULT 0, hs_code TEXT,
            status TEXT DEFAULT 'draft'
        );
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            invoice_no TEXT, order_id TEXT REFERENCES orders(order_id),
            batch_no TEXT, inv_date TEXT, currency TEXT,
            total_amount REAL DEFAULT 0, total_qty INTEGER DEFAULT 0,
            cartons INTEGER DEFAULT 0,
            lines_json TEXT, packs_json TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS shipments (
            shipment_id TEXT PRIMARY KEY,
            order_id TEXT REFERENCES orders(order_id),
            shipment_no TEXT, ship_date TEXT,
            carrier TEXT, tracking_no TEXT,
            port_loading TEXT, port_discharge TEXT,
            container_no TEXT, seal_no TEXT,
            status TEXT DEFAULT 'booked',
            lines_json TEXT, nodes_json TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            order_id TEXT REFERENCES orders(order_id),
            amount REAL DEFAULT 0, currency TEXT,
            method TEXT, received_date TEXT,
            bank_reference TEXT, note TEXT,
            allocated_json TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS costs (
            order_id TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL DEFAULT 0, note TEXT,
            currency TEXT DEFAULT 'CNY',
            fx_rate REAL DEFAULT NULL,
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (order_id, category)
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
            currency TEXT PRIMARY KEY,
            rate REAL DEFAULT 1.0
        );
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            customer_code TEXT,
            company_name TEXT NOT NULL,
            company_name_cn TEXT,
            country TEXT,
            address_line1 TEXT,
            address_line2 TEXT,
            city TEXT,
            state_province TEXT,
            postal_code TEXT,
            tax_id TEXT,
            website TEXT,
            customer_type TEXT,
            payment_terms TEXT,
            default_incoterms TEXT,
            default_currency TEXT,
            credit_limit REAL,
            status TEXT DEFAULT 'active',
            note TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS customer_contacts (
            contact_id TEXT PRIMARY KEY,
            customer_id TEXT REFERENCES customers(customer_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            title TEXT,
            email TEXT,
            phone TEXT,
            whatsapp TEXT,
            is_primary INTEGER DEFAULT 0,
            role TEXT
        );
        CREATE TABLE IF NOT EXISTS customer_documents (
            doc_id TEXT PRIMARY KEY,
            customer_id TEXT REFERENCES customers(customer_id) ON DELETE CASCADE,
            doc_type TEXT,
            file_name TEXT,
            file_path TEXT,
            uploaded_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS communication_logs (
            log_id TEXT PRIMARY KEY,
            customer_id TEXT,
            order_id TEXT,
            order_line_id TEXT,
            contact_id TEXT,
            channel TEXT NOT NULL,
            direction TEXT NOT NULL,
            subject TEXT,
            content TEXT NOT NULL,
            attachments_json TEXT,
            occurred_at TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            tags_json TEXT
        );
        CREATE TABLE IF NOT EXISTS email_accounts (
            account_id TEXT PRIMARY KEY,
            email_address TEXT NOT NULL,
            display_name TEXT,
            smtp_host TEXT,
            smtp_port INTEGER DEFAULT 587,
            smtp_user TEXT,
            smtp_password TEXT,
            is_default INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS email_templates (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            template_type TEXT,
            subject TEXT,
            body TEXT,
            is_builtin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS email_logs (
            email_id TEXT PRIMARY KEY,
            account_id TEXT,
            customer_id TEXT,
            contact_id TEXT,
            order_id TEXT,
            template_type TEXT,
            to_email TEXT,
            cc_email TEXT,
            subject TEXT,
            body TEXT,
            attachments_json TEXT,
            status TEXT DEFAULT 'draft',
            sent_at TEXT,
            error_message TEXT
        );
        """)
        # Seed default rates
        for cur, rate in [("USD", 7.2), ("EUR", 7.8), ("CNY", 1), ("GBP", 9.1)]:
            c.execute("INSERT OR IGNORE INTO fx_rates VALUES (?,?)", (cur, rate))
        # 迁移：costs 表增加 fx_rate 列（每行独立汇率）
        try:
            c.execute("ALTER TABLE costs ADD COLUMN fx_rate REAL DEFAULT NULL")
        except Exception:
            pass

def row_to_dict(row): return dict(row) if row else None
def rows_to_list(rows): return [row_to_dict(r) for r in rows]

# ═══════════════════════════════════════════════════════════
#  ORDER HELPERS
# ═══════════════════════════════════════════════════════════
def load_order(o_id):
    with db() as c:
        o = row_to_dict(c.execute("SELECT * FROM orders WHERE order_id=?", (o_id,)).fetchone())
        if not o: return None
        lines = rows_to_list(c.execute("SELECT * FROM order_lines WHERE order_id=?", (o_id,)).fetchall())
        o["lines"] = lines
        return o

def list_orders():
    with db() as c:
        orders = rows_to_list(c.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall())
        for o in orders:
            lines = rows_to_list(c.execute("SELECT * FROM order_lines WHERE order_id=?", (o["order_id"],)).fetchall())
            o["lines"] = lines
        return orders

def compute_order_status(o):
    lines = o["lines"]
    total_qty = sum(l["quantity"] or 0 for l in lines)
    shipped_qty = sum(l["shipped_qty"] or 0 for l in lines)
    all_done = all(l["status"] == "done" for l in lines)
    all_cxl = all(l["status"] == "cancelled" for l in lines)
    if all_done: return "done"
    if all_cxl: return "cancelled"
    if shipped_qty >= total_qty and total_qty > 0: return "shipped"
    if shipped_qty > 0: return "partial"
    if any(l["status"] == "pending" for l in lines): return "pending"
    if any(l["status"] == "producing" for l in lines): return "producing"
    if any(l["status"] == "confirmed" for l in lines): return "confirmed"
    return o["status"]

# ═══════════════════════════════════════════════════════════
#  ORDERS API
# ═══════════════════════════════════════════════════════════
@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/uploads/<path:fp>")
def download_upload(fp):
    """下载上传附件（沟通记录/客户附件/单证），按 customer/order/log 目录结构"""
    base = os.path.dirname(__file__)
    full = os.path.normpath(os.path.join(base, "uploads", fp))
    if not full.startswith(os.path.join(base, "uploads")):
        return jsonify({"error": "forbidden"}), 403
    if not os.path.isfile(full):
        return jsonify({"error": "not found"}), 404
    return send_from_directory("uploads", fp)

@app.route("/api/orders", methods=["GET"])
def api_list_orders(): return jsonify(list_orders())

@app.route("/api/orders/<o_id>", methods=["GET"])
def api_get_order(o_id):
    o = load_order(o_id)
    if not o: return jsonify({"error": "not found"}), 404
    return jsonify(o)

@app.route("/api/orders", methods=["POST"])
def api_create_order():
    data = request.get_json()
    o_id = data.get("order_id") or ("o" + uuid.uuid4().hex[:10])
    lines = data.get("lines", [])
    total = sum((l.get("quantity") or 0) * (l.get("unit_price") or 0) for l in lines)
    with db() as c:
        c.execute("""INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (o_id, data.get("po_number"), data.get("buyer_id"), data.get("currency", "USD"),
             data.get("incoterms"), data.get("incoterms_place"), data.get("payment_method"),
             total, data.get("status", "confirmed")))
        for l in lines:
            lid = l.get("line_id") or ("l" + uuid.uuid4().hex[:10])
            c.execute("""INSERT INTO order_lines VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (lid, o_id, l.get("sku"), l.get("product_name"), l.get("description"),
                 l.get("quantity", 0), l.get("shipped_qty", 0), l.get("unit_price", 0),
                 l.get("hs_code"), l.get("status", "draft")))
    return jsonify(load_order(o_id))

@app.route("/api/orders/<o_id>", methods=["PUT"])
def api_update_order(o_id):
    data = request.get_json()
    lines = data.get("lines", [])
    total = sum((l.get("quantity") or 0) * (l.get("unit_price") or 0) for l in lines)
    with db() as c:
        c.execute("""UPDATE orders SET po_number=?, buyer_id=?, currency=?, incoterms=?,
            incoterms_place=?, payment_method=?, total_amount=? WHERE order_id=?""",
            (data.get("po_number"), data.get("buyer_id"), data.get("currency"),
             data.get("incoterms"), data.get("incoterms_place"),
             data.get("payment_method"), total, o_id))
        c.execute("DELETE FROM order_lines WHERE order_id=?", (o_id,))
        for l in lines:
            lid = l.get("line_id") or ("l" + uuid.uuid4().hex[:10])
            c.execute("""INSERT INTO order_lines VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (lid, o_id, l.get("sku"), l.get("product_name"), l.get("description"),
                 l.get("quantity", 0), l.get("shipped_qty", 0), l.get("unit_price", 0),
                 l.get("hs_code"), l.get("status", "draft")))
    o = load_order(o_id)
    new_status = compute_order_status(o)
    if new_status != o["status"]:
        with db() as c: c.execute("UPDATE orders SET status=? WHERE order_id=?", (new_status, o_id))
    return jsonify(load_order(o_id))

@app.route("/api/orders/<o_id>", methods=["PATCH"])
def api_patch_order(o_id):
    data = request.get_json()
    allowed = ["status", "po_number", "buyer_id", "currency", "incoterms", "incoterms_place", "payment_method", "total_amount"]
    sets = [f"{k}=?" for k in allowed if k in data]
    if sets:
        with db() as c:
            c.execute(f"UPDATE orders SET {','.join(sets)} WHERE order_id=?", [data[k] for k in allowed if k in data] + [o_id])
    return jsonify(load_order(o_id))

@app.route("/api/orders/<o_id>", methods=["DELETE"])
def api_delete_order(o_id):
    with db() as c:
        c.execute("DELETE FROM order_lines WHERE order_id=?", (o_id,))
        c.execute("DELETE FROM documents WHERE order_id=?", (o_id,))
        c.execute("DELETE FROM shipments WHERE order_id=?", (o_id,))
        c.execute("DELETE FROM payments WHERE order_id=?", (o_id,))
        c.execute("DELETE FROM costs WHERE order_id=?", (o_id,))
        c.execute("DELETE FROM orders WHERE order_id=?", (o_id,))
    return jsonify({"ok": True})

@app.route("/api/orders/<o_id>/lines/<lid>/status", methods=["PUT"])
def api_update_line_status(o_id, lid):
    data = request.get_json()
    status = data.get("status")
    with db() as c:
        c.execute("UPDATE order_lines SET status=? WHERE line_id=? AND order_id=?", (status, lid, o_id))
    o = load_order(o_id)
    new_status = compute_order_status(o)
    if new_status != o["status"]:
        with db() as c: c.execute("UPDATE orders SET status=? WHERE order_id=?", (new_status, o_id))
    return jsonify(load_order(o_id))

# ═══════════════════════════════════════════════════════════
#  DOCUMENTS API
# ═══════════════════════════════════════════════════════════
@app.route("/api/documents", methods=["GET"])
def api_list_docs():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall())
        for r in rows:
            r["lines"] = json.loads(r.pop("lines_json") or "[]")
            r["packs"] = json.loads(r.pop("packs_json") or "[]")
        return jsonify(rows)

@app.route("/api/documents/order/<o_id>", methods=["GET"])
def api_docs_by_order(o_id):
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM documents WHERE order_id=? ORDER BY created_at DESC", (o_id,)).fetchall())
        for r in rows:
            r["lines"] = json.loads(r.pop("lines_json") or "[]")
            r["packs"] = json.loads(r.pop("packs_json") or "[]")
        return jsonify(rows)

@app.route("/api/documents", methods=["POST"])
def api_create_doc():
    data = request.get_json()
    doc_id = data.get("doc_id") or ("d" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO documents (doc_id,invoice_no,order_id,batch_no,inv_date,currency,total_amount,total_qty,cartons,lines_json,packs_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (doc_id, data.get("invoice_no"), data.get("order_id"), data.get("batch_no"),
             data.get("inv_date"), data.get("currency"), data.get("total_amount", 0),
             data.get("total_qty", 0), data.get("cartons", 0),
             json.dumps(data.get("lines", [])), json.dumps(data.get("packs", []))))
    return jsonify({"ok": True, "invoice_no": data.get("invoice_no")})

# ═══════════════════════════════════════════════════════════
#  SHIPMENTS API
# ═══════════════════════════════════════════════════════════
@app.route("/api/shipments", methods=["GET"])
def api_list_shipments():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM shipments ORDER BY created_at DESC").fetchall())
        for r in rows:
            r["lines"] = json.loads(r.pop("lines_json") or "[]")
            r["nodes"] = json.loads(r.pop("nodes_json") or "[]")
        return jsonify(rows)

@app.route("/api/shipments", methods=["POST"])
def api_create_shipment():
    data = request.get_json()
    sid = data.get("shipment_id") or ("s" + uuid.uuid4().hex[:10])
    nodes = data.get("nodes") or [{"key":k,"label":l,"plannedDate":"","actualDate":""} for k,l in
        [("booked","已订舱"),("loaded","已装箱"),("onboard","已装船"),("departed","已离港"),
         ("arrived","已到港"),("cleared","已清关"),("delivered","已签收")]]
    with db() as c:
        for sl in data.get("lines", []):
            c.execute("UPDATE order_lines SET shipped_qty=shipped_qty+? WHERE line_id=?", (sl.get("shipped_qty", 0), sl["order_line_id"]))
            cur = c.execute("SELECT quantity, shipped_qty FROM order_lines WHERE line_id=?", (sl["order_line_id"],))
            row = cur.fetchone()
            if row:
                qty, shipped = row["quantity"], row["shipped_qty"]
                new_st = "shipped" if shipped >= qty else ("partial" if shipped > 0 else "pending")
                c.execute("UPDATE order_lines SET status=? WHERE line_id=?", (new_st, sl["order_line_id"]))
        c.execute("""INSERT INTO shipments (shipment_id,order_id,shipment_no,ship_date,carrier,tracking_no,port_loading,port_discharge,container_no,seal_no,status,lines_json,nodes_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
            (sid, data.get("order_id"), data.get("shipment_no"), data.get("ship_date"),
             data.get("carrier"), data.get("tracking_no"), data.get("port_loading"),
             data.get("port_discharge"), data.get("container_no"), data.get("seal_no"),
             data.get("status", "booked"),
             json.dumps(data.get("lines", [])), json.dumps(nodes)))
    o = load_order(data["order_id"])
    new_status = compute_order_status(o)
    if new_status != o["status"]:
        with db() as c: c.execute("UPDATE orders SET status=? WHERE order_id=?", (new_status, data["order_id"]))
    return jsonify({"ok": True})

@app.route("/api/shipments/<sid>", methods=["PUT"])
def api_update_shipment(sid):
    data = request.get_json()
    updates = []
    params = []
    for f in ["status"]:
        if f in data: updates.append(f"{f}=?"); params.append(data[f])
    if "nodes" in data:
        updates.append("nodes_json=?"); params.append(json.dumps(data["nodes"]))
    if "lines" in data:
        updates.append("lines_json=?"); params.append(json.dumps(data["lines"]))
    params.append(sid)
    with db() as c:
        if updates: c.execute(f"UPDATE shipments SET {','.join(updates)} WHERE shipment_id=?", params)
        # Auto-promote status from nodes
        if "nodes" in data:
            last_done = -1
            for i, n in enumerate(data["nodes"]):
                if n.get("actualDate"): last_done = i
            if last_done >= 0:
                from flask import request as rq
                # Nodes order matches SHIP_NODES index; find key by index
                # We stored nodes with key already, so use the last one with actualDate
                actual_key = None
                for n in data["nodes"]:
                    if n.get("actualDate"): actual_key = n["key"]
                if actual_key:
                    c.execute("UPDATE shipments SET status=? WHERE shipment_id=?", (actual_key, sid))
    return jsonify({"ok": True})

@app.route("/api/shipments/<sid>", methods=["DELETE"])
def api_delete_shipment(sid):
    with db() as c: c.execute("DELETE FROM shipments WHERE shipment_id=?", (sid,))
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  PAYMENTS API
# ═══════════════════════════════════════════════════════════
@app.route("/api/payments", methods=["GET"])
def api_list_payments():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall())
        for r in rows:
            r["allocated_invoices"] = json.loads(r.pop("allocated_json") or "[]")
        return jsonify(rows)

@app.route("/api/payments", methods=["POST"])
def api_create_payment():
    data = request.get_json()
    pid = data.get("payment_id") or ("p" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO payments (payment_id,order_id,amount,currency,method,received_date,bank_reference,note,allocated_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
            (pid, data.get("order_id"), data.get("amount", 0), data.get("currency"),
             data.get("method"), data.get("received_date"), data.get("bank_reference"),
             data.get("note"),
             json.dumps(data.get("allocated_invoices", []))))
    return jsonify({"ok": True})

@app.route("/api/payments/<pid>", methods=["DELETE"])
def api_delete_payment(pid):
    with db() as c: c.execute("DELETE FROM payments WHERE payment_id=?", (pid,))
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  COSTS API
# ═══════════════════════════════════════════════════════════
@app.route("/api/costs", methods=["GET"])
def api_list_costs():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM costs ORDER BY updated_at DESC").fetchall())
        # Aggregate by order_id
        grouped = {}
        for r in rows:
            oid = r["order_id"]
            if oid not in grouped: grouped[oid] = {"order_id": oid, "currency": r["currency"], "fx_rate": r.get("fx_rate"), "costs": []}
            grouped[oid]["costs"].append({
                "category": r["category"], "amount": r["amount"], "note": r["note"], "fx_rate": r.get("fx_rate")
            })
        return jsonify(list(grouped.values()))

@app.route("/api/costs", methods=["POST"])
def api_save_costs():
    data = request.get_json()
    oid = data["order_id"]
    currency = data.get("currency", "CNY")
    fx_rate = data.get("fx_rate")
    with db() as c:
        for cat in ["purchase", "freight", "insurance", "commission", "bank_fee", "other"]:
            row = next((x for x in data.get("costs", []) if x.get("category") == cat), None)
            if row:
                c.execute("""INSERT INTO costs (order_id, category, amount, note, currency, fx_rate, updated_at)
                    VALUES (?,?,?,?,?,? ,strftime('%s','now'))
                    ON CONFLICT(order_id, category) DO UPDATE SET amount=excluded.amount, note=excluded.note, currency=excluded.currency, fx_rate=excluded.fx_rate, updated_at=strftime('%s','now')""",
                    (oid, cat, row.get("amount", 0), row.get("note", ""), currency, fx_rate))
            else:
                c.execute("DELETE FROM costs WHERE order_id=? AND category=?", (oid, cat))
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  FX RATES API
# ═══════════════════════════════════════════════════════════
@app.route("/api/fx", methods=["GET"])
def api_get_fx():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM fx_rates").fetchall())
        return jsonify({r["currency"]: r["rate"] for r in rows})

@app.route("/api/fx/<cur>", methods=["PUT"])
def api_set_fx(cur):
    data = request.get_json()
    rate = float(data.get("rate", 1))
    with db() as c:
        c.execute("INSERT INTO fx_rates VALUES (?,?) ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate", (cur, rate))
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════
#  CUSTOMERS API
# ═══════════════════════════════════════════════════════════
@app.route("/api/customers", methods=["GET"])
def api_list_customers():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM customers ORDER BY created_at DESC").fetchall())
    for r in rows:
        # 附加订单统计
        with db() as c:
            cnt = c.execute("SELECT COUNT(*) FROM orders WHERE buyer_id=?", (r["customer_id"],)).fetchone()[0]
            active_cnt = c.execute("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND status NOT IN ('done','cancelled')", (r["customer_id"],)).fetchone()[0]
            uncollected = c.execute("""
                SELECT COALESCE(SUM(o.total_amount),0) FROM orders o
                WHERE o.buyer_id=? AND o.status NOT IN ('done','cancelled')
            """, (r["customer_id"],)).fetchone()[0]
        r["order_count"] = cnt
        r["active_order_count"] = active_cnt
        r["uncollected_amount"] = uncollected
        # 最近沟通时间（沟通记录模块未实现时返回 None）
        try:
            last_comm = c.execute(
                "SELECT MAX(created_at) FROM communication_logs WHERE customer_id=?",
                (r["customer_id"],)
            ).fetchone()[0]
        except Exception:
            last_comm = None
        r["last_comm_at"] = last_comm
    return jsonify(rows)

@app.route("/api/customers/<cid>", methods=["GET"])
def api_get_customer(cid):
    with db() as c:
        r = row_to_dict(c.execute("SELECT * FROM customers WHERE customer_id=?", (cid,)).fetchone())
        if not r: return jsonify({"error": "not found"}), 404
        r["contacts"] = rows_to_list(c.execute("SELECT * FROM customer_contacts WHERE customer_id=?", (cid,)).fetchall())
        r["docs"] = rows_to_list(c.execute("SELECT * FROM customer_documents WHERE customer_id=?", (cid,)).fetchall())
    return jsonify(r)

@app.route("/api/customers", methods=["POST"])
def api_create_customer():
    data = request.get_json()
    cid = data.get("customer_id") or ("c" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO customers (customer_id,customer_code,company_name,company_name_cn,country,
            address_line1,address_line2,city,state_province,postal_code,tax_id,website,
            customer_type,payment_terms,default_incoterms,default_currency,credit_limit,status,note,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (cid, data.get("customer_code"), data.get("company_name"), data.get("company_name_cn"),
             data.get("country"), data.get("address_line1"), data.get("address_line2"),
             data.get("city"), data.get("state_province"), data.get("postal_code"),
             data.get("tax_id"), data.get("website"), data.get("customer_type"),
             data.get("payment_terms"), data.get("default_incoterms"), data.get("default_currency"),
             data.get("credit_limit"), data.get("status", "active"), data.get("note")))
    return jsonify({"ok": True, "customer_id": cid})

@app.route("/api/customers/<cid>", methods=["PUT"])
def api_update_customer(cid):
    data = request.get_json()
    with db() as c:
        c.execute("""UPDATE customers SET customer_code=?,company_name=?,company_name_cn=?,country=?,
            address_line1=?,address_line2=?,city=?,state_province=?,postal_code=?,tax_id=?,website=?,
            customer_type=?,payment_terms=?,default_incoterms=?,default_currency=?,credit_limit=?,status=?,note=?
            WHERE customer_id=?""",
            (data.get("customer_code"), data.get("company_name"), data.get("company_name_cn"), data.get("country"),
             data.get("address_line1"), data.get("address_line2"), data.get("city"),
             data.get("state_province"), data.get("postal_code"), data.get("tax_id"), data.get("website"),
             data.get("customer_type"), data.get("payment_terms"), data.get("default_incoterms"),
             data.get("default_currency"), data.get("credit_limit"), data.get("status"), data.get("note"), cid))
    return jsonify({"ok": True})

@app.route("/api/customers/<cid>", methods=["DELETE"])
def api_delete_customer(cid):
    with db() as c:
        c.execute("DELETE FROM customer_contacts WHERE customer_id=?", (cid,))
        c.execute("DELETE FROM customer_documents WHERE customer_id=?", (cid,))
        c.execute("DELETE FROM customers WHERE customer_id=?", (cid,))
    return jsonify({"ok": True})

@app.route("/api/customers/<cid>/contacts", methods=["POST"])
def api_add_contact(cid):
    data = request.get_json()
    contact_id = data.get("contact_id") or ("ct" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO customer_contacts (contact_id,customer_id,name,title,email,phone,whatsapp,is_primary,role)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (contact_id, cid, data.get("name"), data.get("title"), data.get("email"),
             data.get("phone"), data.get("whatsapp"), 1 if data.get("is_primary") else 0,
             data.get("role")))
    return jsonify({"ok": True, "contact_id": contact_id})

@app.route("/api/customers/<cid>/contacts/<contact_id>", methods=["PUT"])
def api_update_contact(cid, contact_id):
    data = request.get_json()
    with db() as c:
        c.execute("""UPDATE customer_contacts SET name=?,title=?,email=?,phone=?,whatsapp=?,is_primary=?,role=?
            WHERE contact_id=? AND customer_id=?""",
            (data.get("name"), data.get("title"), data.get("email"), data.get("phone"),
             data.get("whatsapp"), 1 if data.get("is_primary") else 0, data.get("role"),
             contact_id, cid))
    return jsonify({"ok": True})

@app.route("/api/customers/<cid>/contacts/<contact_id>", methods=["DELETE"])
def api_delete_contact(cid, contact_id):
    with db() as c:
        c.execute("DELETE FROM customer_contacts WHERE contact_id=? AND customer_id=?", (contact_id, cid))
    return jsonify({"ok": True})

@app.route("/api/customers/<cid>/documents", methods=["POST"])
def api_add_customer_doc(cid):
    data = request.get_json()
    doc_id = data.get("doc_id") or ("cd" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO customer_documents (doc_id,customer_id,doc_type,file_name,file_path,uploaded_at)
            VALUES (?,?,?,?,?,datetime('now','localtime'))""",
            (doc_id, cid, data.get("doc_type"), data.get("file_name"), data.get("file_path", "")))
    return jsonify({"ok": True, "doc_id": doc_id})

@app.route("/api/customers/<cid>/documents/<doc_id>", methods=["DELETE"])
def api_delete_customer_doc(cid, doc_id):
    with db() as c:
        c.execute("DELETE FROM customer_documents WHERE doc_id=? AND customer_id=?", (doc_id, cid))
        # 同步删除磁盘文件
        row = c.execute("SELECT file_path FROM customer_documents WHERE doc_id=?", (doc_id,)).fetchone()
        if row and row[0]:
            fpath = os.path.join(os.path.dirname(__file__), row[0])
            if os.path.exists(fpath):
                os.remove(fpath)
    return jsonify({"ok": True})

@app.route("/api/customers/<cid>/documents/upload", methods=["POST"])
def api_upload_customer_doc(cid):
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    doc_type = request.form.get("doc_type", "其他")
    safe_name = uuid.uuid4().hex[:8] + "_" + f.filename
    rel_path = os.path.join("uploads", safe_name)
    f.save(os.path.join(os.path.dirname(__file__), rel_path))
    doc_id = "cd" + uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("""INSERT INTO customer_documents (doc_id,customer_id,doc_type,file_name,file_path,uploaded_at)
            VALUES (?,?,?,?,?,datetime('now','localtime'))""",
            (doc_id, cid, doc_type, f.filename, rel_path))
    return jsonify({"ok": True, "doc_id": doc_id})

# ═══════════════════════════════════════════════════════════
#  INCREMENT 6: COMMUNICATION LOGS
# ═══════════════════════════════════════════════════════════
@app.route("/api/communications", methods=["GET"])
def api_list_communications():
    """列表，支持 ?customer_id=&order_id=&channel=&direction=&tag= 筛选"""
    q = request.args
    sql = "SELECT * FROM communication_logs WHERE 1=1"
    params = []
    if q.get("customer_id"):
        sql += " AND customer_id=?"; params.append(q["customer_id"])
    if q.get("order_id"):
        sql += " AND order_id=?"; params.append(q["order_id"])
    if q.get("channel"):
        sql += " AND channel=?"; params.append(q["channel"])
    if q.get("direction"):
        sql += " AND direction=?"; params.append(q["direction"])
    if q.get("tag"):
        sql += " AND tags_json LIKE ?"; params.append("%\"" + q["tag"] + "\"%")
    sql += " ORDER BY occurred_at DESC"
    with db() as c:
        return jsonify(rows_to_list(c.execute(sql, params).fetchall()))

@app.route("/api/communications", methods=["POST"])
def api_create_communication():
    data = request.get_json()
    log_id = data.get("log_id") or ("cl" + uuid.uuid4().hex[:10])
    atts = data.get("attachments") or []
    tags = data.get("tags") or []
    with db() as c:
        c.execute("""INSERT INTO communication_logs (log_id,customer_id,order_id,order_line_id,contact_id,
            channel,direction,subject,content,attachments_json,occurred_at,created_at,tags_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (log_id, data.get("customer_id"), data.get("order_id"), data.get("order_line_id"),
             data.get("contact_id"), data.get("channel"), data.get("direction"),
             data.get("subject"), data.get("content"), json.dumps(atts, ensure_ascii=False),
             data.get("occurred_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
             datetime.now().strftime("%Y-%m-%d %H:%M"),
             json.dumps(tags, ensure_ascii=False)))
    # 更新客户最近沟通时间（customers 表无此列时忽略）
    return jsonify({"ok": True, "log_id": log_id})

@app.route("/api/communications/<log_id>", methods=["PUT"])
def api_update_communication(log_id):
    data = request.get_json()
    atts = data.get("attachments")
    tags = data.get("tags")
    with db() as c:
        c.execute("""UPDATE communication_logs SET customer_id=?,order_id=?,order_line_id=?,contact_id=?,
            channel=?,direction=?,subject=?,content=?,attachments_json=?,occurred_at=?,tags_json=?
            WHERE log_id=?""",
            (data.get("customer_id"), data.get("order_id"), data.get("order_line_id"),
             data.get("contact_id"), data.get("channel"), data.get("direction"),
             data.get("subject"), data.get("content"),
             json.dumps(atts, ensure_ascii=False) if atts is not None else None,
             data.get("occurred_at"),
             json.dumps(tags, ensure_ascii=False) if tags is not None else None,
             log_id))
    return jsonify({"ok": True})

@app.route("/api/communications/<log_id>", methods=["DELETE"])
def api_delete_communication(log_id):
    with db() as c:
        c.execute("DELETE FROM communication_logs WHERE log_id=?", (log_id,))
    return jsonify({"ok": True})

@app.route("/api/communications/export/<cid>", methods=["GET"])
def api_export_communications(cid):
    """导出某客户所有沟通记录为 CSV"""
    with db() as c:
        rows = rows_to_list(c.execute(
            "SELECT * FROM communication_logs WHERE customer_id=? ORDER BY occurred_at DESC", (cid,)
        ).fetchall())
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["log_id", "order_id", "channel", "direction", "subject", "content", "occurred_at", "tags", "created_at"])
    for r in rows:
        w.writerow([r["log_id"], r.get("order_id",""), r["channel"], r["direction"],
                    r.get("subject",""), r["content"], r["occurred_at"],
                    r.get("tags_json",""), r.get("created_at","")])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=communications_"+cid+".csv"
    return resp

@app.route("/api/communications/<log_id>/attachments/upload", methods=["POST"])
def api_upload_comm_attachment(log_id):
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    cid = request.form.get("customer_id", "")
    oid = request.form.get("order_id", "")
    safe_name = uuid.uuid4().hex[:8] + "_" + f.filename
    rel_dir = os.path.join("uploads", cid or "customer", oid or "order", log_id)
    os.makedirs(os.path.join(os.path.dirname(__file__), rel_dir), exist_ok=True)
    f.save(os.path.join(os.path.dirname(__file__), rel_dir, safe_name))
    rel_path = os.path.join(rel_dir, safe_name)
    with db() as c:
        row = c.execute("SELECT attachments_json FROM communication_logs WHERE log_id=?", (log_id,)).fetchone()
        atts = json.loads(row[0]) if row and row[0] else []
        atts.append({"file_name": f.filename, "file_path": rel_path})
        c.execute("UPDATE communication_logs SET attachments_json=? WHERE log_id=?",
                  (json.dumps(atts, ensure_ascii=False), log_id))
    return jsonify({"ok": True, "attachment": atts[-1]})

# ═══════════════════════════════════════════════════════════
#  INCREMENT 7: EMAIL ACCOUNTS / TEMPLATES / LOGS
# ═══════════════════════════════════════════════════════════
@app.route("/api/email/accounts", methods=["GET"])
def api_list_email_accounts():
    with db() as c:
        rows = rows_to_list(c.execute("SELECT * FROM email_accounts").fetchall())
    for r in rows:
        r["smtp_password"] = "******" if r.get("smtp_password") else ""
    return jsonify(rows)

@app.route("/api/email/accounts", methods=["POST"])
def api_save_email_account():
    data = request.get_json()
    aid = data.get("account_id") or ("ea" + uuid.uuid4().hex[:10])
    if not data.get("smtp_password") or data.get("smtp_password") == "******":
        with db() as c:
            old = c.execute("SELECT smtp_password FROM email_accounts WHERE account_id=?", (aid,)).fetchone()
            data["smtp_password"] = (old[0] if old else "")
    with db() as c:
        if data.get("is_default"):
            c.execute("UPDATE email_accounts SET is_default=0")
            data["is_default"] = 1
        c.execute("""INSERT INTO email_accounts (account_id,email_address,display_name,smtp_host,smtp_port,
            smtp_user,smtp_password,is_default) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET email_address=excluded.email_address,
            display_name=excluded.display_name,smtp_host=excluded.smtp_host,smtp_port=excluded.smtp_port,
            smtp_user=excluded.smtp_user,smtp_password=excluded.smtp_password,is_default=excluded.is_default""",
            (aid, data.get("email_address"), data.get("display_name"), data.get("smtp_host"),
             data.get("smtp_port", 587), data.get("smtp_user"), data.get("smtp_password"),
             1 if data.get("is_default") else 0))
    return jsonify({"ok": True, "account_id": aid})

@app.route("/api/email/accounts/<aid>", methods=["DELETE"])
def api_delete_email_account(aid):
    with db() as c:
        c.execute("DELETE FROM email_accounts WHERE account_id=?", (aid,))
    return jsonify({"ok": True})

@app.route("/api/email/templates", methods=["GET"])
def api_list_email_templates():
    with db() as c:
        return jsonify(rows_to_list(c.execute("SELECT * FROM email_templates ORDER BY created_at DESC").fetchall()))

@app.route("/api/email/templates", methods=["POST"])
def api_save_email_template():
    data = request.get_json()
    tid = data.get("template_id") or ("et" + uuid.uuid4().hex[:10])
    with db() as c:
        c.execute("""INSERT INTO email_templates (template_id,name,template_type,subject,body,is_builtin)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(template_id) DO UPDATE SET name=excluded.name,template_type=excluded.template_type,
            subject=excluded.subject,body=excluded.body""",
            (tid, data.get("name"), data.get("template_type"), data.get("subject"),
             data.get("body"), 1 if data.get("is_builtin") else 0))
    return jsonify({"ok": True, "template_id": tid})

@app.route("/api/email/templates/<tid>", methods=["DELETE"])
def api_delete_email_template(tid):
    with db() as c:
        row = c.execute("SELECT is_builtin FROM email_templates WHERE template_id=?", (tid,)).fetchone()
        if row and row[0]:
            return jsonify({"error": "内置模板不可删除，可编辑或复制"}), 400
        c.execute("DELETE FROM email_templates WHERE template_id=?", (tid,))
    return jsonify({"ok": True})

@app.route("/api/email/render", methods=["POST"])
def api_render_email():
    """渲染模板：{template_id, order_id, customer_id} → 返回渲染后的 subject/body"""
    data = request.get_json()
    tid = data.get("template_id")
    oid = data.get("order_id")
    with db() as c:
        tpl = row_to_dict(c.execute("SELECT * FROM email_templates WHERE template_id=?", (tid,)).fetchone())
    if not tpl:
        return jsonify({"error": "template not found"}), 404
    order = row_to_dict(c.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()) if oid else None
    if order is None and oid:
        with db() as c2:
            order = row_to_dict(c2.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())
    o_lines = []
    cust = None
    contact = None
    with db() as c:
        if oid:
            o_lines = rows_to_list(c.execute("SELECT * FROM order_lines WHERE order_id=?", (oid,)).fetchall())
        cid = data.get("customer_id") or (order or {}).get("buyer_id")
        if cid:
            cust = row_to_dict(c.execute("SELECT * FROM customers WHERE customer_id=?", (cid,)).fetchone())
            if data.get("contact_id"):
                contact = row_to_dict(c.execute(
                    "SELECT * FROM customer_contacts WHERE contact_id=? AND customer_id=?",
                    (data["contact_id"], cid)).fetchone())
            else:
                contact = row_to_dict(c.execute(
                    "SELECT * FROM customer_contacts WHERE customer_id=? AND is_primary=1", (cid,)).fetchone())
                if not contact:
                    contact = row_to_dict(c.execute(
                        "SELECT * FROM customer_contacts WHERE customer_id=? LIMIT 1", (cid,)).fetchone())

    # 变量映射
    lines_desc = []
    for l in (o_lines or []):
        lines_desc.append(f"{l.get('product_name') or l.get('sku') or ''} x {l.get('quantity',0)}")
    incoterms = ""
    if order:
        incoterms = (order.get("incoterms") or "") + (" " + (order.get("incoterms_place") or "") if order.get("incoterms_place") else "")
    variables = {
        "contact_name": (contact or {}).get("name", ""),
        "po_number": (order or {}).get("po_number", ""),
        "pi_number": (order or {}).get("po_number", ""),
        "product_list": "\n- ".join(lines_desc) if lines_desc else "",
        "total_amount": f"{(order or {}).get('total_amount', 0):,.2f}" if order else "",
        "currency": (order or {}).get("currency", ""),
        "payment_terms": (order or {}).get("payment_method", ""),
        "incoterms": incoterms,
        "sender_name": "",
        "company_name": "Our Company",
        "customer_name": (cust or {}).get("company_name", ""),
    }
    # 取发件人
    with db() as c:
        acc = row_to_dict(c.execute("SELECT * FROM email_accounts WHERE is_default=1 LIMIT 1").fetchone())
        if not acc:
            acc = row_to_dict(c.execute("SELECT * FROM email_accounts LIMIT 1").fetchone())
    if acc:
        variables["sender_name"] = acc.get("display_name") or acc.get("email_address", "")

    # 催款变量：计算该客户所有订单的未收清金额
    if tpl.get("template_type") == "payment":
        cid = cust.get("customer_id") if cust else None
        overdue_lines = []
        total_overdue = 0.0
        with db() as c:
            cust_orders = rows_to_list(c.execute(
                "SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC", (cid,) if cid else ()
            ).fetchall()) if cid else []
            for co in cust_orders:
                invs = rows_to_list(c.execute(
                    "SELECT * FROM documents WHERE order_id=?", (co["order_id"],)
                ).fetchall())
                paid = rows_to_list(c.execute(
                    "SELECT * FROM payments WHERE order_id=?", (co["order_id"],)
                ).fetchall())
                paid_amount = sum(p.get("amount") or 0 for p in paid)
                due = (co.get("total_amount") or 0) - paid_amount
                if due > 0.01:
                    overdue_lines.append(f"- {co.get('po_number','')} · 总额 {co.get('currency','')} {due:,.2f}")
                    total_overdue += due
        variables["overdue_list"] = "\n".join(overdue_lines) if overdue_lines else "（无未收清款项）"
        variables["overdue_days"] = ""
        variables["overdue_amount"] = f"{total_overdue:,.2f}"

    subject = tpl.get("subject", "")
    body = tpl.get("body", "")
    for k, v in variables.items():
        subject = subject.replace("{{" + k + "}}", str(v))
        body = body.replace("{{" + k + "}}", str(v))
    return jsonify({"subject": subject, "body": body, "variables": variables,
                    "to_email": (contact or {}).get("email", ""),
                    "order": order, "customer": cust, "contact": contact})

@app.route("/api/email/send", methods=["POST"])
def api_send_email():
    """SMTP 发送邮件 + 记录 email_logs + 自动创建 communication_logs"""
    import smtplib
    from email.message import EmailMessage
    data = request.get_json()
    aid = data.get("account_id")
    with db() as c:
        acc = row_to_dict(c.execute("SELECT * FROM email_accounts WHERE account_id=?", (aid,)).fetchone())
    if not acc:
        return jsonify({"error": "account not found"}), 404
    email_id = "em" + uuid.uuid4().hex[:10]
    error_msg = None
    sent_at = None
    try:
        msg = EmailMessage()
        msg["From"] = acc["email_address"]
        msg["To"] = data.get("to_email", "")
        if data.get("cc_email"):
            msg["Cc"] = data["cc_email"]
        msg["Subject"] = data.get("subject", "")
        msg.set_content(data.get("body", ""))
        # 附加附件（file_path 相对项目根目录）
        for att in (data.get("attachments") or []):
            fp = os.path.join(os.path.dirname(__file__), att["file_path"])
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    maintype, subtype = mimetypes.guess_type(att.get("file_name", fp))
                    maintype = (maintype or "application").split("/")[0]
                    subtype = subtype or "octet-stream"
                    msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype,
                                       filename=att.get("file_name", os.path.basename(fp)))
        with smtplib.SMTP(acc["smtp_host"], int(acc["smtp_port"] or 587)) as s:
            s.starttls()
            s.login(acc["smtp_user"], acc["smtp_password"])
            s.send_message(msg)
        sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "sent"
    except Exception as e:
        error_msg = str(e)
        status = "failed"

    with db() as c:
        c.execute("""INSERT INTO email_logs (email_id,account_id,customer_id,contact_id,order_id,
            template_type,to_email,cc_email,subject,body,attachments_json,status,sent_at,error_message)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (email_id, aid, data.get("customer_id"), data.get("contact_id"), data.get("order_id"),
             data.get("template_type"), data.get("to_email", ""), data.get("cc_email", ""),
             data.get("subject", ""), data.get("body", ""),
             json.dumps(data.get("attachments") or [], ensure_ascii=False),
             status, sent_at, error_msg))
        # 自动创建沟通记录
        log_id = "cl" + uuid.uuid4().hex[:10]
        c.execute("""INSERT INTO communication_logs (log_id,customer_id,order_id,contact_id,channel,direction,
            subject,content,attachments_json,occurred_at,tags_json) VALUES (?,?,?,?,?,?,?,?,?,?,'[]')""",
            (log_id, data.get("customer_id"), data.get("order_id"), data.get("contact_id"),
             "邮件", "发出", data.get("subject", ""),
             ("[邮件] " + (data.get("subject") or "")).strip(),
             json.dumps(data.get("attachments") or [], ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M")))
    if error_msg:
        return jsonify({"ok": False, "email_id": email_id, "error": error_msg, "log_id": log_id}), 200
    return jsonify({"ok": True, "email_id": email_id, "log_id": log_id})

@app.route("/api/email/logs", methods=["GET"])
def api_list_email_logs():
    q = request.args
    sql = "SELECT * FROM email_logs WHERE 1=1"
    params = []
    if q.get("order_id"):
        sql += " AND order_id=?"; params.append(q["order_id"])
    if q.get("customer_id"):
        sql += " AND customer_id=?"; params.append(q["customer_id"])
    sql += " ORDER BY email_id DESC LIMIT 200"
    with db() as c:
        rows = rows_to_list(c.execute(sql, params).fetchall())
    return jsonify(rows)

# 内置模板预置
def seed_builtin_templates():
    with db() as c:
        exists = c.execute("SELECT COUNT(*) FROM email_templates WHERE is_builtin=1").fetchone()[0]
        if exists:
            return
        builtins = [
            ("PI 模板", "pi", "PI for {{po_number}} — Proforma Invoice",
             """Dear {{contact_name}},

Thank you for your order {{po_number}}.

Please find attached our Proforma Invoice {{pi_number}} for your confirmation.

Order Details:
- Product: {{product_list}}
- Total Amount: {{currency}} {{total_amount}}
- Payment Terms: {{payment_terms}}
- Incoterms: {{incoterms}}

Please confirm and arrange the deposit payment. We will start production upon receipt.

Best regards,
{{sender_name}}"""),
            ("CI 模板", "ci", "Commercial Invoice for {{po_number}}",
             """Dear {{contact_name}},

Please find attached our Commercial Invoice and Packing List for order {{po_number}}.

- Total Amount: {{currency}} {{total_amount}}
- Incoterms: {{incoterms}}

Estimated shipment: per booking confirmation.

Best regards,
{{sender_name}}"""),
            ("催款模板", "payment", "Payment Reminder for {{po_number}}",
             """Dear {{contact_name}},

We would like to remind you that the following invoices are overdue:

{{overdue_list}}

Overdue days: {{overdue_days}}
Total overdue amount: {{overdue_amount}}

Please arrange payment at your earliest convenience.

Best regards,
{{sender_name}}"""),
            ("发货通知模板", "shipment", "Shipment Notice for {{po_number}}",
             """Dear {{contact_name}},

We are pleased to inform you that the goods for order {{po_number}} have been shipped.

- Total Amount: {{currency}} {{total_amount}}
- Incoterms: {{incoterms}}

Attached: Bill of Lading copy and Packing List.

Best regards,
{{sender_name}}"""),
        ]
        for name, ttype, subj, body in builtins:
            tid = "et_builtin_" + ttype
            c.execute("""INSERT INTO email_templates (template_id,name,template_type,subject,body,is_builtin)
                VALUES (?,?,?,?,?,1)""", (tid, name, ttype, subj, body))

# ═══════════════════════════════════════════════════════════
#  START
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    seed_builtin_templates()
    print(f"▶ 外贸履约系统启动")
    print(f"▶ SQLite: {DB_PATH}")
    print(f"▶ 访问: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
