import sqlite3, os, time, json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "hive.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    # Operators: people
    c.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id TEXT PRIMARY KEY,
        name TEXT,
        pools TEXT,
        hc_balance REAL DEFAULT 100.0,
        zone TEXT,
        created_at TEXT
    )""")
    # Needs: critical bottom lines
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
    # Reputation events / attestations
    c.execute("""
    CREATE TABLE IF NOT EXISTS attestations (
        id TEXT PRIMARY KEY,
        need_id TEXT,
        attester_id TEXT,
        operator_id TEXT,
        created_at TEXT,
        FOREIGN KEY(need_id) REFERENCES needs(id)
    )""")
    # Ledger: all HC moves - this IS the file ledger
    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY,
        from_id TEXT,
        to_id TEXT,
        amount REAL,
        type TEXT,
        need_id TEXT,
        note TEXT,
        created_at TEXT
    )""")
    conn.commit()

    # Seed if empty
    c.execute("SELECT COUNT(*) as cnt FROM operators")
    if c.fetchone()["cnt"] == 0:
        now = datetime.utcnow().isoformat()
        ops = [
            ("drew", "DREW", '["complex_systems_logic","ecommerce_ops","logistics_code","media_engineering"]', 150.0, "46250", now),
            ("josh", "JOSH", '["logistics_routing","warehouse_ops"]', 120.0, "46250", now),
            ("neighbor_1", "NEIGHBOR_1", '["food_security","real_world_ops"]', 100.0, "46250", now),
        ]
        c.executemany("INSERT INTO operators VALUES (?,?,?,?,?,?)", ops)
        
        needs = [
            ("need_001", "infrastructure", "Fix drainage behind 86th St units - universally disliked", 9, 0.1, 10, 90.0, "46250", '["real_world_ops"]', "open", "neighbor_1", None, now),
            ("need_002", "food_security", "Food delivery for 5222 E 86th Apt 308 - baseline secured", 8, 0.6, 10, 13.33, "46250", '["logistics_routing"]', "open", "drew", None, now),
            ("need_003", "utilities", "Help setup local Beehive node on RTX 4050 laptop", 7, 0.8, 10, 8.75, "46250", '["complex_systems_logic"]', "open", "josh", None, now),
        ]
        c.executemany("INSERT INTO needs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", needs)
        conn.commit()
    conn.close()

def barter_value(baseline, desirability, urgency):
    d = max(0.1, float(desirability))
    return round(float(baseline) / d * float(urgency), 2)

if __name__ == "__main__":
    init_db()
    print(f"Ledger initialized at {DB_PATH}")
