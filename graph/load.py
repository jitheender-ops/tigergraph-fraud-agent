#!/usr/bin/env python
"""Create the schema, load the data and install the queries in TigerGraph.

  uv run python graph/load.py --schema      create graph + vertices + edges
  uv run python graph/load.py --data        export CSVs and upsert them
  uv run python graph/load.py --docs        load the GraphRAG corpus into the vector store
  uv run python graph/load.py --queries     install graph/queries.gsql
  uv run python graph/load.py --all
"""
from __future__ import annotations
import argparse, os, pathlib, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import duckdb
from tg import connect, GRAPH

HERE = pathlib.Path(__file__).parent
EXPORT = pathlib.Path("build/tg")
BATCH = 50_000


def run_gsql(conn, text, label):
    print(f"-- {label} ...", flush=True)
    out = conn.gsql(text)
    print(out[-2000:] if isinstance(out, str) else out)
    return out


def do_schema(conn):
    run_gsql(conn, (HERE / "schema.gsql").read_text(), "schema")


def do_queries(conn):
    run_gsql(conn, (HERE / "queries.gsql").read_text(), "queries")


def export():
    """Flatten the derived tables into the CSVs the loader upserts."""
    EXPORT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect("build/fraud.db", read_only=True)
    jobs = {
        "customer": "SELECT DISTINCT customer_id FROM tx",
        "card": """SELECT card_id, customer_id, coalesce(any_value(card4),'') AS network,
                          coalesce(any_value(card6),'') AS card_type
                   FROM tx GROUP BY card_id, customer_id""",
        # n_cards is what ring_component filters each hop on, so it has to be an
        # attribute rather than a count recomputed inside the BFS loop.
        "device": """SELECT device_profile, count(DISTINCT card_id) AS n_cards FROM tx
                     WHERE device_profile IS NOT NULL GROUP BY 1""",
        "email": "SELECT DISTINCT p_email FROM tx WHERE p_email IS NOT NULL",
        "region": "SELECT DISTINCT CAST(addr1 AS VARCHAR) AS region_id FROM tx WHERE addr1 IS NOT NULL",
        "transaction": """
            SELECT txn_id, card_id, customer_id, strftime(ts,'%Y-%m-%d %H:%M:%S') AS ts,
                   amount, coalesce(product_cd,'') AS product_cd, channel, risk_score,
                   coalesce(CAST(addr1 AS VARCHAR),'') AS addr1,
                   coalesce(CAST(addr2 AS VARCHAR),'') AS addr2,
                   coalesce(dist1, 0) AS dist1,
                   coalesce(p_email,'') AS p_email, coalesce(r_email,'') AS r_email,
                   coalesce(device_profile,'') AS device_profile,
                   coalesce(device_type,'') AS device_type,
                   coalesce(id_15,'') AS id_15, coalesce(id_23,'') AS id_23,
                   coalesce(id_30,'') AS id_30, coalesce(id_31,'') AS id_31,
                   coalesce(id_33,'') AS id_33,
                   concat_ws(',',M1,M2,M3,M4,M5,M6,M7,M8,M9) AS m_flags,
                   concat_ws(',',C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14) AS c_counts,
                   concat_ws(',',D1,D2,D3,D4,D5,D6,D7,D8,D9,D10,D11,D12,D13,D14,D15) AS d_deltas
            FROM tx""",
        "closed_case": """
            SELECT case_id, customer_id, card_id,
                   strftime(opened_at::TIMESTAMP,'%Y-%m-%d %H:%M:%S') AS opened_at,
                   strftime(closed_at::TIMESTAMP,'%Y-%m-%d %H:%M:%S') AS closed_at,
                   outcome, pattern, coalesce(first_fraud_txn_id::VARCHAR,'') AS first_fraud_txn_id,
                   n_txns, exposure_usd, coalesce(actions_taken,'') AS actions_taken,
                   coalesce(report_filed::VARCHAR,'') AS report_filed,
                   coalesce(analyst_notes,'') AS analyst_notes
            FROM closed_case""",
        # edges
        "e_owns": "SELECT DISTINCT customer_id, card_id FROM tx",
        "e_made": "SELECT card_id, txn_id FROM tx",
        "e_device": "SELECT txn_id, device_profile FROM tx WHERE device_profile IS NOT NULL",
        "e_email": "SELECT txn_id, p_email FROM tx WHERE p_email IS NOT NULL",
        "e_region": "SELECT txn_id, CAST(addr1 AS VARCHAR) AS region_id FROM tx WHERE addr1 IS NOT NULL",
        "e_next": """SELECT txn_id AS src, nxt AS dst, gap FROM (
                       SELECT txn_id, lead(txn_id) OVER (PARTITION BY card_id ORDER BY ts) AS nxt,
                              date_diff('second', ts, lead(ts) OVER (PARTITION BY card_id ORDER BY ts)) AS gap
                       FROM tx) WHERE nxt IS NOT NULL""",
        "e_case_txn": """SELECT c.case_id, TRY_CAST(u.tid AS BIGINT) AS txn_id
                         FROM closed_case c, unnest(str_split(c.txn_ids,'|')) AS u(tid)
                         WHERE TRY_CAST(u.tid AS BIGINT) IS NOT NULL""",
        "e_case_card": "SELECT case_id, card_id FROM closed_case",
        "e_case_conn": """SELECT c.case_id, trim(u.cid) AS card_id
                          FROM closed_case c, unnest(str_split(c.connected_card_ids,'|')) AS u(cid)
                          WHERE c.connected_card_ids IS NOT NULL AND trim(u.cid) <> ''""",
    }
    for name, sql in jobs.items():
        path = EXPORT / f"{name}.csv"
        con.execute(f"COPY ({sql}) TO '{path}' (HEADER, DELIMITER ',')")
        print(f"  {name:14} -> {path}")
    con.close()


