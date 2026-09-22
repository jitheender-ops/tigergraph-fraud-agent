"""Build the answer file. Every field the spec lists, in the spec's shape.

The narrative is assembled deterministically from the evidence actually gathered, so
no ID or amount can be invented. When an LLM key is present it rewrites the prose for
fluency and is checked afterwards: any answer that drops or invents an ID is discarded
and the deterministic text is kept.
"""
from __future__ import annotations
import datetime as dt, re

PATTERN_PROSE = {
    "card_testing": "testing of a stolen card number before use",
    "card_not_present_fraud": "card-not-present use of the number without the card",
    "card_not_present_new_device": "card-not-present use from a device new to the account",
    "out_of_region_use": "card-present use in a billing region the cardholder has no history in",
    "account_takeover": "mixed-channel activity consistent with stolen credentials",
    "undocumented": "no documented pattern the bank recognises",
    "none": "no fraud pattern",
}


def _d(x):
    return x.strftime("%Y-%m-%d") if isinstance(x, (dt.datetime, dt.date)) else str(x)[:10]


def build(trigger, r, llm=None, backend=None) -> dict:
    f, ep_, sig = r["f"], r["episode"], r["signals"]
    verdict, prob, pattern = r["verdict"], r["prob"], r["pattern"]
    status = {"fraud": "closed_fraud", "legitimate": "closed_legitimate",
              "uncertain": "escalated" if any(a["action"] == "ESCALATE_TO_ANALYST"
                                              for a in r["final"]) else "open"}[verdict]

    evidence = [{
        "claim": s.claim,
        "source": s.source,
        "ref": s.ref or "query:card_window",
        "entity_ids": [str(e) for e in s.entity_ids],
    } for s in sig]

    prior_ids = [p["case_id"] for p in r["prior"]][:8] + \
                [p["case_id"] for p in r["dev_prior"]][:4]
    prior_ids = list(dict.fromkeys(prior_ids))

    reported_pattern = pattern if verdict != "legitimate" else "none"
    graph_case_id = f"CASE-2016-{trigger['case_id'].split('-')[1]}"
    summary = _summary(trigger, r)
    if llm:
        summary = llm.polish_summary(summary, r) or summary

    sar = _sar(trigger, r, llm)

    return {
        "case_id": trigger["case_id"],
        "case": {
            "status": status,
            "verdict": verdict,
            "fraud_probability": round(prob, 2),
            "pattern": reported_pattern,
            "pattern_description": (r["pattern_desc"]
                                    if reported_pattern == "undocumented" else ""),
            "affected_txn_ids": ep_["txn_ids"] if verdict != "legitimate" else [],
            "first_suspicious_txn_id": ep_["first_txn_id"] if verdict != "legitimate" else "",
            "connected_card_ids": r["connected"],
            "connected_device_profiles": [d for d in r["devices"] if d],
            "exposure_usd": round(r["exposure"], 2),
            "evidence": evidence,
            "similar_prior_cases": prior_ids,
            "summary": summary,
            # True only when the case vertex actually landed in TigerGraph. On the
            # DuckDB mirror the write goes to build/graph_cases.jsonl, which is
            # auditable but is not the graph, and saying otherwise in a submitted
            # answer file would be a false claim.
            "written_to_graph": getattr(backend, "name", "duckdb") in ("tigergraph", "mcp"),
            "graph_case_id": graph_case_id,
        },
        "evidence_requests": [
            {"type": q["type"], "asked_after_step": q["asked_after_step"],
             "assumed_response": q["assumed_response"]} for q in r["requests"]
        ],
        "next_best_actions": {
            "initial": [{"action": a["action"], "route": a["route"], "reason": a["reason"]}
                        for a in r["initial"]],
            "final": [{"action": a["action"], "route": a["route"], "reason": a["reason"]}
                      for a in r["final"]],
            "what_changed": _what_changed(r),
        },
        "sar": sar,
        "stop_reason": _stop(r),
        "tool_calls": 0,     # filled by the runner from the tool log
        "tokens": 0,
        "latency_s": r["latency_s"],
    }


