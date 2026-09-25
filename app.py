from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import uuid, os, json
from datetime import datetime
from ledger import get_conn, init_db, barter_value
from reputation import can_payout, get_attestations, check_attestation_threshold
import pathlib

app = Flask(__name__, static_folder="static")
CORS(app)
init_db()

BASE_DIR = pathlib.Path(__file__).parent

@app.route("/")
def index():
    # Serve the built frontend if exists, else simple message
    static_index = BASE_DIR / "static" / "index.html"
    if static_index.exists():
        return send_from_directory("static", "index.html")
    return jsonify({"status": "Beehive API running", "ledger": "hive.db", "endpoints": ["/api/needs","/api/operators","/api/ledger"]})

@app.route("/api/operators")
def operators():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM operators").fetchall()]
    conn.close()
    for r in rows:
        try: r["pools"] = json.loads(r["pools"])
        except: pass
    return jsonify(rows)

@app.route("/api/needs")
def needs():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM needs ORDER BY created_at DESC").fetchall()]
    conn.close()
    for r in rows:
        try: r["required_pools"] = json.loads(r["required_pools"])
        except: pass
    return jsonify(rows)

@app.route("/api/ledger")
def ledger():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM ledger ORDER BY created_at DESC LIMIT 100").fetchall()]
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
    return jsonify({"id": nid, "barter_value": value})

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
    # get claimed operator
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
    amount = need["barter_value"]
    
    # Move HC
    conn.execute("UPDATE operators SET hc_balance = hc_balance - ? WHERE id=?", (amount, from_id))
    conn.execute("UPDATE operators SET hc_balance = hc_balance + ? WHERE id=?", (amount, to_id))
    lid = f"led_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                 (lid, from_id, to_id, amount, "task_payout", need_id, f"Payout for {need_id}", datetime.utcnow().isoformat()))
    conn.execute("UPDATE needs SET status='paid' WHERE id=?", (need_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "amount": amount, "from": from_id, "to": to_id})

@app.route("/api/transfer", methods=["POST"])
def transfer():
    data = request.json
    from_id = data["from_id"]
    to_id = data["to_id"]
    amount = float(data["amount"])
    note = data.get("note","hand-to-hand transfer")
    conn = get_conn()
    # check balance
    bal = conn.execute("SELECT hc_balance FROM operators WHERE id=?", (from_id,)).fetchone()
    if not bal or bal["hc_balance"] < amount:
        conn.close()
        return jsonify({"error": "insufficient HC"}), 400
    conn.execute("UPDATE operators SET hc_balance = hc_balance - ? WHERE id=?", (amount, from_id))
    conn.execute("UPDATE operators SET hc_balance = hc_balance + ? WHERE id=?", (amount, to_id))
    lid = f"led_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                 (lid, from_id, to_id, amount, "transfer", None, note, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/cashout", methods=["POST"])
def cashout():
    # Logs that HC was settled in cash hand-to-hand - no processor
    data = request.json
    from_id = data["from_id"]
    to_id = data["to_id"]
    amount = float(data["amount"])
    cash_amount = data.get("cash_amount", amount)  # local conversion
    conn = get_conn()
    lid = f"led_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                 (lid, from_id, to_id, amount, "cashout", None, f"Cash settled ${cash_amount} for {amount} HC - hand to hand", datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "note": f"Logged {amount} HC settled as ${cash_amount} cash hand-to-hand. No fee."})

if __name__ == "__main__":
    print("BEEHIVE RUNNING")
    print("Ledger file: hive.db - copy this file hand-to-hand, it's the economy")
    print("Local: http://localhost:5000")
    print("Network: http://YOUR_LOCAL_IP:5000 - share this with neighbors on same WiFi")
    app.run(host="0.0.0.0", port=5000, debug=True)