def upsert(conn, name, vtype=None, etype=None, attrs=None, src=None, tgt=None):
    """Stream a CSV in batches through the REST upsert endpoint."""
    import csv
    path = EXPORT / f"{name}.csv"
    t0, n = time.time(), 0
    with open(path) as fh:
        rdr = csv.DictReader(fh)
        batch = []
        for row in rdr:
            batch.append(row)
            if len(batch) >= BATCH:
                n += _flush(conn, batch, vtype, etype, attrs, src, tgt)
                batch = []
        if batch:
            n += _flush(conn, batch, vtype, etype, attrs, src, tgt)
    print(f"  {name:14} {n:>9,} rows in {time.time()-t0:.1f}s")


def _flush(conn, batch, vtype, etype, attrs, src, tgt):
    if vtype:
        data = {r[attrs[0]]: {k: (v if v != "" else None) for k, v in r.items() if k != attrs[0]}
                for r in batch}
        data = {k: {a: {"value": v} for a, v in d.items() if v is not None}
                for k, d in data.items()}
        conn.upsertVertices(vtype, [(k, v) for k, v in data.items()])
    else:
        edges = [(r[src[1]], r[tgt[1]], {}) for r in batch]
        conn.upsertEdges(src[0], etype, tgt[0], edges)
    return len(batch)


def do_docs(conn):
    """Push the GraphRAG corpus into TigerGraph's vector store.

    prep/build_corpus.py chunks and embeds; this only uploads. The vectors are already
    L2-normalised, which is what the COSINE metric on the vector attribute expects.
    Small enough (309 chunks) to go in one upsert.
    """
    import json

    import numpy as np
    vec_path, chunk_path = pathlib.Path("build/corpus.npz"), pathlib.Path("build/corpus.json")
    if not vec_path.exists():
        sys.exit("build/corpus.npz is missing. Run: uv run python prep/build_corpus.py")
    vecs = np.load(vec_path)["vectors"]
    chunks = json.loads(chunk_path.read_text())
    rows = [(c["doc_id"], {"source": c["source"], "section": c["section"],
                           "title": c["title"], "text": c["text"],
                           "embedding": vecs[c["idx"]].tolist()})
            for c in chunks]
    t0 = time.time()
    conn.upsertVertices("DocChunk", rows)
    print(f"  DocChunk       {len(rows):>9,} chunks in {time.time()-t0:.1f}s "
          f"({vecs.shape[1]}-dim)")


def do_data(conn):
    export()
    print("-- upserting vertices")
    upsert(conn, "customer", vtype="Customer", attrs=["customer_id"])
    upsert(conn, "card", vtype="Card", attrs=["card_id"])
    upsert(conn, "device", vtype="DeviceProfile", attrs=["device_profile"])
    upsert(conn, "email", vtype="EmailDomain", attrs=["p_email"])
    upsert(conn, "region", vtype="BillingRegion", attrs=["region_id"])
    upsert(conn, "transaction", vtype="Transaction", attrs=["txn_id"])
    upsert(conn, "closed_case", vtype="ClosedCase", attrs=["case_id"])
    print("-- upserting edges")
    upsert(conn, "e_owns", etype="OWNS", src=("Customer", "customer_id"), tgt=("Card", "card_id"))
    upsert(conn, "e_made", etype="MADE", src=("Card", "card_id"), tgt=("Transaction", "txn_id"))
    upsert(conn, "e_device", etype="FROM_DEVICE", src=("Transaction", "txn_id"), tgt=("DeviceProfile", "device_profile"))
    upsert(conn, "e_email", etype="PURCHASER_EMAIL", src=("Transaction", "txn_id"), tgt=("EmailDomain", "p_email"))
    upsert(conn, "e_region", etype="BILLED_IN", src=("Transaction", "txn_id"), tgt=("BillingRegion", "region_id"))
    upsert(conn, "e_next", etype="NEXT", src=("Transaction", "src"), tgt=("Transaction", "dst"))
    upsert(conn, "e_case_txn", etype="INVOLVES", src=("ClosedCase", "case_id"), tgt=("Transaction", "txn_id"))
    upsert(conn, "e_case_card", etype="ON_CARD", src=("ClosedCase", "case_id"), tgt=("Card", "card_id"))
    upsert(conn, "e_case_conn", etype="CONNECTED_TO", src=("ClosedCase", "case_id"), tgt=("Card", "card_id"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--data", action="store_true")
    ap.add_argument("--docs", action="store_true")
    ap.add_argument("--queries", action="store_true")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.export_only:
        return export()
    conn = connect()
    if a.all or a.schema:
        do_schema(conn)
    if a.all or a.data:
        do_data(conn)
    if a.all or a.docs:
        do_docs(conn)
    if a.all or a.queries:
        do_queries(conn)
    print("\nvertex counts:", conn.getVertexCount("*"))


if __name__ == "__main__":
    main()
