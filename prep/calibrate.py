"""Measure each signal's likelihood ratio on the closed cases and print the log-odds
weight it justifies. The weights in agent/patterns.py come from here, not from taste.

LR = P(condition | confirmed_fraud) / P(condition | cleared); weight = ln(LR).
Laplace-smoothed, because some conditions are empty in one class.
"""
import duckdb, math, sys
sys.path.insert(0, "agent")
from features import FEATURE_SQL

con = duckdb.connect("build/fraud.db", read_only=True)
con.execute("""CREATE OR REPLACE TEMP TABLE anchors AS
  SELECT case_id AS key_id,
         coalesce(first_fraud_txn_id::VARCHAR, split_part(txn_ids,'|',1))::BIGINT AS txn_id
  FROM closed_case""")
con.execute(f"CREATE OR REPLACE TEMP TABLE feat AS {FEATURE_SQL}")
df = con.sql("""SELECT f.*, cc.outcome FROM feat f
                JOIN closed_case cc ON cc.case_id = f.key_id""").df()

fraud = df[df.outcome == "confirmed_fraud"]
clear = df[df.outcome == "cleared"]

CONDS = {
  "m_flags_2plus":      lambda d: d.m_false_n >= 2,
  "m_flags_1":          lambda d: d.m_false_n == 1,
  "risk_high_085":      lambda d: d.risk_score >= 0.85,
  "risk_low_030":       lambda d: d.risk_score <= 0.30,
  "dev_new":            lambda d: d.dev_new == 1,
  "dev_new_corrob":     lambda d: (d.dev_new == 1) & ((d.m_false_n >= 1) | (d.proxy == 1)),
  "dev_rare_le20":      lambda d: (d.dev_specific == 1) & (d.dev_cards <= 20) & (d.dev_cards > 0),
  "dev_common_150plus": lambda d: d.dev_cards > 150,
  "proxy":              lambda d: d.proxy == 1,
  "region_new_mflag":   lambda d: (d.prior_in_region == 0) & (d.m_false_n >= 1),
  "region_new_clean":   lambda d: (d.prior_in_region == 0) & (d.m_false_n == 0),
  "recurring_monthly":  lambda d: (d.same_amount_months >= 3) & (d.same_amount_n >= 3)
                                  & (d.same_amount_gap_days >= 20) & (d.same_amount_gap_days <= 45),
  "small_auths_3plus":  lambda d: d.small_auths_24h >= 3,
  "amount_outlier_4x":  lambda d: (d.amt_vs_median >= 4) & (d.amt_over_p95 == 1),
  "channel_odd":        lambda d: d.channel_odd == 1,
  "burst_10plus":       lambda d: d.burst_48h >= 10,
}

print(f"{'condition':22} {'P(c|fraud)':>11} {'P(c|clear)':>11} {'LR':>8} {'weight':>8}   n_fraud n_clear")
print("-" * 86)
for name, fn in CONDS.items():
    nf, nc = int(fn(fraud).sum()), int(fn(clear).sum())
    pf = (nf + 0.5) / (len(fraud) + 1)
    pc = (nc + 0.5) / (len(clear) + 1)
    lr = pf / pc
    print(f"{name:22} {pf:>11.4f} {pc:>11.4f} {lr:>8.2f} {math.log(lr):>8.2f}   {nf:>7} {nc:>7}")

# --- the sharper, targeted conditions the marginal ones above do not capture ----------
print()
print("== targeted conditions ==")
con.execute("""CREATE OR REPLACE TEMP TABLE ring AS
  SELECT f.key_id,
         (SELECT count(DISTINCT x.card_id) FROM tx x
          WHERE x.device_profile = f.device_profile
            AND x.ts BETWEEN f.ts - INTERVAL 30 DAY AND f.ts) AS cards_in_window
  FROM feat f WHERE f.device_profile IS NOT NULL AND f.dev_cards <= 80""")
r = con.sql("""SELECT cc.outcome,
   count(*) AS n,
   sum(CASE WHEN g.cards_in_window >= 3 THEN 1 ELSE 0 END) AS ring3
  FROM ring g JOIN closed_case cc ON cc.case_id = g.key_id GROUP BY 1""").df()
print(r.to_string())
nf_all, nc_all = len(fraud), len(clear)
row = {x.outcome: x for _, x in r.iterrows()}
if "confirmed_fraud" in row and "cleared" in row:
    pf = (row["confirmed_fraud"].ring3 + 0.5) / (nf_all + 1)
    pc = (row["cleared"].ring3 + 0.5) / (nc_all + 1)
    print(f"rare-device ring >=3 cards/30d : LR {pf/pc:.2f}  weight {math.log(pf/pc):+.2f}")

# episode coherence: does the closed case span more than one transaction?
ep = con.sql("""SELECT outcome,
   avg(CASE WHEN n_txns >= 3 THEN 1.0 ELSE 0 END) AS p_3plus,
   avg(n_txns) AS avg_txns FROM closed_case GROUP BY 1""").df()
print()
print(ep.to_string())

# --- case memory: what an earlier closed case on the same card or device is worth -------
# prior_fraud / prior_cleared / device_prior_fraud were set by hand. Each case is scored
# only against cases closed BEFORE it opened, exactly as the agent retrieves them.
print()
print("== case memory ==")
mem = con.sql("""
  SELECT a.case_id, a.outcome,
    EXISTS (SELECT 1 FROM closed_case b WHERE b.card_id = a.card_id
            AND b.opened_at < a.opened_at AND b.outcome = 'confirmed_fraud') AS card_fraud,
    EXISTS (SELECT 1 FROM closed_case b WHERE b.card_id = a.card_id
            AND b.opened_at < a.opened_at AND b.outcome = 'cleared') AS card_cleared,
    EXISTS (SELECT 1 FROM feat f JOIN closed_case b ON b.opened_at < a.opened_at
              AND b.outcome = 'confirmed_fraud' AND b.card_id <> a.card_id
            JOIN tx t ON t.txn_id = TRY_CAST(split_part(b.txn_ids, '|', 1) AS BIGINT)
            WHERE f.key_id = a.case_id AND f.dev_specific = 1 AND f.dev_cards <= 50
              AND t.device_profile = f.device_profile) AS device_fraud
  FROM closed_case a""").df()
MEMORY = {}
for name, col in (("prior_fraud", "card_fraud"), ("prior_cleared", "card_cleared"),
                  ("device_prior_fraud", "device_fraud")):
    nf = int(mem[mem.outcome == "confirmed_fraud"][col].sum())
    nc = int(mem[mem.outcome == "cleared"][col].sum())
    pf, pc = (nf + 0.5) / (len(fraud) + 1), (nc + 0.5) / (len(clear) + 1)
    MEMORY[name] = math.log(pf / pc)
    print(f"{name:22} {pf:>11.4f} {pc:>11.4f} {pf/pc:>8.2f} {math.log(pf/pc):>8.2f}   {nf:>7} {nc:>7}")
