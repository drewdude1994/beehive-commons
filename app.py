from flask import Flask, jsonify, request, send_from_directory, redirect
from flask_cors import CORS
import uuid, os, json, requests, base64
from datetime import datetime
from ledger import get_conn, init_db, barter_value, calc_fee, HC_TO_USD_RATE, PLATFORM_FEE_PCT, PLATFORM_PAYPAL_EMAIL
from reputation import can_payout, get_attestations, check_attestation_threshold
import pathlib

FEE_DISPLAY = f"{PLATFORM_FEE_PCT*100:g}%"
NET_PCT_DISPLAY = f"{(1-PLATFORM_FEE_PCT)*100:g}%"

app = Flask(__name__, static_folder="static")
CORS(app)
init_db()

BASE_DIR = pathlib.Path(__file__).parent

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_SECRET_KEY:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

def stripe_enabled():
    return bool(STRIPE_SECRET_KEY)

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
    return jsonify({"status": "Beehive API running", "paypal_enabled": paypal_enabled(), "stripe_enabled": stripe_enabled(), "fee": FEE_DISPLAY, "platform": PLATFORM_PAYPAL_EMAIL})

@app.route("/api/config")
def config():
    return jsonify({
        "HC_TO_USD_RATE": HC_TO_USD_RATE,
        "PLATFORM_FEE_PCT": PLATFORM_FEE_PCT,
        "PLATFORM_FEE_DISPLAY": FEE_DISPLAY,
        "PLATFORM_PAYPAL_EMAIL": PLATFORM_PAYPAL_EMAIL,
        "PAYPAL_MODE": PAYPAL_MODE,
        "PAYPAL_ENABLED": paypal_enabled(),
        "STRIPE_ENABLED": stripe_enabled(),
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
        paypal_email = r.pop("paypal_email", None)
        if paypal_email and "@" in paypal_email and len(paypal_email) > 3:
            parts = paypal_email.split("@")
            r["paypal_email_masked"] = f"{parts[0][:2]}***@{parts[1]}"
    return jsonify(rows)

@app.route("/api/needs")
def needs():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM needs ORDER BY created_at DESC").fetchall()]
    paid_need_ids = set(row[0] for row in conn.execute("SELECT need_id FROM stripe_payments WHERE status='paid'").fetchall())
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
        r["funded"] = (r["id"] in paid_need_ids) or not stripe_enabled()
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
    need = conn.execute("SELECT status FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return jsonify({"error": "need not found"}), 404
    if need["status"] in ("cancelled", "paid"):
        conn.close()
        return jsonify({"error": f"Job is {need['status']} and cannot be claimed"}), 400
    conn.execute("UPDATE needs SET claimed_by=?, status='claimed' WHERE id=?", (operator_id, need_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/complete", methods=["POST"])
def complete():
    data = request.json
    need_id = data["need_id"]
    conn = get_conn()
    need = conn.execute("SELECT status FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return jsonify({"error": "need not found"}), 404
    if need["status"] == "cancelled":
        conn.close()
        return jsonify({"error": "Job was cancelled and refunded - it cannot be completed"}), 400
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
    if need["status"] in ("cancelled", "paid"):
        conn.close()
        return jsonify({"error": f"Job is {need['status']} - it cannot be paid out"}), 400
    can, msg = can_payout(need_id)
    if not can:
        conn.close()
        return jsonify({"error": msg}), 400
    if stripe_enabled():
        paid_row = conn.execute("SELECT id FROM stripe_payments WHERE need_id=? AND status='paid'", (need_id,)).fetchone()
        if not paid_row:
            conn.close()
            return jsonify({"error": "Job not funded - the poster must pay via Stripe before the worker can be paid out", "stripe_required": True}), 402
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
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid1, from_id, to_id, net_hc, net_usd, "task_payout_net", need_id, f"Net payout for {need_id} - {NET_PCT_DISPLAY}", now))
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid2, from_id, "hive_platform", fee_hc, fee_usd, "platform_fee", need_id, f"Platform fee {FEE_DISPLAY} for {need_id}", now))
    conn.execute("UPDATE needs SET status='paid' WHERE id=?", (need_id,))
    conn.commit()
    # PayPal payouts
    payout_items = [
        {"email": worker_email, "amount": net_usd, "note": f"Beehive payout {net_hc} HC (${net_usd}) for {need_id} - {NET_PCT_DISPLAY} - Thank you Operator!", "type": "worker"},
        {"email": PLATFORM_PAYPAL_EMAIL, "amount": fee_usd, "note": f"Beehive platform fee {fee_hc} HC (${fee_usd}) {FEE_DISPLAY} for {need_id}", "type": "platform"}
    ]
    success, batch_id, paypal_resp, error = send_paypal_payouts(payout_items, need_id)
    if success:
        pid1 = f"pp_{uuid.uuid4().hex[:8]}"
        pid2 = f"pp_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO paypal_payouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid1, batch_id, need_id, from_id, to_id, worker_email, gross_usd, fee_usd, net_usd, "worker", "sent", json.dumps(paypal_resp), now))
        conn.execute("INSERT INTO paypal_payouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid2, batch_id, need_id, from_id, "hive_platform", PLATFORM_PAYPAL_EMAIL, gross_usd, fee_usd, fee_usd, "platform", "sent", json.dumps(paypal_resp), now))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "paypal": True, "batch_id": batch_id, "gross": amount, "gross_usd": gross_usd, "fee_hc": fee_hc, "fee_usd": fee_usd, "net_hc": net_hc, "net_usd": net_usd, "worker_email": worker_email, "platform_email": PLATFORM_PAYPAL_EMAIL, "message": f"PAID: Worker got ${net_usd} ({NET_PCT_DISPLAY}) to {worker_email}, Platform got ${fee_usd} ({FEE_DISPLAY}) to {PLATFORM_PAYPAL_EMAIL}", "paypal_response": paypal_resp})
    else:
        conn.close()
        return jsonify({"ok": True, "paypal": False, "paypal_pending": True, "gross": amount, "gross_usd": gross_usd, "fee_hc": fee_hc, "fee_usd": fee_usd, "net_hc": net_hc, "net_usd": net_usd, "worker_email": worker_email, "platform_email": PLATFORM_PAYPAL_EMAIL, "message": f"HC credited: Worker {net_hc} HC pending PayPal ${net_usd} to {worker_email}. Platform fee {fee_hc} HC pending ${fee_usd} to {PLATFORM_PAYPAL_EMAIL}. Reason: {error}", "error": error, "setup_hint": "Set PAYPAL_CLIENT_ID, PAYPAL_SECRET, PLATFORM_PAYPAL_EMAIL env vars on Render"})

# ---------------- STRIPE: FUNDING A JOB ----------------
# A job must be paid for in real USD via Stripe Checkout before the
# worker can be paid out. The platform Stripe account receives the
# gross amount; the platform fee + PayPal payout flow stays unchanged.

def fund_need_from_session(session):
    """Idempotently record a paid Stripe Checkout session and fund its need."""
    metadata = session.get("metadata") or {}
    need_id = metadata.get("need_id")
    if not need_id:
        return None
    conn = get_conn()
    need = conn.execute("SELECT * FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return None
    already = conn.execute("SELECT id FROM stripe_payments WHERE session_id=? AND status='paid'", (session["id"],)).fetchone()
    if not already:
        pid = f"pay_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO stripe_payments VALUES (?,?,?,?,?,?,?,?)", (
            pid, need_id, session["id"], session.get("payment_intent") or "",
            metadata.get("payer_id", need["posted_by"]),
            round((session.get("amount_total") or 0) / 100.0, 2), "paid",
            datetime.utcnow().isoformat()))
        if need["status"] == "open":
            conn.execute("UPDATE needs SET status='funded' WHERE id=?", (need_id,))
    conn.commit()
    conn.close()
    return need_id

@app.route("/api/create_checkout", methods=["POST"])
def create_checkout():
    data = request.json or {}
    need_id = data.get("need_id")
    conn = get_conn()
    need = conn.execute("SELECT * FROM needs WHERE id=?", (need_id,)).fetchone()
    conn.close()
    if not need:
        return jsonify({"error": "need not found"}), 404
    if not stripe_enabled():
        return jsonify({"stripe_enabled": False, "message": "Stripe not configured - job posted without upfront payment. Set STRIPE_SECRET_KEY on Render to enable payments."})
    conn = get_conn()
    already_funded = conn.execute("SELECT id FROM stripe_payments WHERE need_id=? AND status='paid'", (need_id,)).fetchone()
    conn.close()
    if already_funded:
        return jsonify({"stripe_enabled": True, "already_funded": True, "message": "This job is already funded."})
    gross_usd = round(float(need["barter_value"]) * HC_TO_USD_RATE, 2)
    cents = int(round(gross_usd * 100))
    if cents < 50:
        return jsonify({"error": f"Job value ${gross_usd} is below the Stripe minimum of $0.50 - raise the baseline"}), 400
    base_url = request.host_url.rstrip("/")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": cents,
                "product_data": {
                    "name": f"Beehive job: {need['type'] or 'Community need'}",
                    "description": (need["description"] or "Community job posting")[:300]
                }
            }
        }],
        success_url=base_url + "/?checkout=success&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=base_url + "/?checkout=cancelled",
        metadata={"need_id": need_id, "payer_id": need["posted_by"]}
    )
    return jsonify({"stripe_enabled": True, "checkout_url": session.url, "session_id": session.id, "amount_usd": gross_usd})

