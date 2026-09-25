from ledger import get_conn
from collections import Counter

def check_attestation_threshold(need):
    return 3 if float(need["desirability"]) <= 0.3 else 2

def get_attestations(need_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM attestations WHERE need_id=?", (need_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def is_collusion(need_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT attester_id, COUNT(*) as cnt FROM attestations GROUP BY attester_id HAVING cnt > 3")
    flagged = [dict(r) for r in c.fetchall()]
    conn.close()
    return len(flagged) > 0, flagged

def can_payout(need_id):
    from ledger import get_conn
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM needs WHERE id=?", (need_id,))
    need = c.fetchone()
    conn.close()
    if not need:
        return False, "Need not found"
    need = dict(need)
    atts = get_attestations(need_id)
    thresh = check_attestation_threshold(need)
    collusion, flagged = is_collusion(need_id)
    if collusion:
        return False, f"Collusion flag: {flagged} - requires external attestation"
    if len(atts) >= thresh:
        return True, f"{len(atts)}/{thresh} attestations OK"
    return False, f"Need {thresh} attestations, have {len(atts)}"
