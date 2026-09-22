#!/usr/bin/env python
"""Run the agent over the case pack. Writes cases/<case_id>.json."""
from __future__ import annotations
import argparse, json, os, pathlib, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

import duckdb
from dotenv import load_dotenv
load_dotenv()
from backend import DuckDBBackend, ToolLog, reset_case_log
from investigate import Investigation
import answer as ans


def get_backend(kind, log):
    if kind == "tigergraph":
        from tg import connect
        from backend import TigerGraphBackend
        return TigerGraphBackend(connect(), log)
    if kind == "mcp":
        from mcp_backend import MCPBackend
        return MCPBackend(log=log)
    return DuckDBBackend(log=log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="duckdb", choices=["duckdb", "tigergraph", "mcp"])
    ap.add_argument("--out", default="cases")
    ap.add_argument("--case", action="append", help="run only these case ids")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    llm = None
    if not args.no_llm and (os.getenv("OPENAI_API_KEY") or os.getenv("SARVAM_API_KEY")):
        from llm import LLM
        llm = LLM()

    con = duckdb.connect("build/fraud.db", read_only=True)
    triggers = con.sql("SELECT * FROM case_pack ORDER BY case_id").df().to_dict("records")
    if args.case:
        triggers = [t for t in triggers if t["case_id"] in set(args.case)]

    pathlib.Path(args.out).mkdir(exist_ok=True)
    reset_case_log()          # this run owns the case log; monitor.py appends to it
    rows = []
    for t in triggers:
        log = ToolLog()
        b = get_backend(args.backend, log)
        if llm:
            llm.start_case()
        inv = Investigation(b, t, llm=llm)
        r = inv.run()

        payload = {
            "graph_case_id": f"CASE-2016-{t['case_id'].split('-')[1]}",
            "source_case_id": t["case_id"], "customer_id": t["customer_id"],
            "card_id": t["card_id"], "opened_at": t["opened_at"],
            "status": {"fraud": "closed_fraud", "legitimate": "closed_legitimate"}.get(r["verdict"], "open"),
            "verdict": r["verdict"], "fraud_probability": round(r["prob"], 2),
            "pattern": r["pattern"], "pattern_description": r["pattern_desc"],
            "exposure_usd": round(r["exposure"], 2), "summary": "",
            "stop_reason": "", "actions_final": "|".join(a["action"] for a in r["final"]),
            "sar_filed": r["sar_file"], "txn_ids": r["episode"]["txn_ids"],
            "connected_cards": r["connected"], "devices": [d for d in r["devices"] if d],
            "prior_cases": [p["case_id"] for p in r["prior"]][:8],
        }
        out = ans.build(t, r, llm=llm)
        payload["summary"] = out["case"]["summary"]
        payload["stop_reason"] = out["stop_reason"]
        b.write_case(payload)

        out["tool_calls"] = log.count
        out["tokens"] = llm.tokens_for_case() if llm else 0
        p = pathlib.Path(args.out) / f"{t['case_id']}.json"
        p.write_text(json.dumps(out, indent=2, default=str))

        rows.append((t["case_id"], t["trigger_type"], r["verdict"], round(r["prob"], 2),
                     r["pattern"], round(r["exposure"], 2), out["sar"]["file"],
                     len(r["connected"]), log.count,
                     "|".join(a["action"] for a in r["final"])))

    w = f"{'case':9} {'trigger':16} {'verdict':11} {'p':5} {'pattern':28} {'exposure':>10} {'sar':5} {'conn':4} {'tc':3}  actions"
    print(w)
    print("-" * len(w))
    for x in rows:
        print(f"{x[0]:9} {x[1]:16} {x[2]:11} {x[3]:<5} {x[4]:28} {x[5]:>10,.2f} {str(x[6]):5} {x[7]:<4} {x[8]:<3}  {x[9]}")
    print(f"\n{len(rows)} cases -> {args.out}/")


if __name__ == "__main__":
    main()
