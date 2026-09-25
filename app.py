from flask import Flask, jsonify, request, send_from_directory, redirect
from flask_cors import CORS
import uuid, os, json, requests, base64
from datetime import datetime
from ledger import get_conn, init_db, barter_value, calc_fee, HC_TO_USD_RATE, PLATFORM_FEE_PCT, PLATFORM_PAYPAL_EMAIL
from reputation import can_payout, get_attestations, check_attestation_threshold
import pathlib

app = Flask(__name__, static_folder="static")
CORS(app)
init_db()

BASE_DIR = pathlib.Path(__file__).parent

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

def paypal_enabled():
    return bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)

def get_paypal_access_token():
    if not paypal_enabled():
        return None, "PayPal not configured"
    try:
        auth = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
        headers = {"Accept": "application/json", "Accept-Language": "en_US", "Authorization": f"Basic {auth}"}
        data = {"grant_type": "client_credentials"}
        resp = requests.post(f"{PAYPAL_API_BASE}/v1/oauth2/token", headers=headers, data=data, timeout=15)
        resp.raise_for_status()
        return resp.json().get("access_token"), None
    except Exception as e:
        return None, f"PayPal auth failed: {str(e)}"

def send_paypal_payouts(payout_items, need_id):
    if not paypal_enabled():
        return False, None, None, "PayPal not configured - HC credited, payout pending setup"
    token, err = get_paypal_access_token()
    if not token:
        return False, None, None, err
    batch_id = f"BEEHIVE_{need_id}_{uuid.uuid4().hex[:6].upper()}"
    items = []
    for idx, item in enumerate(payout_items):
        items.append({
            "recipient_type": "EMAIL",
            "amount": {"value": f"{float(item['amount']):.2f}", "currency": "USD"},
            "receiver": item["email"],
            "note": item["note"][:500],
            "sender_item_id": f"{need_id}_{item['type']}_{idx}"
        })
    payload = {
        "sender_batch_header": {
            "sender_batch_id": batch_id,
            "email_subject": "You have received a payout from The Beehive Commons!",
            "email_message": "You completed a task in The Beehive Commons and have been paid. Thank you for being an Operator!"
        },
        "items": items
    }
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = requests.post(f"{PAYPAL_API_BASE}/v1/payments/payouts", headers=headers, json=payload, timeout=20)
        result = resp.json()
        if resp.status_code in [200, 201, 202]:
            return True, result.get("batch_header", {}).get("payout_batch_id", batch_id), result, None
        else:
            return False, batch_id, result, f"PayPal API error: {resp.status_code} - {result}"
    except Exception as e:
        return False, batch_id, None, f"Payout exception: {str(e)}"

@app.route("/static/boomerang.mp4")
def boomerang_alias():
    if (BASE_DIR / "static" / "videos" / "background-loop.mp4").exists():
        return redirect("/static/videos/background-loop.mp4")
    if (BASE_DIR / "static" / "boomerang.mp4").exists():
        return send_from_directory("static", "boomerang.mp4")
    return ("", 204)

@app.route("/")
def index():
    templates_index = BASE_DIR / "templates" / "index.html"
    if templates_index.exists():
        return send_from_directory("templates", "index.html")
    static_index = BASE_DIR / "static" / "index.html"
    if static_index.exists():
        return send_from_directory("static", "index.html")
    return jsonify({"status": "Beehive API running - WITH PAYPAL PAYOUTS", "ledger": "hive.db", "paypal_enabled": paypal_enabled(), "fee": "1.4%", "platform": PLATFORM_PAYPAL_EMAIL})

@app.route("/api/config")
def config():
    return jsonify({
        "HC_TO_USD_RATE": HC_TO_USD_RATE,
        "PLATFORM_FEE_PCT": PLATFORM_FEE_PCT,
        "PLATFORM_FEE_DISPLAY": "1.4%",
        "PLATFORM_PAYPAL_EMAIL": PLATFORM_PAYPAL_EMAIL,
        "PAYPAL_MODE": PAYPAL_MODE,
        "PAYPAL_ENABLED": paypal_enabled(),
        "RATE": f"1 HC = ${HC_TO_USD_RATE}"
    })

