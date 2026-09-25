import sqlite3, os, json
from datetime import datetime
DB_PATH = os.path.join(os.path.dirname(__file__), "hive.db")
HC_TO_USD_RATE = 1.0
PLATFORM_FEE_PCT = 0.014
PLATFORM_PAYPAL_EMAIL = os.getenv("PLATFORM_PAYPAL_EMAIL", "drewdude1994@gmail.com")
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_conn(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS operators (id TEXT PRIMARY KEY, name TEXT, pools TEXT, hc_balance REAL DEFAULT 100.0, usd_earned REAL DEFAULT 0.0, zone TEXT, password TEXT DEFAULT '', created_at TEXT)""")
    for col, default in [("name","TEXT"),("pools","TEXT DEFAULT '[\"real_world_ops\"]'"),("hc_balance","REAL DEFAULT 100.0"),("usd_earned","REAL DEFAULT 0.0"),("zone","TEXT DEFAULT '46250'"),("password","TEXT DEFAULT ''"),("paypal_email","TEXT DEFAULT ''"),("paypal_verified","INTEGER DEFAULT 0"),("created_at","TEXT")]:
        try: c.execute(f"ALTER TABLE operators ADD COLUMN {col} {default}")
        except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS needs (id TEXT PRIMARY KEY, type TEXT, description TEXT, urgency REAL, desirability REAL, baseline REAL, barter_value REAL, zone TEXT, required_pools TEXT, status TEXT, posted_by TEXT, claimed_by TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS attestations (id TEXT PRIMARY KEY, need_id TEXT, attester_id TEXT, operator_id TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ledger (id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, amount REAL, usd_amount REAL, type TEXT, need_id TEXT, note TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS paypal_payouts (id TEXT PRIMARY KEY, batch_id TEXT, need_id TEXT, from_id TEXT, to_id TEXT, to_email TEXT, gross_amount REAL, fee_amount REAL, net_amount REAL, type TEXT, status TEXT, paypal_response TEXT, created_at TEXT)""")
    conn.commit()
    platform = c.execute("SELECT id FROM operators WHERE id='hive_platform'").fetchone()
    if not platform:
        now = datetime.utcnow().isoformat()
        c.execute("INSERT INTO operators (id, name, pools, hc_balance, usd_earned, zone, password, paypal_email, created_at) VALUES (?,?,?,?,?,?,?,?,?)", ("hive_platform", "Beehive Platform - Drew", json.dumps(["platform"]), 0.0, 0.0, "46250", "", PLATFORM_PAYPAL_EMAIL, now))
        conn.commit()
    else:
        c.execute("UPDATE operators SET paypal_email=? WHERE id='hive_platform'", (PLATFORM_PAYPAL_EMAIL,))
        conn.commit()
    conn.close()
def barter_value(baseline, desirability, urgency):
    d = max(0.1, float(desirability)); return round(float(baseline) / d * float(urgency), 2)
def calc_fee(amount):
    fee_hc = round(float(amount) * PLATFORM_FEE_PCT, 4); fee_usd = round(fee_hc * HC_TO_USD_RATE, 2); net_hc = round(float(amount) - fee_hc, 4); return fee_hc, fee_usd, net_hc
