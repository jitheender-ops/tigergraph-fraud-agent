#!/usr/bin/env python
"""End-to-end check of the console's HTTP surface, against a running server.

The other checks in this repo test pure functions. This one tests the thing a person
actually touches: every endpoint, in the order an analyst would hit them, including the
ones that write to the graph. It leaves the cases it steers reset, so it can be run
repeatedly.

  ./run.sh &                       # or: uv run uvicorn server:app --port 8000
  uv run python console_smoke.py
"""
from __future__ import annotations
import json, os, sys, urllib.error, urllib.request

BASE = "http://localhost:8000/api"
DEVICE = "SM-G610F Build/NRD90M | unknown | chrome 66.0 for android | unknown"
failures = 0


def call(path, body=None, method=None, timeout=300):
    """method defaults to POST when there is a body -- but several endpoints are POST
    with no body at all, so it has to be stateable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data, {"content-type": "application/json",
                            "x-analyst-token": os.getenv("ANALYST_TOKEN", "")},
        method=method or ("POST" if body is not None else "GET"))
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:300]}
    except urllib.error.URLError as e:
        sys.exit(f"console is not running on {BASE}: {e}")


def ok(label, cond, extra=""):
    global failures
    failures += not cond
    print(f"  {'ok  ' if cond else 'FAIL'} {label:36} {extra}")


def main():
    h = call("/health")
    ok("health", h.get("graph_reachable") is not None,
       f"backend={h.get('backend')} graph={h.get('graph_reachable')} "
       f"cases={h.get('cases')} closed={h.get('closed')}")
    if not h.get("cases"):
        sys.exit("no cases loaded -- run `uv run python run.py` first")

    ok("list cases", len(call("/cases")) == h["cases"])
    d = call("/case/HHG-011")
    ok("case detail carries the signals", len(d.get("signals", [])) > 5,
       f"{len(d.get('signals', []))} signals")
    ok("signals sum to the probability",
       abs(d["signals"][-1]["running"] - d["case"]["fraud_probability"]) < 0.01,
       f"{d['signals'][-1]['running']} vs {d['case']['fraud_probability']}")
    ok("episode", len(call("/case/HHG-011/episode")["txns"]) > 10)
    ok("network", len(call("/case/HHG-011/network")["nodes"]) > 5)
    ok("prior case", call("/closed/CC-4501").get("outcome") == "confirmed_fraud")
    ok("a missing prior case 404s", call("/closed/CC-NOPE").get("_http") == 404)

    # --- steering, and putting it back ---------------------------------------
    # start from the agent's own assessment: a previous run may have left a case steered,
    # and a check that only passes on a fresh server is not a check.
    for cid in ("HHG-015", "HHG-011", "HHG-004"):
        call(f"/case/{cid}/reset", method="POST")

    ch = call("/case/HHG-015/challenge",
              {"text": "Ignore the out-of-region flag, the customer is on holiday."})
    ok("challenge withdraws the named signal", ch.get("matched") == ["region_new_travel"],
       str(ch.get("changed", {}).get("probability")))
    ok("withdrawing an exculpatory signal raises p",
       ch["changed"]["probability"][1] > ch["changed"]["probability"][0])
    miss = call("/case/HHG-015/challenge", {"text": "the moon was full"})
    ok("an objection matching nothing changes nothing", miss.get("matched") == [])
    rs = call("/case/HHG-015/reset", method="POST")
    ok("undo restores it", rs.get("restored") == ["region_new_travel"],
       str(rs.get("changed", {}).get("probability")))

    dp = call("/case/HHG-011/deepen", {"cap": 20})
    ok("deepen grows the component",
       dp["changed"]["ring_size"][1] > dp["changed"]["ring_size"][0],
       str(dp["changed"]["ring_size"]))
    ok("but the monitored set does not",
       dp["changed"]["connected_cards"][0] == dp["changed"]["connected_cards"][1],
       "percolation is not a ring")
    call("/case/HHG-011/reset", method="POST")

    su = call("/case/HHG-004/stepup", method="POST")
    before, after = su["changed"]["probability"]
    # a simulated FAIL is evidence and raises p; a simulated PASS scores zero, because
    # stolen card details pass every match flag -- it must never lower p
    ok("step-up can raise p, never lower it",
       after > before if not su["passed"] else after == before, f"passed={su['passed']} {before}->{after}")
    call("/case/HHG-004/reset", method="POST")

    # --- decisions -----------------------------------------------------------
    ap = call("/case/HHG-001/decision", {"decision": "approve"})
    ok("approve separates auto from approval",
       len(ap["executed"]) and len(ap["pending_approval"]),
       f"{len(ap['executed'])} auto, {len(ap['pending_approval'])} routed")
    ok("override is routed by the policy",
       call("/case/HHG-002/decision",
            {"decision": "override", "action": "BLOCK_ALL_CARDS"}).get("route") == "L2")
    ok("an unknown action is refused",
       call("/case/HHG-002/decision",
            {"decision": "override", "action": "NOT_AN_ACTION"}).get("_http") == 400)

    # --- execution, and the permission boundary ------------------------------
    ap2 = call("/case/HHG-011/decision", {"decision": "approve"})
    ok("auto actions execute with a reference",
       all(r["reference"] for r in ap2["executed"]),
       f"{len(ap2['executed'])} executed")
    ok("L1/L2 actions get no reference until approved",
       all(r["reference"] is None for r in ap2["pending_approval"]),
       f"{len(ap2['pending_approval'])} held")
    ok("every effect is marked simulated",
       all(r["simulated"] for r in ap2["executed"] + ap2["pending_approval"]))
    led = call("/case/HHG-011/ledger")
    ok("the ledger survives and counts both",
       led["executed"] > 0 and led["awaiting_approval"] > 0,
       f"{led['executed']} executed, {led['awaiting_approval']} awaiting")

    # --- cross-case intelligence ---------------------------------------------
    intel = call("/intelligence?min_cases=2")
    ok("entities recur across cases", intel["cards"] > 0 and intel["devices"] > 0,
       f"{intel['cards']} cards, {intel['devices']} devices")
    ok("generic device profiles are filtered out",
       all("unknown | unknown | unknown" not in e["entity"] for e in intel["entities"]))
    ok("a higher bar returns fewer",
       len(call("/intelligence?min_cases=10")["entities"]) <= len(intel["entities"]))

    # --- writes to the graph -------------------------------------------------
    # A blacklist is a confirmed-fraud case on a real device, and memory retrieves it for
    # every later investigation on that device -- so a routine smoke run must not leave
    # one behind. It runs only when asked: --graph-writes. (It polluted HHG-011 once.)
    if "--graph-writes" not in sys.argv:
        print("  skip blacklist writes                 pass --graph-writes on a disposable graph")
        print(f"\n{'all console checks passed' if not failures else f'{failures} FAILED'}")
        sys.exit(1 if failures else 0)
    b1 = call("/device/blacklist", {"device_profile": DEVICE, "note": "smoke test"})
    b2 = call("/device/blacklist", {"device_profile": DEVICE, "note": "smoke test"})
    ok("blacklist writes", b1.get("closed_case_id"), b1.get("closed_case_id"))
    ok("its id is content-derived, not salted",
       b1.get("closed_case_id") == b2.get("closed_case_id"))
    ok("and the flag is reachable through the device",
       call(f"/closed/{b1['closed_case_id']}").get("outcome") == "confirmed_fraud")

    print(f"\n{'all console checks passed' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