@app.route("/api/checkout_status")
def checkout_status():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    if not stripe_enabled():
        return jsonify({"stripe_enabled": False, "error": "Stripe not configured"}), 200
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        return jsonify({"error": f"Stripe lookup failed: {str(e)}"}), 502
    paid = session.payment_status == "paid"
    need_id = None
    if paid:
        need_id = fund_need_from_session(session)
    else:
        need_id = (session.metadata or {}).get("need_id")
    conn = get_conn()
    need = conn.execute("SELECT status FROM needs WHERE id=?", (need_id,)).fetchone() if need_id else None
    conn.close()
    return jsonify({"stripe_enabled": True, "payment_status": session.payment_status, "need_id": need_id, "need_status": need["status"] if need else None, "amount_usd": round((session.amount_total or 0) / 100.0, 2)})

@app.route("/api/stripe_webhook", methods=["POST"])
def stripe_webhook():
    # Unsigned webhooks are rejected: without the webhook secret this
    # endpoint would let anyone mark a job as funded for free. Without
    # the secret configured, funding is verified via /api/checkout_status
    # instead (which checks directly with Stripe).
    if not (stripe_enabled() and STRIPE_WEBHOOK_SECRET):
        return jsonify({"error": "webhooks not configured"}), 503
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(request.data, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": f"webhook rejected: {str(e)}"}), 400
    if event.get("type") == "checkout.session.completed":
        fund_need_from_session(event["data"]["object"])
    return jsonify({"received": True})

@app.route("/api/refund", methods=["POST"])
def refund_need():
    """Job poster cancels a funded job and gets their card refunded.
    Allowed until the work is completed - once the job is marked
    complete/attested, the money is committed to the worker."""
    data = request.json or {}
    need_id = data.get("need_id")
    requester = (data.get("requester_id") or "").strip()
    conn = get_conn()
    need = conn.execute("SELECT * FROM needs WHERE id=?", (need_id,)).fetchone()
    if not need:
        conn.close()
        return jsonify({"error": "need not found"}), 404
    need = dict(need)
    if need["status"] == "paid":
        conn.close()
        return jsonify({"error": "Job already paid out - cannot refund"}), 400
    if need["status"] == "needs_attestation":
        conn.close()
        return jsonify({"error": "Work is already completed and awaiting attestation - the money is committed to the worker"}), 400
    if requester != need["posted_by"]:
        conn.close()
        return jsonify({"error": "Only the job poster can cancel and refund this job"}), 403
    pay_row = conn.execute("SELECT * FROM stripe_payments WHERE need_id=? AND status='paid'", (need_id,)).fetchone()
    if not pay_row:
        conn.close()
        return jsonify({"error": "This job was never funded, so there is nothing to refund"}), 400
    refund_amount = float(pay_row["amount_usd"])
    refund_note = None
    if stripe_enabled() and pay_row["payment_intent"]:
        try:
            refund = stripe.Refund.create(payment_intent=pay_row["payment_intent"])
            refund_note = refund.id
        except Exception as e:
            conn.close()
            return jsonify({"error": f"Stripe refund failed: {str(e)}"}), 502
    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE stripe_payments SET status='refunded' WHERE id=?", (pay_row["id"],))
    conn.execute("UPDATE needs SET status='cancelled' WHERE id=?", (need_id,))
    lid = f"led_{uuid.uuid4().hex[:8]}"
    note = f"Refund of ${refund_amount} to poster for {need_id}" + (f" (Stripe refund {refund_note})" if refund_note else "")
    conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)", (lid, "hive_platform", need["posted_by"], 0.0, refund_amount, "stripe_refund", need_id, note, now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": f"Refunded ${refund_amount} to your card. Job {need_id} is cancelled."})

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
    if op_id == "hive_platform":
        return jsonify({"error": "The platform account cannot be logged into from the website"}), 403
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
    result.pop("paypal_email", None)
    try:
        result["pools"] = json.loads(result.get("pools","[]"))
    except:
        pass
    result["paypal_configured"] = paypal_enabled()
    result["platform_fee"] = FEE_DISPLAY
    return jsonify(result)

if __name__ == "__main__":
    print("BEEHIVE RUNNING WITH REAL PAYPAL PAYOUTS")
    print(f"Fee: {PLATFORM_FEE_PCT*100}% -> {PLATFORM_PAYPAL_EMAIL}")
    print(f"Rate: 1 HC = ${HC_TO_USD_RATE}")
    print(f"PayPal Mode: {PAYPAL_MODE} - Enabled: {paypal_enabled()}")
    print(f"Stripe: {'ENABLED' if stripe_enabled() else 'not configured (jobs post without payment)'}")
    app.run(host="0.0.0.0", port=5000, debug=True)
