import sqlite3, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "hive.db")
HC_TO_USD_RATE = 1.0
PLATFORM_FEE_PCT = 0.014

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id TEXT PRIMARY KEY,
        name TEXT,
        pools TEXT,
        hc_balance REAL DEFAULT 100.0,
        usd_earned REAL DEFAULT 0.0,
        zone TEXT,
        created_at TEXT,
        password TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS needs (
        id TEXT PRIMARY KEY,
        type TEXT,
        description TEXT,
        urgency REAL,
        desirability REAL,
        baseline REAL,
        barter_value REAL,
        zone TEXT,
        required_pools TEXT,
        status TEXT,
        posted_by TEXT,
        claimed_by TEXT,
        created_at TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY,
        from_id TEXT,
        to_id TEXT,
        amount REAL,
        usd_amount REAL,
        type TEXT,
        need_id TEXT,
        note TEXT,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

def calc_fee(amount):
    fee_hc = round(float(amount) * PLATFORM_FEE_PCT, 4)
    fee_usd = round(fee_hc * HC_TO_USD_RATE, 2)
    net_hc = round(float(amount) - fee_hc, 4)
    return fee_hc, fee_usd, net_hc
