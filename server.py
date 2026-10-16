"""
外贸履约系统 v1.0 — 后端服务
Python Flask + SQLite

启动: python3 server.py
访问: http://localhost:5000
"""
import sqlite3, json, os, uuid, time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
DB_PATH = os.path.join(os.path.dirname(__file__), "fulfillment.db")

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
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (order_id, category)
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
            currency TEXT PRIMARY KEY,
            rate REAL DEFAULT 1.0
        );
        """)
        # Seed default rates
        for cur, rate in [("USD", 7.2), ("EUR", 7.8), ("CNY", 1), ("GBP", 9.1)]:
            c.execute("INSERT OR IGNORE INTO fx_rates VALUES (?,?)", (cur, rate))

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
            if oid not in grouped: grouped[oid] = {"order_id": oid, "currency": r["currency"], "costs": []}
            grouped[oid]["costs"].append({
                "category": r["category"], "amount": r["amount"], "note": r["note"]
            })
        return jsonify(list(grouped.values()))

@app.route("/api/costs", methods=["POST"])
def api_save_costs():
    data = request.get_json()
    oid = data["order_id"]
    currency = data.get("currency", "CNY")
    with db() as c:
        for cat in ["purchase", "freight", "insurance", "commission", "bank_fee", "other"]:
            row = next((x for x in data.get("costs", []) if x.get("category") == cat), None)
            if row:
                c.execute("""INSERT INTO costs VALUES (?,?,?,?,?,strftime('%s','now'))
                    ON CONFLICT(order_id, category) DO UPDATE SET amount=excluded.amount, note=excluded.note, currency=excluded.currency, updated_at=strftime('%s','now')""",
                    (oid, cat, row.get("amount", 0), row.get("note", ""), currency))
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
#  START
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print(f"▶ 外贸履约系统启动")
    print(f"▶ SQLite: {DB_PATH}")
    print(f"▶ 访问: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