@app.route("/api/operators")
def operators():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM operators").fetchall()]
    conn.close()
    for r in rows:
        try: r["pools"] = json.loads(r["pools"])
        except: pass
        r.pop("password", None)
        if r.get("paypal_email"):
            email = r["paypal_email"]
            if "@" in email and len(email) > 3:
                parts = email.split("@")
                r["paypal_email_masked"] = f"{parts[0][:2]}***@{parts[1]}"
    return jsonify(rows)

@app.route("/api/needs")
def needs():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM needs ORDER BY created_at DESC").fetchall()]
    conn.close()
    for r in rows:
        try: r["required_pools"] = json.loads(r["required_pools"])
        except: pass
        fee_hc, fee_usd, net_hc = calc_fee(r["barter_value"])
        r["fee_hc"] = fee_hc
        r["fee_usd"] = fee_usd
        r["net_hc"] = net_hc
        r["net_usd"] = round(net_hc * HC_TO_USD_RATE, 2)
        r["gross_usd"] = round(float(r["barter_value"]) * HC_TO_USD_RATE, 2)
    return jsonify(rows)

@app.route("/api/ledger")
def ledger_route():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM ledger ORDER BY created_at DESC LIMIT 100").fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/payouts")
def payouts():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM paypal_payouts ORDER BY created_at DESC LIMIT 100").fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/create_need", methods=["POST"])
def create_need():
    data = request.json
    nid = f"need_{uuid.uuid4().hex[:8]}"
    baseline = float(data.get("baseline", 10))
    desirability = float(data.get("desirability", 0.5))
    urgency = float(data.get("urgency", 5))
    value = barter_value(baseline, desirability, urgency)
    conn = get_conn()
    conn.execute("INSERT INTO needs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (nid, data.get("type","general"), data.get("description",""), urgency, desirability, baseline, value,
                  data.get("zone","46250"), json.dumps(data.get("required_pools",["real_world_ops"])),
                  "open", data.get("posted_by","drew"), None, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    fee_hc, fee_usd, net_hc = calc_fee(value)
    return jsonify({"id": nid, "barter_value": value, "fee_hc": fee_hc, "fee_usd": fee_usd, "net_hc": net_hc})

@app.route("/api/claim", methods=["POST"])
def claim():
    data = request.json
    need_id = data["need_id"]
    operator_id = data["operator_id"]
    conn = get_conn()
    conn.execute("UPDATE needs SET claimed_by=?, status='claimed' WHERE id=?", (operator_id, need_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/complete", methods=["POST"])
def complete():
    data = request.json
    need_id = data["need_id"]
    conn = get_conn()
    conn.execute("UPDATE needs SET status='needs_attestation' WHERE id=?", (need_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/attest", methods=["POST"])
def attest():
    data = request.json
    need_id = data["need_id"]
    attester_id = data["attester_id"]
    conn = get_conn()
    need = conn.execute("SELECT * FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return jsonify({"error": "need not found"}), 404
    operator_id = need["claimed_by"]
    aid = f"att_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO attestations VALUES (?,?,?,?,?)",
                 (aid, need_id, attester_id, operator_id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    atts = get_attestations(need_id)
    can, msg = can_payout(need_id)
    return jsonify({"attestations": atts, "can_payout": can, "msg": msg})

@app.route("/api/pay", methods=["POST"])
def pay():
    data = request.json
    need_id = data["need_id"]
    conn = get_conn()
    need = conn.execute("SELECT * FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return jsonify({"error": "need not found"}), 404
    need = dict(need)
    can, msg = can_payout(need_id)
    if not can:
        conn.close()
        return jsonify({"error": msg}), 400
    from_id = need["posted_by"]
    to_id = need["claimed_by"]
    amount = float(need["barter_value"])
    gross_usd = round(amount * HC_TO_USD_RATE, 2)
    fee_hc, fee_usd, net_hc = calc_fee(amount)
    fee_usd = round(fee_hc * HC_TO_USD_RATE, 2)
    net_usd = round(net_hc * HC_TO_USD_RATE, 2)
    worker = conn.execute("SELECT * FROM operators WHERE id=?", (to_id,)).fetchone()
    worker_email = worker["paypal_email"] if worker and "paypal_email" in worker.keys() else ""
    if not worker_email:
        conn.close()
        return jsonify({"error": f"Worker {to_id} has no PayPal email"}), 400
    conn.execute("UPDATE operators SET hc_balance = hc_balance - ? WHERE id=?", (amount, from_id))
    conn.execute("UPDATE operators SET hc_balance = hc_balance + ?, usd_earned = usd_earned + ? WHERE id=?", (net_hc, net_usd, to_id))
    conn.execute("UPDATE operators SET hc_balance = hc_balance + ?, usd_earned = usd_earned + ? WHERE id='hive_platform'", (fee_hc, fee_usd))
    lid1 = f"led_{uuid.uuid4().hex[:8]}"
    lid2 = f"led_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid1, from_id, to_id, net_hc, net_usd, "task_payout_net", need_id, f"Net payout for {need_id} - 98.6%", now))
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid2, from_id, "hive_platform", fee_hc, fee_usd, "platform_fee", need_id, f"Platform fee 1.4% for {need_id}", now))
    conn.execute("UPDATE needs SET status='paid' WHERE id=?", (need_id,))
    conn.commit()
    # PayPal payouts
    payout_items = [
        {"email": worker_email, "amount": net_usd, "note": f"Beehive payout {net_hc} HC (${net_usd}) for {need_id} - 98.6% - Thank you Operator!", "type": "worker"},
        {"email": PLATFORM_PAYPAL_EMAIL, "amount": fee_usd, "note": f"Beehive platform fee {fee_hc} HC (${fee_usd}) 1.4% for {need_id}", "type": "platform"}
    ]
    success, batch_id, paypal_resp, error = send_paypal_payouts(payout_items, need_id)
    if success:
        pid1 = f"pp_{uuid.uuid4().hex[:8]}"
        pid2 = f"pp_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO paypal_payouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid1, batch_id, need_id, from_id, to_id, worker_email, gross_usd, fee_usd, net_usd, "worker", "sent", json.dumps(paypal_resp), now))
        conn.execute("INSERT INTO paypal_payouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid2, batch_id, need_id, from_id, "hive_platform", PLATFORM_PAYPAL_EMAIL, gross_usd, fee_usd, fee_usd, "platform", "sent", json.dumps(paypal_resp), now))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "paypal": True, "batch_id": batch_id, "gross": amount, "gross_usd": gross_usd, "fee_hc": fee_hc, "fee_usd": fee_usd, "net_hc": net_hc, "net_usd": net_usd, "worker_email": worker_email, "platform_email": PLATFORM_PAYPAL_EMAIL, "message": f"PAID: Worker got ${net_usd} (98.6%) to {worker_email}, Platform got ${fee_usd} (1.4%) to {PLATFORM_PAYPAL_EMAIL}", "paypal_response": paypal_resp})
    else:
        conn.close()
        return jsonify({"ok": True, "paypal": False, "paypal_pending": True, "gross": amount, "gross_usd": gross_usd, "fee_hc": fee_hc, "fee_usd": fee_usd, "net_hc": net_hc, "net_usd": net_usd, "worker_email": worker_email, "platform_email": PLATFORM_PAYPAL_EMAIL, "message": f"HC credited: Worker {net_hc} HC pending PayPal ${net_usd} to {worker_email}. Platform fee {fee_hc} HC pending ${fee_usd} to {PLATFORM_PAYPAL_EMAIL}. Reason: {error}", "error": error, "setup_hint": "Set PAYPAL_CLIENT_ID, PAYPAL_SECRET, PLATFORM_PAYPAL_EMAIL env vars on Render"})

@app.route("/api/transfer", methods=["POST"])
def transfer():
    data = request.json
    from_id = data["from_id"]
    to_id = data["to_id"]
    amount = float(data["amount"])
    usd_amount = round(float(amount) * HC_TO_USD_RATE, 2)
    note = data.get("note","hand-to-hand transfer")
    conn = get_conn()
    bal = conn.execute("SELECT hc_balance FROM operators WHERE id=?", (from_id,)).fetchone()
    if not bal or bal["hc_balance"] < amount:
        conn.close()
        return jsonify({"error": "insufficient HC"}), 400
    conn.execute("UPDATE operators SET hc_balance = hc_balance - ? WHERE id=?", (amount, from_id))
    conn.execute("UPDATE operators SET hc_balance = hc_balance + ? WHERE id=?", (amount, to_id))
    lid = f"led_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid, from_id, to_id, amount, usd_amount, "transfer", None, note, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/cashout", methods=["POST"])
def cashout():
    data = request.json
    from_id = data["from_id"]
    to_id = data["to_id"]
    amount = float(data["amount"])
    usd_amount = round(float(amount) * HC_TO_USD_RATE, 2)
    cash_amount = data.get("cash_amount", amount)
    conn = get_conn()
    lid = f"led_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid, from_id, to_id, amount, usd_amount, "cashout", None, f"Cash settled ${cash_amount} for {amount} HC - hand to hand", datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "note": f"Logged {amount} HC settled as ${cash_amount} cash hand-to-hand. No fee."})

@app.route("/api/create_operator", methods=["POST"])
def create_operator():
    data = request.json or {}
    op_id = data.get("id", "").strip().lower().replace(" ", "_")
    name = data.get("name", "").strip()
    password = data.get("password", "")
    paypal_email = data.get("paypal", "").strip() or data.get("paypal_email", "").strip()
    if not op_id or not name or not password:
        return jsonify({"error": "Name and password required"}), 400
    if not paypal_email:
        return jsonify({"error": "PayPal email required to get paid"}), 400
    conn = get_conn()
    exists = conn.execute("SELECT id FROM operators WHERE id=?", (op_id,)).fetchone()
    if exists:
        conn.close()
        return jsonify({"error": f"Operator {op_id} already exists"}), 400
    pools = json.dumps(["real_world_ops"])
    now = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO operators (id, name, pools, hc_balance, usd_earned, zone, password, paypal_email, created_at) VALUES (?,?,?,?,?,?,?,?,?)", (op_id, name, pools, 100.0, 0.0, "46250", password, paypal_email, now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": op_id, "name": name, "paypal_email": paypal_email})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    op_id = data.get("id", "").strip().lower()
    handle = data.get("handle", "").strip()
    password = data.get("password", "")
    if not op_id and handle:
        op_id = handle.lower().replace(" ", "_")
    if not op_id or not password:
        return jsonify({"error": "Handle and password required"}), 400
    conn = get_conn()
    row = conn.execute("SELECT * FROM operators WHERE id=?", (op_id,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM operators WHERE lower(name)=?", (handle.lower() if handle else op_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Operator not found"}), 404
    try:
        stored = row["password"] if "password" in row.keys() else ""
    except:
        stored = ""
    if stored and stored != password:
        conn.close()
        return jsonify({"error": "Invalid password"}), 401
    if not stored and row:
        try:
            conn.execute("UPDATE operators SET password=? WHERE id=?", (password, row["id"]))
            conn.commit()
        except:
            pass
    result = dict(row)
    conn.close()
    result.pop("password", None)
    try:
        result["pools"] = json.loads(result.get("pools","[]"))
    except:
        pass
    result["paypal_configured"] = paypal_enabled()
    result["platform_fee"] = "1.4%"
    return jsonify(result)

if __name__ == "__main__":
    print("BEEHIVE RUNNING WITH REAL PAYPAL PAYOUTS")
    print(f"Fee: {PLATFORM_FEE_PCT*100}% -> {PLATFORM_PAYPAL_EMAIL}")
    print(f"Rate: 1 HC = ${HC_TO_USD_RATE}")
    print(f"PayPal Mode: {PAYPAL_MODE} - Enabled: {paypal_enabled()}")
    app.run(host="0.0.0.0", port=5000, debug=True)