def _summary(t, r):
    f, ep_ = r["f"], r["episode"]
    bits = []
    amt = f["amount"]
    bits.append(
        f"{t['trigger_type'].replace('_',' ').capitalize()} on card {t['card_id']} over a "
        f"${amt:,.2f} {f['channel'].replace('_',' ')} transaction on {_d(f['ts'])}.")
    top = sorted([s for s in r["signals"] if s.weight > 0], key=lambda s: -s.weight)[:2]
    neg = sorted([s for s in r["signals"] if s.weight < 0], key=lambda s: s.weight)[:2]
    if top:
        bits.append("Against it: " + " ".join(_fact(s.claim) for s in top))
    if neg:
        bits.append("For the cardholder: " + " ".join(_fact(s.claim) for s in neg))
    if r["verdict"] == "fraud":
        bits.append(
            f"Assessed as {PATTERN_PROSE[r['pattern']]} at probability {r['prob']:.2f}, "
            f"{len(ep_['txn_ids'])} transaction(s) and ${r['exposure']:,.2f} of exposure.")
    elif r["verdict"] == "legitimate":
        bits.append(
            f"Assessed as legitimate at fraud probability {r['prob']:.2f}; no transaction is "
            f"treated as part of a fraud episode and exposure is zero.")
    else:
        bits.append(
            f"The evidence conflicts and probability sits at {r['prob']:.2f}, so the case is "
            f"not decided in the graph; exposure if the activity is fraudulent is "
            f"${r['exposure']:,.2f}.")
    if r["connected"]:
        bits.append(
            f"{len(r['connected'])} other card(s) transacted through the same device profile in "
            f"the window and are named for monitoring.")
    return " ".join(bits)


def _sar(t, r, llm):
    if not r["sar_file"]:
        return {"file": False, "reason": r["sar_reason"], "narrative": "",
                "subjects": [], "total_amount_usd": 0, "activity_dates": []}
    f, ep_ = r["f"], r["episode"]
    subjects = [t["customer_id"], t["card_id"]] + r["connected"][:8] + \
               [d for d in r["devices"] if d]
    narrative = _narrative(t, r)
    if llm:
        narrative = llm.polish_narrative(narrative, r, subjects) or narrative
    return {
        "file": True, "reason": r["sar_reason"], "narrative": narrative,
        "subjects": list(dict.fromkeys(subjects)),
        "total_amount_usd": round(r["exposure"], 2),
        "activity_dates": [_d(ep_["t0"]), _d(ep_["t1"])],
    }


def _narrative(t, r):
    f, ep_ = r["f"], r["episode"]
    s = []
    s.append(
        f"Between {_d(ep_['t0'])} and {_d(ep_['t1'])}, card {t['card_id']} belonging to customer "
        f"{t['customer_id']} was used for {len(ep_['txn_ids'])} transaction(s) totalling "
        f"${r['exposure']:,.2f}, of which the transaction that triggered this investigation was "
        f"${f['amount']:,.2f} on {_d(f['ts'])}.")
    s.append(
        f"The activity was conducted {f['channel'].replace('_',' ')}"
        + (f" under product code {f['product_cd']}" if f.get("product_cd") else "")
        + (f", billed in region {f['addr1']}" if f.get("addr1") == f.get("addr1") and f.get("addr1") is not None else "")
        + (f", from the device profile '{f['device_profile']}'" if f.get("device_profile") else "")
        + ".")
    # A regulator reads the observation, not how the agent weighted it. Each claim is
    # written as observation-first, justification-after, so the lead sentence is the fact.
    for sg in sorted([x for x in r["signals"] if x.weight > 0.4], key=lambda x: -x.weight)[:4]:
        s.append(_fact(sg.claim))
    if r["connected"]:
        s.append(
            f"The same device profile was used by {len(r['connected'])} other card(s) in the "
            f"thirty days to {_d(f['ts'])} ({', '.join(r['connected'][:6])}), indicating a common "
            f"actor operating across more than one cardholder.")
    if r["prior"]:
        s.append(
            f"The bank's closed investigations already record "
            f"{len([p for p in r['prior'] if p.get('outcome')=='confirmed_fraud'])} confirmed "
            f"fraud case(s) on this card "
            f"({', '.join(p['case_id'] for p in r['prior'][:4])}).")
    if r["requests"]:
        said = r["requests"][0]["assumed_response"].split("ASSUMPTION")[0].strip().rstrip(".")
        said = said[0].lower() + said[1:] if said else said
        s.append(f"On contact, {said}. That response is an assumption recorded in the case "
                 f"file, as no cardholder replies were available to this investigation.")
    if r["pattern"] == "undocumented":
        s.append(
            f"The activity is suspicious because it fits none of the five fraud patterns the "
            f"institution documents, while showing the coordination described above. It is "
            f"assessed at a {r['prob']:.0%} probability of being fraudulent on {r['n_ind']} "
            f"independent pieces of evidence drawn from the transaction graph and the "
            f"institution's closed-case history.")
    else:
        s.append(
            f"The activity is suspicious because it is consistent with "
            f"{PATTERN_PROSE[r['pattern']]}, assessed at a {r['prob']:.0%} probability of being "
            f"fraudulent on {r['n_ind']} independent pieces of evidence drawn from the "
            f"transaction graph and the institution's closed-case history.")
    s.append(_filing_reason(r))
    s.append(f"The total amount of suspicious activity is ${r['exposure']:,.2f}. "
             + _action_prose(r["final"]))
    return " ".join(s)


