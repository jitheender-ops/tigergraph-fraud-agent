#!/usr/bin/env python
"""Check every answer file against the spec in the dataset README.

Missing fields score zero, invented IDs score zero, and sar.file must agree with
whether FILE_REPORT appears in the final actions. All three are checked here, plus
that every ID actually exists in the dataset.
"""
import json, pathlib, sys
import duckdb

ACTIONS = {"ALLOW_TRANSACTION","DECLINE_TRANSACTION","MONITOR_CARD","MONITOR_CONNECTED_CARDS",
           "WARN_CUSTOMER","VERIFY_WITH_CUSTOMER","STEP_UP_AUTH","BLOCK_CARD","BLOCK_ALL_CARDS",
           "GENERATE_REPORT","CREATE_CASE","FILE_REPORT","ESCALATE_TO_ANALYST","CLOSE_NO_FRAUD"}
ROUTES = {"auto","L1","L2"}
PATTERNS = {"card_testing","card_not_present_fraud","card_not_present_new_device",
            "out_of_region_use","account_takeover","undocumented","none"}
STATUS = {"open","closed_fraud","closed_legitimate","escalated"}
VERDICT = {"fraud","legitimate","uncertain"}
SOURCES = {"graph","document","customer","external"}
REQ_TYPES = {"customer_validation","step_up_auth","analyst_info"}

CASE_FIELDS = ["status","verdict","fraud_probability","pattern","pattern_description",
               "affected_txn_ids","first_suspicious_txn_id","connected_card_ids",
               "connected_device_profiles","exposure_usd","evidence","similar_prior_cases",
               "summary","written_to_graph","graph_case_id"]
SAR_FIELDS = ["file","reason","narrative","subjects","total_amount_usd","activity_dates"]
TOP_FIELDS = ["case_id","case","evidence_requests","next_best_actions","sar","stop_reason",
              "tool_calls","tokens","latency_s"]


