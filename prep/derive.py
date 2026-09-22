"""Derive card_id + device_profile, build investigation views, export slim CSVs for TigerGraph.

card_id rule was recovered empirically, not guessed: ranking a customer's distinct
(card1, card6) tuples ascending with NULLs first reproduces 4665/4665 card_ids in
closed_cases_history.csv and 20/20 in case_pack.csv. See prep/validate_card_id.py.
"""
import duckdb, pathlib

con = duckdb.connect("build/fraud.db")
con.execute("SET preserve_insertion_order=false")

# --- card identity -----------------------------------------------------------
con.execute("""
CREATE OR REPLACE TABLE card_key AS
SELECT customer_id, card1, card6,
       customer_id || '-K' || row_number() OVER (
         PARTITION BY customer_id ORDER BY card1 ASC NULLS FIRST, card6 ASC NULLS FIRST
       ) AS card_id
FROM (SELECT DISTINCT customer_id, card1, card6 FROM txn)
""")

# --- transaction fact table (slim: investigation-relevant columns only) -------
con.execute("""
CREATE OR REPLACE TABLE tx AS
SELECT
  t.TransactionID::BIGINT            AS txn_id,
  k.card_id,
  t.customer_id,
  t.ts::TIMESTAMP                    AS ts,
  t.TransactionAmt::DOUBLE           AS amount,
  t.ProductCD                        AS product_cd,
  t.channel,
  t.risk_score::DOUBLE               AS risk_score,
  t.card1, t.card2, t.card3, t.card4, t.card5, t.card6,
  t.addr1, t.addr2, t.dist1, t.dist2,
  t.P_emaildomain                    AS p_email,
  t.R_emaildomain                    AS r_email,
  t.C1,t.C2,t.C3,t.C4,t.C5,t.C6,t.C7,t.C8,t.C9,t.C10,t.C11,t.C12,t.C13,t.C14,
  t.D1,t.D2,t.D3,t.D4,t.D5,t.D6,t.D7,t.D8,t.D9,t.D10,t.D11,t.D12,t.D13,t.D14,t.D15,
  t.M1,t.M2,t.M3,t.M4,t.M5,t.M6,t.M7,t.M8,t.M9,
  i.DeviceType                       AS device_type,
  i.DeviceInfo                       AS device_info,
  i.id_12,i.id_15,i.id_16,i.id_23,i.id_28,i.id_29,i.id_30,i.id_31,i.id_33,i.id_34,i.id_35,i.id_36,i.id_37,i.id_38,
  i.id_01,i.id_02,i.id_05,i.id_06,i.id_11,
  -- device profile as defined by the task: DeviceInfo | OS | browser | screen
  CASE WHEN i.TransactionID IS NULL THEN NULL ELSE
    coalesce(i.DeviceInfo,'unknown') || ' | ' || coalesce(i.id_30,'unknown') || ' | ' ||
    coalesce(i.id_31,'unknown')      || ' | ' || coalesce(i.id_33,'unknown')
  END                                AS device_profile
FROM txn t
JOIN card_key k
  ON k.customer_id = t.customer_id
 AND k.card1 IS NOT DISTINCT FROM t.card1
 AND k.card6 IS NOT DISTINCT FROM t.card6
LEFT JOIN identity i ON i.TransactionID = t.TransactionID
""")
con.execute("CREATE INDEX IF NOT EXISTS tx_card ON tx(card_id)")
con.execute("CREATE INDEX IF NOT EXISTS tx_cust ON tx(customer_id)")
con.execute("CREATE INDEX IF NOT EXISTS tx_id   ON tx(txn_id)")
con.execute("CREATE INDEX IF NOT EXISTS tx_dev  ON tx(device_profile)")
con.execute("CREATE INDEX IF NOT EXISTS tx_ts   ON tx(ts)")

# --- per-card behavioural baseline (what "normal" looks like for this card) ---
con.execute("""
CREATE OR REPLACE TABLE card_profile AS
SELECT card_id, customer_id,
       count(*) AS n_txns, min(ts) AS first_ts, max(ts) AS last_ts,
       avg(amount) AS amt_mean, median(amount) AS amt_median,
       quantile_cont(amount, 0.95) AS amt_p95, max(amount) AS amt_max,
       sum(amount) AS amt_total,
       count(DISTINCT addr1) AS n_regions,
       count(DISTINCT product_cd) AS n_products,
       count(DISTINCT device_profile) AS n_devices,
       sum(CASE WHEN channel='online' THEN 1 ELSE 0 END)::DOUBLE / count(*) AS online_share
FROM tx GROUP BY card_id, customer_id
""")

pathlib.Path("build/export").mkdir(parents=True, exist_ok=True)
for name, sql in {
    "customer": "SELECT DISTINCT customer_id FROM tx",
    "card":     "SELECT card_id, customer_id, any_value(card4) AS network, any_value(card6) AS card_type FROM tx GROUP BY card_id, customer_id",
    "device":   "SELECT DISTINCT device_profile FROM tx WHERE device_profile IS NOT NULL",
    "region":   "SELECT DISTINCT addr1 AS region_id FROM tx WHERE addr1 IS NOT NULL",
    "email":    "SELECT DISTINCT p_email AS domain FROM tx WHERE p_email IS NOT NULL",
}.items():
    con.execute(f"COPY ({sql}) TO 'build/export/{name}.csv' (HEADER, DELIMITER ',')")

print("tx rows        ", con.sql("SELECT count(*) FROM tx").fetchone()[0])
print("cards          ", con.sql("SELECT count(*) FROM card_key").fetchone()[0])
print("device profiles", con.sql("SELECT count(DISTINCT device_profile) FROM tx WHERE device_profile IS NOT NULL").fetchone()[0])
con.close()