_ACTION_PROSE = {
    "BLOCK_CARD": "the card has been blocked and scheduled for reissue",
    "BLOCK_ALL_CARDS": "every card held by the customer has been blocked",
    "DECLINE_TRANSACTION": "the pending authorisation has been declined",
    "MONITOR_CARD": "the card has been placed under heightened monitoring",
    "MONITOR_CONNECTED_CARDS": "the cards sharing the same origin have been placed under monitoring",
    "STEP_UP_AUTH": "step-up authentication has been required for further activity",
    "VERIFY_WITH_CUSTOMER": "the cardholder has been contacted to verify the activity",
    "WARN_CUSTOMER": "the cardholder has been sent an advisory notice",
    "CREATE_CASE": "an internal investigation case has been opened",
    "ESCALATE_TO_ANALYST": "the matter has been escalated to a fraud analyst",
    "ALLOW_TRANSACTION": "the transaction was allowed to stand",
    "CLOSE_NO_FRAUD": "the alert was closed as legitimate",
    "GENERATE_REPORT": "an internal report has been written",
}


def _action_prose(actions):
    """A regulator reads what the institution did, not the bank's internal action codes."""
    parts = [_ACTION_PROSE[a["action"]] for a in actions
             if a["action"] in _ACTION_PROSE]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].capitalize() + "."
    return (", ".join(parts[:-1]) + ", and " + parts[-1]).capitalize() + "."


def _filing_reason(r):
    """Restate the policy test in plain terms rather than citing a rule number."""
    if r["pattern"] == "undocumented":
        return ("The filing is made because the activity is coordinated across accounts and "
                "matches no documented pattern, which requires a report regardless of amount.")
    if r["exposure"] > 1000:
        return (f"The filing is made because the loss exposure of ${r['exposure']:,.2f} exceeds "
                f"the institution's $1,000 reporting threshold.")
    return ("The filing is made because the activity connects to a device profile shared with "
            "other compromised cards, which requires a report regardless of amount.")


# Every evidence claim is written observation-first: the opening sentence states what was
# observed, and anything after it is the calibration behind the weight. A case file wants
# both; a summary and a regulator's narrative want only the observation.
_SENT = re.compile(r"(?<=[a-z0-9\)\]%])\.\s+(?=[A-Z0-9$])")


def _fact(claim: str) -> str:
    """The observed part of an evidence claim: its first sentence."""
    return _SENT.split(claim.strip(), 1)[0].rstrip(". ") + "."


def _what_changed(r):
    if not r["requests"]:
        return "nothing"
    a0 = {a["action"] for a in r["initial"]}
    a1 = {a["action"] for a in r["final"]}
    if a0 == a1:
        return ("The requested evidence came back consistent with the initial assessment, so the "
                "recommended actions are unchanged.")
    added, dropped = sorted(a1 - a0), sorted(a0 - a1)
    resp = r["requests"][0]["assumed_response"].split("ASSUMPTION")[0].strip().rstrip(".")
    out = f"{resp}, moving assessed probability to {r['prob']:.2f}."
    if added:
        out += " Added: " + ", ".join(added) + "."
    if dropped:
        out += " Dropped: " + ", ".join(dropped) + "."
    return out


def _stop(r):
    import policy as pol
    return pol.stop_reason(r["prob"], r["n_ind"], r["verdict"], bool(r["requests"]),
                           bool(r["requests"]))