def main(out="cases"):
    con = duckdb.connect("build/fraud.db", read_only=True)
    txns = {str(r[0]) for r in con.sql("SELECT txn_id FROM tx").fetchall()}
    cards = {r[0] for r in con.sql("SELECT DISTINCT card_id FROM tx").fetchall()}
    ccs = {r[0] for r in con.sql("SELECT case_id FROM closed_case").fetchall()}
    # cases an analyst closed from the console on the DuckDB mirror are real memory too
    log = pathlib.Path("build/console_closed_cases.jsonl")
    if log.exists():
        ccs |= {json.loads(l)["case_id"] for l in log.read_text().splitlines() if l.strip()}
    devs = {r[0] for r in con.sql("SELECT DISTINCT device_profile FROM tx WHERE device_profile IS NOT NULL").fetchall()}
    expect = [r[0] for r in con.sql("SELECT case_id FROM case_pack ORDER BY case_id").fetchall()]

    errs, warns = [], []
    for cid in expect:
        p = pathlib.Path(out) / f"{cid}.json"
        if not p.exists():
            errs.append(f"{cid}: MISSING FILE"); continue
        d = json.loads(p.read_text())
        E = lambda m: errs.append(f"{cid}: {m}")

        for k in TOP_FIELDS:
            if k not in d: E(f"missing top-level field '{k}'")
        c = d.get("case", {})
        for k in CASE_FIELDS:
            if k not in c: E(f"missing case.{k}")
        s = d.get("sar", {})
        for k in SAR_FIELDS:
            if k not in s: E(f"missing sar.{k}")

        if c.get("status") not in STATUS: E(f"bad status {c.get('status')!r}")
        if c.get("verdict") not in VERDICT: E(f"bad verdict {c.get('verdict')!r}")
        if c.get("pattern") not in PATTERNS: E(f"bad pattern {c.get('pattern')!r}")
        fp = c.get("fraud_probability")
        if not isinstance(fp, (int, float)) or not 0 <= fp <= 1: E(f"fraud_probability {fp}")
        if c.get("pattern") == "undocumented" and not c.get("pattern_description"):
            E("pattern is undocumented but pattern_description is empty")
        if c.get("pattern") != "undocumented" and c.get("pattern_description"):
            E("pattern_description must be '' unless pattern is undocumented")

        # verdict / exposure / SAR coherence
        if c.get("verdict") == "legitimate":
            if c.get("affected_txn_ids"): E("legitimate verdict with affected_txn_ids")
            if c.get("exposure_usd"): E("legitimate verdict with non-zero exposure")
            if s.get("file"): E("legitimate verdict with sar.file true")
        final = [a["action"] for a in d.get("next_best_actions", {}).get("final", [])]
        if s.get("file") != ("FILE_REPORT" in final):
            E(f"sar.file={s.get('file')} disagrees with FILE_REPORT in final actions")
        if s.get("file"):
            n = len(s.get("narrative","").split("."))
            if n < 6: E(f"SAR narrative has only ~{n} sentences, spec asks 6-12")
            if not s.get("subjects"): E("sar.file true but subjects empty")
            if len(s.get("activity_dates", [])) != 2: E("sar.activity_dates must have 2 dates")
        else:
            if s.get("narrative"): E("sar.file false but narrative non-empty")
            if s.get("subjects"): E("sar.file false but subjects non-empty")
            if s.get("total_amount_usd"): E("sar.file false but total_amount_usd non-zero")
            if s.get("activity_dates"): E("sar.file false but activity_dates non-empty")

        # actions and routes
        for phase in ("initial","final"):
            for a in d.get("next_best_actions", {}).get(phase, []):
                if a.get("action") not in ACTIONS: E(f"{phase}: bad action {a.get('action')!r}")
                if a.get("route") not in ROUTES: E(f"{phase}: bad route {a.get('route')!r}")
                if not a.get("reason"): E(f"{phase}: {a.get('action')} has no reason")
        # route correctness per policy section 2
        exp = c.get("exposure_usd", 0)
        for a in d.get("next_best_actions", {}).get("final", []):
            act, rt = a.get("action"), a.get("route")
            want = ("L1" if act == "DECLINE_TRANSACTION" else
                    ("L1" if exp <= 2500 else "L2") if act == "BLOCK_CARD" else
                    "L2" if act in ("BLOCK_ALL_CARDS","FILE_REPORT") else "auto")
            if rt != want: E(f"{act} routed {rt}, policy says {want} at exposure ${exp:,.2f}")
        if "BLOCK_ALL_CARDS" in final: warns.append(f"{cid}: BLOCK_ALL_CARDS used (R10)")

        # contradictions
        # A verdict of legitimate that still declines or blocks is a contradiction the
        # answer file cannot defend: exposure is zero and no transaction is named, yet
        # the cardholder is refused. Caught only after an external signal moved a case
        # into the legitimate band while R4 was still firing on it.
        if c["verdict"] == "legitimate" and any(
                x in final for x in ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION")):
            err(cid, f"verdict is legitimate but the final actions include "
                     f"{sorted({'BLOCK_CARD','BLOCK_ALL_CARDS','DECLINE_TRANSACTION'} & set(final))}")
        if "CLOSE_NO_FRAUD" in final and any(x in final for x in ("BLOCK_CARD","BLOCK_ALL_CARDS")):
            E("final actions both close the alert and block the card")
        if "ALLOW_TRANSACTION" in final and "DECLINE_TRANSACTION" in final:
            E("final actions both allow and decline the transaction")

        # every ID must exist in the dataset
        for t in c.get("affected_txn_ids", []):
            if str(t) not in txns: E(f"unknown txn id {t}")
        if c.get("first_suspicious_txn_id") and str(c["first_suspicious_txn_id"]) not in txns:
            E(f"unknown first_suspicious_txn_id {c['first_suspicious_txn_id']}")
        for k in c.get("connected_card_ids", []):
            if k not in cards: E(f"unknown card id {k}")
        for k in c.get("similar_prior_cases", []):
            if k not in ccs: E(f"unknown closed case id {k}")
        for k in c.get("connected_device_profiles", []):
            if k not in devs: E(f"unknown device profile {k}")
        for q in d.get("evidence_requests", []):
            if q.get("type") not in REQ_TYPES: E(f"bad evidence_request type {q.get('type')!r}")
            if not q.get("reason"): E(f"evidence_request {q.get('type')} has no reason")
        for e in c.get("evidence", []):
            if e.get("source") not in SOURCES: E(f"bad evidence source {e.get('source')!r}")
            if not e.get("claim"): E("evidence entry with empty claim")
        if not c.get("evidence"): E("no evidence recorded")
        if not c.get("summary"): E("empty summary")
        if not d.get("stop_reason"): E("empty stop_reason")

    for w in warns: print("WARN ", w)
    for e in errs: print("ERROR", e)
    print(f"\n{len(expect)} cases checked, {len(errs)} errors, {len(warns)} warnings")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
