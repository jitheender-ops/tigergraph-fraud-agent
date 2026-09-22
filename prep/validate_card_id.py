"""The card_id derivation is load-bearing: every ID in every answer file depends on it.
Ground truth = the 4665 card_ids in closed_cases_history.csv + 20 in case_pack.csv."""
import duckdb, sys

con = duckdb.connect("build/fraud.db", read_only=True)

def matched(src, tid):
    return con.sql(f"""
      SELECT count(*), sum(CASE WHEN x.card_id = k.card_id THEN 1 ELSE 0 END)
      FROM {src} x JOIN tx t ON t.txn_id = x.{tid}
      JOIN card_key k ON k.customer_id = t.customer_id
        AND k.card1 IS NOT DISTINCT FROM t.card1 AND k.card6 IS NOT DISTINCT FROM t.card6
    """).fetchone()

n_cc, m_cc = matched("closed_case", "first_fraud_txn_id")
n_cp, m_cp = matched("case_pack", "flagged_txn_id")
print(f"closed cases {m_cc}/{n_cc}   case pack {m_cp}/{n_cp}")

assert m_cc == n_cc, f"card_id rule broke on closed cases: {m_cc}/{n_cc}"
assert m_cp == n_cp == 20, f"card_id rule broke on case pack: {m_cp}/{n_cp}"
assert con.sql("SELECT count(*) FROM tx").fetchone()[0] == 590742, "lost transactions in derive"
print("OK")
