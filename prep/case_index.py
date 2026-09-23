"""Build build/case_index.npz: one feature vector per closed case, for agent/similar.py.

  uv run python prep/case_index.py
"""
import sys
sys.path.insert(0, "agent")

import duckdb, numpy as np
from features import FEATURE_SQL
import similar

con = duckdb.connect("build/fraud.db", read_only=True)
con.execute("""CREATE OR REPLACE TEMP TABLE anchors AS
  SELECT case_id AS key_id,
         coalesce(first_fraud_txn_id::VARCHAR, split_part(txn_ids,'|',1))::BIGINT AS txn_id
  FROM closed_case""")
con.execute(f"CREATE OR REPLACE TEMP TABLE feat AS {FEATURE_SQL}")
df = con.sql("""SELECT f.*, cc.outcome, cc.pattern, cc.opened_at
                FROM feat f JOIN closed_case cc ON cc.case_id = f.key_id
                ORDER BY cc.case_id""").df()
raw = np.array([similar.vector(r) for r in df.to_dict("records")])
mean, std = raw.mean(axis=0), raw.std(axis=0)
std[std == 0] = 1.0
np.savez(similar.INDEX, vecs=(raw - mean) / std, mean=mean, std=std,
         case_ids=df.key_id.to_numpy(), outcomes=df.outcome.to_numpy(),
         patterns=df.pattern.to_numpy(),
         opened_at=df.opened_at.to_numpy().astype("datetime64[s]"))
print(f"{similar.INDEX}: {len(df):,} closed cases x {raw.shape[1]} features")
