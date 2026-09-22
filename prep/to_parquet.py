"""One-time: raw CSVs -> Parquet + DuckDB. Everything downstream reads build/fraud.db."""
import duckdb, pathlib

DB = "build/fraud.db"
pathlib.Path("build").mkdir(exist_ok=True)
con = duckdb.connect(DB)
con.execute("SET preserve_insertion_order=false")

con.execute("""
CREATE OR REPLACE TABLE txn AS
SELECT * FROM read_csv('data/transactions.csv', header=true, sample_size=-1, ignore_errors=true)
""")
con.execute("""
CREATE OR REPLACE TABLE identity AS
SELECT * FROM read_csv('data/identity.csv', header=true, sample_size=-1, ignore_errors=true)
""")
con.execute("""
CREATE OR REPLACE TABLE closed_case AS
SELECT * FROM read_csv('data/closed_cases_history.csv', header=true, sample_size=-1, ignore_errors=true)
""")
con.execute("""
CREATE OR REPLACE TABLE case_pack AS
SELECT * FROM read_csv('data/case_pack.csv', header=true, sample_size=-1, ignore_errors=true)
""")
for t in ("txn", "identity", "closed_case", "case_pack"):
    print(t, con.sql(f"SELECT count(*) FROM {t}").fetchone()[0])
con.close()
