import sqlite3
import os
import json
from flask import Flask, request, jsonify, render_template
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "hive.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html') if os.path.exists(os.path.join(app.root_path, 'templates', 'index.html')) else "The Beehive Engine is Active."

@app.route('/api/balance/<account_id>', methods=['GET'])
def get_balance(account_id):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
    res = c.fetchone()
    conn.close()
    if not res:
        return jsonify({"error": "Account not found"}), 404
    return jsonify(dict(res))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
