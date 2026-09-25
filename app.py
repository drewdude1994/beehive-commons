import os, sqlite3
from flask import Flask, render_template, request, jsonify, send_from_directory
from ledger import init_db, get_conn, barter_value, calc_fee

app = Flask(__name__)
init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/boomerang.mp4')
def boomerang_alias():
    return '', 204

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    name = data.get('name', '').strip()
    password = data.get('password', '').strip()
    conn = get_conn()
    op = conn.execute("SELECT * FROM operators WHERE name = ? AND password = ?", (name, password)).fetchone()
    conn.close()
    if op:
        return jsonify({"success": True, "operator": dict(op)})
    return jsonify({"success": False, "error": "Invalid credentials"})

@app.route('/api/create_operator', methods=['POST'])
def api_create_operator():
    data = request.json or {}
    name = data.get('name', '').strip()
    paypal = data.get('paypal_email', '').strip()
    password = data.get('password', '').strip()
    if not name or not password:
        return jsonify({"success": False, "error": "Name and password required"})
    
    conn = get_conn()
    try:
        conn.execute("INSERT INTO operators (id, name, pools, hc_balance, usd_earned, zone, created_at, password) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)",
                     (name, name, "general", 100.0, 0.0, "Indianapolis", password))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)})
    conn.close()
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
