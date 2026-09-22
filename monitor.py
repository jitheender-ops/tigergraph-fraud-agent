#!/usr/bin/env python
"""Self-directed monitoring: the agent opening its own cases.

The twenty benchmark cases all arrive from a trigger somebody else pulled -- a model
score, a cardholder, an analyst. A fraud agent that only answers the doorbell misses
everything nobody thought to flag, and the dataset says plainly that not every pattern
in it is documented.

This is the other half. Connected components over the device-sharing graph (prep/rings.py)
produce a few dozen bounded components: small groups of cards tied together transitively
through device profiles that are specific enough to describe a machine. The large ones
are worthless -- transitive device sharing percolates into one giant component at every
threshold tested -- but the bounded tail is exactly the shape a ring has, and against
the closed history a 4-10 card component runs 15 confirmed fraud to 2 cleared.

The sweep ranks those components by money moved in the exam window, discards any that
touch a benchmark card (this is meant to be work nobody asked for), and runs the same
investigation, the same policy engine and the same answer format over the largest
transaction in each. The output is the same shape as cases/, in monitoring/.

  uv run python monitor.py --top 5
"""
from __future__ import annotations
import argparse, json, os, pathlib, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

import duckdb
from dotenv import load_dotenv
load_dotenv()
from backend import DuckDBBackend, ToolLog
from investigate import Investigation
import answer as ans

WINDOW_FROM = "2016-11-02"          # the exam window: after the last closed case
MIN_RING, MAX_RING = 2, 10          # the bounded tail; above this the component percolates

CANDIDATES = f"""
WITH pack AS (SELECT DISTINCT card_id FROM case_pack),
     -- a ring is only interesting if none of its cards is already a benchmark case
     ring AS (SELECT r.ring_id, r.ring_size
              FROM card_ring r
              WHERE r.ring_size BETWEEN {MIN_RING} AND {MAX_RING}
              GROUP BY 1,2
              HAVING count(*) FILTER (WHERE r.card_id IN (SELECT card_id FROM pack)) = 0),
     act AS (SELECT cr.ring_id, count(*) AS n_txn, sum(t.amount) AS amt,
                    max(t.risk_score) AS max_risk,
                    count(DISTINCT t.device_profile) AS n_dev,
                    arg_max(t.txn_id, t.amount) AS anchor_txn
             FROM card_ring cr JOIN tx t USING (card_id)
             WHERE t.ts >= TIMESTAMP '{WINDOW_FROM}' GROUP BY 1)
SELECT r.ring_id, r.ring_size, a.n_txn, a.amt, a.max_risk, a.n_dev, a.anchor_txn
FROM ring r JOIN act a USING (ring_id)
ORDER BY a.amt DESC
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out", default="monitoring")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect("build/fraud.db", read_only=True)
    try:
        rings = con.sql(CANDIDATES).df().head(args.top).to_dict("records")
    except duckdb.CatalogException:
        sys.exit("card_ring is missing. Run: uv run python prep/rings.py")
    if not rings:
        sys.exit("no bounded ring components are active in the window")

    llm = None
    if not args.no_llm and (os.getenv("OPENAI_API_KEY") or os.getenv("SARVAM_API_KEY")):
        from llm import LLM
        llm = LLM()

    out = pathlib.Path(args.out)
    out.mkdir(exist_ok=True)
    rows = []
    for i, r in enumerate(rings, 1):
        anchor = con.execute(
            "SELECT txn_id, card_id, customer_id, ts, amount, risk_score FROM tx WHERE txn_id = ?",
            [int(r["anchor_txn"])]).df().iloc[0]
        trigger = {
            "case_id": f"MON-{i:03d}",
            "opened_at": anchor.ts.to_pydatetime(),
            "trigger_type": "monitoring_sweep",
            "trigger_text": (
                f"Self-directed sweep. Connected components over the device-sharing graph "
                f"put card {anchor.card_id} in a bounded component of {int(r['ring_size'])} "
                f"cards sharing {int(r['n_dev'])} device profile(s), moving "
                f"${r['amt']:,.2f} across {int(r['n_txn'])} transactions since {WINDOW_FROM}. "
                f"No model score, cardholder report or analyst request opened this."),
            "flagged_txn_id": int(anchor.txn_id),
            "card_id": anchor.card_id, "customer_id": anchor.customer_id,
            "risk_score": float(anchor.risk_score) if anchor.risk_score == anchor.risk_score else None,
        }

        log = ToolLog()
        b = DuckDBBackend(log=log)
        if llm:
            llm.start_case()
        res = Investigation(b, trigger).run()
        a = ans.build(trigger, res, llm=llm)
        a["tool_calls"], a["tokens"] = log.count, (llm.tokens_for_case() if llm else 0)
        a["trigger"] = {"type": trigger["trigger_type"], "text": trigger["trigger_text"]}

        b.write_case({
            "graph_case_id": f"CASE-2016-M{i:03d}", "source_case_id": trigger["case_id"],
            "customer_id": trigger["customer_id"], "card_id": trigger["card_id"],
            "opened_at": trigger["opened_at"], "verdict": res["verdict"],
            "status": a["case"]["status"], "fraud_probability": round(res["prob"], 2),
            "pattern": a["case"]["pattern"], "pattern_description": res["pattern_desc"],
            "exposure_usd": round(res["exposure"], 2), "summary": a["case"]["summary"],
            "stop_reason": a["stop_reason"],
            "actions_final": "|".join(x["action"] for x in res["final"]),
            "sar_filed": res["sar_file"], "txn_ids": a["case"]["affected_txn_ids"],
            "connected_cards": res["connected"], "devices": [d for d in res["devices"] if d],
            "prior_cases": [p["case_id"] for p in res["prior"]][:8],
        })
        (out / f"{trigger['case_id']}.json").write_text(json.dumps(a, indent=2, default=str))
        rows.append((trigger["case_id"], int(r["ring_size"]), r["amt"], res["verdict"],
                     round(res["prob"], 2), a["case"]["pattern"], res["sar_file"],
                     "|".join(x["action"] for x in res["final"])))

    hdr = f"{'case':8} {'ring':>4} {'ring $':>11} {'verdict':11} {'p':5} {'pattern':28} {'sar':5} actions"
    print(hdr); print("-" * len(hdr))
    for x in rows:
        print(f"{x[0]:8} {x[1]:>4} {x[2]:>11,.2f} {x[3]:11} {x[4]:<5} {x[5]:28} {str(x[6]):5} {x[7]}")

    (out / "index.md").write_text(
        "# Self-directed monitoring sweep\n\n"
        f"Connected components over the device-sharing graph, bounded to "
        f"{MIN_RING}-{MAX_RING} cards, ranked by money moved since {WINDOW_FROM}. "
        "No benchmark card appears in any of these rings: nobody asked for this work.\n\n"
        "| case | ring | $ in window | verdict | p | pattern | SAR | final actions |\n"
        "|---|---:|---:|---|---:|---|---|---|\n"
        + "".join(f"| [{x[0]}]({x[0]}.json) | {x[1]} | {x[2]:,.2f} | {x[3]} | {x[4]} "
                  f"| {x[5]} | {x[6]} | {x[7].replace('|', ', ')} |\n" for x in rows))
    print(f"\n{len(rows)} self-opened cases -> {out}/")


if __name__ == "__main__":
    main()
