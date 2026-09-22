#!/usr/bin/env python
"""HTTP surface for the analyst console.

The agent investigates and recommends. This is where a human argues with it.

Three of these endpoints re-run the investigation rather than editing its output, which
is the whole point: an analyst who withdraws a premise gets a new probability out of the
same deterministic scorer, not a number the LLM was asked to revise. The only thing the
model does in the steering loop is map free text onto a signal name -- a classification,
not a judgement -- and even that is validated against the signals actually present.

  uv run uvicorn server:app --reload --port 8000
"""
from __future__ import annotations
import datetime as dt, hashlib, json, os, pathlib, sys, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

from dotenv import load_dotenv
load_dotenv()
import math

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import answer as ans
import patterns as P
import policy as pol
from backend import ToolLog
from investigate import Investigation
from run import get_backend

BACKEND = os.getenv("CONSOLE_BACKEND", "tigergraph")
CASES, MONITORING = pathlib.Path("cases"), pathlib.Path("monitoring")

app = FastAPI(title="Fraud Investigation Console")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# One backend for the process. get_backend() opens a connection each time it is called,
# and on the MCP path it starts a whole server subprocess -- once per request was a leak,
# not a design. Each call still gets its own ToolLog, which is the only per-request state.
_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def backend(log: ToolLog | None = None):
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = get_backend(BACKEND, ToolLog())
    _BACKEND.log = log or ToolLog()
    return _BACKEND


@app.exception_handler(Exception)
def unhandled(request, exc: Exception):
    """A dead database should read as a sentence, not a traceback in the browser.

    Deliberately narrow: registering a handler for Exception also catches the
    HTTPExceptions the endpoints raise on purpose, which turned every honest 404 and 409
    into a 503 about the database being down.
    """
    from fastapi.responses import JSONResponse
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=503, content={
        "error": f"{type(exc).__name__}: {exc}"[:400],
        "hint": f"The console is on the '{BACKEND}' backend. If that is TigerGraph, "
                f"check the container is up: ./graph/local_tigergraph.sh status",
    })


@app.get("/api/health")
def health():
    """Enough to tell a broken database from a broken front end."""
    try:
        n = len(backend(ToolLog()).prior_cases_for_card("__none__"))
        ok = True
    except Exception as e:                       # noqa: BLE001 - reported, not raised
        n, ok = str(e)[:200], False
    return {"backend": BACKEND, "graph_reachable": ok, "probe": n,
            "cases": len(STORE), "closed": sum(1 for c in STORE.values() if c.closed)}


# ---------------------------------------------------------------------------
# state. One process, one analyst, so the working set is a dict -- the durable
# record is the graph, which is where every decision below actually lands.
# ---------------------------------------------------------------------------
class Case:
    def __init__(self, answer, trigger, source):
        self.answer, self.trigger, self.source = answer, trigger, source
        self.events: list[dict] = []
        self.suppressed: set[str] = set()
        self.analyst_signals: list[P.Signal] = []
        self.ring_cap: int | None = None
        self.closed: str | None = None
        self.signals = None        # real Signal objects, filled by the first re-run

        # Steering re-runs the investigation and writes back onto this object; two
        # requests for one case must not interleave. FastAPI runs sync endpoints in a
        # threadpool, so this is not theoretical.
        self.lock = threading.Lock()

    def log(self, kind, detail, **extra):
        self.events.append({"at": dt.datetime.now().isoformat(timespec="seconds"),
                            "kind": kind, "detail": detail, **extra})
        return self.events[-1]


STORE: dict[str, Case] = {}


def load():
    tp = pathlib.Path("build/triggers.json")
    if not tp.exists():
        print("console: build/triggers.json is missing -- run `uv run python run.py` "
              "first. Starting empty so /api/health still answers.")
        return
    triggers = json.loads(tp.read_text())
    # triggers.json is JSON, so opened_at came back a string; the investigation does
    # date arithmetic on it.
    for t in triggers.values():
        if isinstance(t.get("opened_at"), str):
            t["opened_at"] = dt.datetime.fromisoformat(t["opened_at"])
        # a customer_report trigger has no model score, and pandas spells that NaN,
        # which json.dumps refuses. None is what the wire means by "no score".
        for k, v in list(t.items()):
            if isinstance(v, float) and v != v:
                t[k] = None
    for folder, source in ((CASES, "cases"), (MONITORING, "monitoring")):
        for p in sorted(folder.glob("*.json")):
            a = json.loads(p.read_text())
            cid = a["case_id"]
            if cid in triggers:
                STORE[cid] = Case(a, triggers[cid], source)
    _recover_closed()
    print(f"console: {len(STORE)} cases loaded, backend={BACKEND}")


def _recover_closed():
    """A case closed in a previous session is closed.

    The working set is in memory, but the decision is not: closing writes a ClosedCase
    vertex. Without this the console forgets on restart and offers to close a case the
    graph already records, which would write the decision twice.
    """
    try:
        b = backend()
    except Exception as e:                       # noqa: BLE001 - the graph may be down
        print(f"console: could not reach the graph to recover closed cases ({e})")
        return
    for cid, c in STORE.items():
        try:
            row = b.closed_case(closed_case_id(cid))
        except Exception:                        # noqa: BLE001 - absence is not an error
            continue
        if row:
            c.closed = row.get("outcome")
            c.log("recovered", f"already closed as {c.closed} in a previous session")


def closed_case_id(cid: str) -> str:
    return f"CC-{cid.replace('-', '')}"


def get(cid) -> Case:
    if cid not in STORE:
        raise HTTPException(404, f"no case {cid}")
    return STORE[cid]


def rerun(c: Case) -> dict:
    """Re-investigate under the analyst's current steering and replace the answer."""
    log = ToolLog()
    b = backend(log)
    r = Investigation(b, c.trigger, suppress=c.suppressed,
                      analyst_signals=c.analyst_signals, ring_cap=c.ring_cap).run()
    out = ans.build(c.trigger, r, backend=b)
    out["tool_calls"], out["tokens"] = log.count, 0
    c.signals = r["signals"]
    ring = next((x for x in r["signals"] if x.name == "ring_component"), None)
    before_ring, c.ring_size = getattr(c, "ring_size", None), _ring_size(ring)
    before = c.answer
    c.answer = out
    return {
        "case": out,
        "signals": _signals(c),
        "changed": {
            "probability": [before["case"]["fraud_probability"],
                            out["case"]["fraud_probability"]],
            "verdict": [before["case"]["verdict"], out["case"]["verdict"]],
            "actions": [[a["action"] for a in before["next_best_actions"]["final"]],
                        [a["action"] for a in out["next_best_actions"]["final"]]],
            "sar": [before["sar"]["file"], out["sar"]["file"]],
            # a deeper look usually moves the ring rather than the probability, and a
            # diff that only reports the number makes it look like nothing happened
            "connected_cards": [len(before["case"]["connected_card_ids"]),
                                len(out["case"]["connected_card_ids"])],
            "evidence": [len(before["case"]["evidence"]), len(out["case"]["evidence"])],
            "ring_size": [before_ring, c.ring_size],
        },
    }


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
@app.get("/api/cases")
def list_cases():
    return [{"case_id": cid, "source": c.source, "closed": c.closed,
             "events": len(c.events), **c.answer} for cid, c in STORE.items()]


@app.get("/api/case/{cid}")
def one_case(cid: str):
    c = get(cid)
    if c.signals is None:
        rerun(c)
    return {"case_id": cid, "source": c.source, "closed": c.closed,
            "trigger": c.trigger, "events": c.events, "signals": _signals(c),
            "suppressed": sorted(c.suppressed), "ring_cap": c.ring_cap, **c.answer}


def _signals(c: Case) -> list[dict]:
    """The probability decomposed.

    The answer file carries `evidence`, whose shape the submission spec fixes: claim,
    source, ref, entity_ids. It has no room for the two things that explain the number --
    the signal's name and its log-odds weight -- so the console gets them here instead of
    the spec being bent to fit a screen. The running total is what the waterfall draws:
    a probability you cannot decompose is a probability nobody can argue with.
    """
    out, total = [], 0.0
    for sg in (c.signals or []):
        total += sg.weight
        out.append({"name": sg.name, "weight": round(sg.weight, 3),
                    "running": round(1 / (1 + math.exp(-total)), 4),
                    "source": sg.source, "claim": sg.claim,
                    "withdrawn": sg.name.startswith("withdrawn:")})
    return out


# ---------------------------------------------------------------------------
# steer
# ---------------------------------------------------------------------------
class Challenge(BaseModel):
    text: str = Field(..., min_length=3, max_length=500)


@app.post("/api/case/{cid}/challenge")
def challenge(cid: str, body: Challenge):
    """"Ignore the out-of-region flag, the customer is on holiday."

    The claim is matched to the signals actually in evidence, those signals are
    withdrawn, and the probability is recomputed by the same log-odds sum as before. The
    withdrawn claims stay in the case file marked WITHDRAWN BY ANALYST, because a case
    that quietly loses evidence is not auditable.
    """
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    names = _signal_names(c)
    hit = _match_signals(body.text, names)
    if not hit:
        return {"matched": [], "note": "No signal in this case matches that objection. "
                "Nothing was changed.", "names": sorted(names)}
    with c.lock:
        c.suppressed |= set(hit)
        c.analyst_signals = [P.Signal(
            "analyst_context", 0.0,
            f"Analyst context: {body.text.strip()}", [], f"analyst:{cid}",
            source="external")]
        res = rerun(c)
    c.log("challenge", body.text.strip(), withdrew=sorted(hit),
          moved=res["changed"]["probability"])
    return {"matched": sorted(hit), **res}


@app.post("/api/case/{cid}/reset")
def reset(cid: str):
    """Withdraw the withdrawal.

    An analyst who challenges the wrong signal had no way back: `suppressed` only ever
    grew. Steering a case is a judgement, and a judgement you cannot take back is a trap,
    not a control.
    """
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    if not (c.suppressed or c.analyst_signals or c.ring_cap):
        return {"note": "Nothing to reset -- this case is as the agent left it."}
    with c.lock:
        undone = sorted(c.suppressed)
        c.suppressed, c.analyst_signals, c.ring_cap = set(), [], None
        res = rerun(c)
    c.log("reset", "analyst steering cleared; back to the agent's own assessment",
          restored=undone)
    return {"restored": undone, **res}


class Deepen(BaseModel):
    cap: int = Field(20, ge=2, le=200)


@app.post("/api/case/{cid}/deepen")
def deepen(cid: str, body: Deepen):
    """Widen the device-sharing expansion. The calibrated cap is 8 because the
    transitive hull percolates above it (prep/rings.py); an analyst asking to look
    further gets the wider component, and the case file records that they asked."""
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    with c.lock:
        if c.signals is None:
            rerun(c)      # establish the baseline so the diff has something to show
        c.ring_cap = body.cap
        res = rerun(c)
    c.log("deepen", f"device-sharing component recomputed at a cap of {body.cap} cards",
          ring_cap=body.cap)
    return res


@app.post("/api/case/{cid}/stepup")
def stepup(cid: str):
    """Fire the step-up challenge and fold the outcome back in as evidence."""
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    prob = c.answer["case"]["fraud_probability"]
    passed = prob < 0.55       # same rule the offline loop uses
    with c.lock:
        c.analyst_signals = [x for x in c.analyst_signals if x.name != "step_up"]
        c.analyst_signals.append(P.Signal(
            "step_up", P.W["customer_confirmed"] if passed else P.W["customer_denied"],
            ("One-time passcode completed from the cardholder's registered number."
             if passed else
             "Step-up authentication was not completed; the challenge expired "
             "unanswered.")
            + " SIMULATED: no authentication responses ship with this dataset, and the "
              "weight is damped accordingly.",
            [], "evidence_request:step_up_auth", source="customer"))
        res = rerun(c)
    c.log("stepup", "passed" if passed else "not completed",
          moved=res["changed"]["probability"])
    return {"passed": passed, **res}


@app.get("/api/case/{cid}/episode")
def episode(cid: str):
    """The transactions around the flagged one, so the pattern can be seen rather than
    asserted. Card testing is three sub-$5 authorisations and then a purchase; that is a
    shape, and a list of 25 identifiers is not."""
    c = get(cid)
    b = backend()
    t = c.trigger
    anchor = dt.datetime.fromisoformat(str(t["opened_at"]))
    lo, hi = anchor - dt.timedelta(hours=72), anchor + dt.timedelta(hours=24)
    df = b.card_window(t["card_id"], lo, hi)
    if not len(df):
        return {"anchor": str(t["flagged_txn_id"]), "txns": []}
    affected = set(c.answer["case"]["affected_txn_ids"])
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "txn_id": str(r.txn_id), "ts": str(r.ts), "amount": float(r.amount),
            "channel": r.channel, "product_cd": r.product_cd or "",
            "risk_score": None if r.risk_score != r.risk_score else float(r.risk_score),
            "in_episode": str(r.txn_id) in affected,
            "anchor": str(r.txn_id) == str(t["flagged_txn_id"]),
        })
    return {"anchor": str(t["flagged_txn_id"]), "window": [str(lo), str(hi)], "txns": rows}


@app.get("/api/case/{cid}/network")
def network(cid: str):
    """The ego-network the investigation actually walked: this card, the device profiles
    it used, the other cards on them, and the closed cases those reach. Assembled from
    what the case already found -- it is a view of the traversal, not a second one."""
    c = get(cid)
    a, t = c.answer, c.trigger
    card = t["card_id"]
    nodes = [{"id": card, "kind": "card", "label": card, "center": True},
             {"id": t["customer_id"], "kind": "customer", "label": t["customer_id"]}]
    edges = [{"from": t["customer_id"], "to": card, "kind": "OWNS"}]

    for d in a["case"]["connected_device_profiles"]:
        nodes.append({"id": d, "kind": "device", "label": d.split(" | ")[0] or "unknown"})
        edges.append({"from": card, "to": d, "kind": "FROM_DEVICE"})
        for other in a["case"]["connected_card_ids"]:
            if not any(n["id"] == other for n in nodes):
                nodes.append({"id": other, "kind": "card", "label": other})
            edges.append({"from": other, "to": d, "kind": "FROM_DEVICE"})

    for pc in a["case"]["similar_prior_cases"][:8]:
        nodes.append({"id": pc, "kind": "closed_case", "label": pc})
        edges.append({"from": card, "to": pc, "kind": "HAS_CLOSED_CASE"})

    ring = getattr(c, "ring_size", None)
    return {"nodes": nodes, "edges": edges, "ring_size": ring,
            "ring_cap": c.ring_cap or 8}


@app.get("/api/closed/{case_id}")
def closed_case(case_id: str):
    """One prior investigation, in full. The chips under `similar_prior_cases` are the
    bank's own closed cases; this is what they actually said."""
    b = backend()
    row = b.closed_case(case_id)
    if not row:
        raise HTTPException(404, f"no closed case {case_id}")
    return {k: (str(v) if isinstance(v, (dt.datetime, dt.date)) else v)
            for k, v in row.items()}


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------
class Decision(BaseModel):
    decision: str = Field(..., pattern="^(approve|override)$")
    action: str | None = None
    note: str = ""


@app.post("/api/case/{cid}/decision")
def decide(cid: str, body: Decision):
    """Approve the recommended set, or override it with one the policy still routes.

    An override does not get a free pass: the action has to be one the policy knows, and
    it is routed by the same table, so an analyst overriding to BLOCK_ALL_CARDS still
    sees the L2 approval it demands.
    """
    c = get(cid)
    final = c.answer["next_best_actions"]["final"]
    if body.decision == "approve":
        c.log("approve", "analyst approved the recommended action set",
              actions=[a["action"] for a in final])
        return {"executed": [a for a in final if a["route"] == "auto"],
                "pending_approval": [a for a in final if a["route"] != "auto"],
                "events": c.events}
    if not body.action:
        raise HTTPException(400, "an override needs an action")
    try:
        route = pol.route_for(body.action, c.answer["case"]["exposure_usd"])
    except ValueError:
        raise HTTPException(400, f"{body.action} is not a policy action")
    c.log("override", body.note or f"analyst overrode to {body.action}",
          action=body.action, route=route,
          replaced=[a["action"] for a in final])
    return {"action": body.action, "route": route, "events": c.events}


class Close(BaseModel):
    outcome: str = Field(..., pattern="^(confirmed_fraud|cleared)$")
    note: str = ""


@app.post("/api/case/{cid}/close")
def close(cid: str, body: Close):
    """Write the analyst's decision into the graph as a ClosedCase.

    This is the memory loop, and it is not a metaphor: prior_cases_for_card and
    prior_cases_for_device read ClosedCase vertices, so the next investigation that
    touches this card or this device retrieves what the analyst just decided. Nothing is
    retrained -- the evidence set grows.
    """
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is already closed")
    a, t = c.answer, c.trigger
    case_id = closed_case_id(cid)
    payload = {
        "p_case_id": case_id, "p_customer_id": t["customer_id"], "p_card_id": t["card_id"],
        "p_opened_at": str(t["opened_at"])[:19],
        "p_closed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "p_outcome": body.outcome,
        "p_pattern": a["case"]["pattern"] if body.outcome == "confirmed_fraud" else "none",
        "p_first_txn_id": a["case"]["first_suspicious_txn_id"],
        "p_n_txns": len(a["case"]["affected_txn_ids"]),
        "p_exposure": a["case"]["exposure_usd"] if body.outcome == "confirmed_fraud" else 0.0,
        "p_actions_taken": "|".join(x["action"] for x in a["next_best_actions"]["final"]),
        "p_report_filed": str(a["sar"]["file"]),
        "p_analyst_notes": body.note or f"Closed from the console as {body.outcome}.",
        "p_txn_ids": a["case"]["affected_txn_ids"],
        "p_connected_cards": a["case"]["connected_card_ids"],
    }
    written = _write_closed_case(payload)
    c.closed = body.outcome
    c.log("close", f"closed as {body.outcome}; written to the graph as {case_id}",
          graph_case_id=case_id, written=written)
    return {"closed_case_id": case_id, "written_to_graph": written, "events": c.events}


class Blacklist(BaseModel):
    device_profile: str
    note: str = ""


@app.post("/api/device/blacklist")
def blacklist(body: Blacklist):
    """Mark a device profile compromised, by recording a confirmed-fraud case against
    the transactions that ran on it. query 7 (prior_cases_for_device) then reaches it
    from any future transaction on the same profile -- so the flag propagates through
    the graph rather than through a side table nothing else reads."""
    b = backend()
    lo = dt.datetime(2016, 1, 1)
    hi = dt.datetime(2017, 1, 1)
    ring = b.device_neighbors(body.device_profile, lo, hi)
    txns = ring["txns"]
    txn_ids = [str(x) for x in (txns.txn_id.tolist() if len(txns) else [])][:200]
    # hash() is salted per process, so the same device profile produced a different case
    # id on every restart and blacklisting it twice made two vertices. A content hash is
    # the same everywhere, which is what an identifier derived from content has to be.
    digest = hashlib.blake2s(body.device_profile.encode(), digest_size=4).hexdigest()
    case_id = f"CC-BL{digest}"
    written = _write_closed_case({
        "p_case_id": case_id, "p_customer_id": "", "p_card_id": "",
        "p_opened_at": "2016-12-31 23:59:59",
        "p_closed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "p_outcome": "confirmed_fraud", "p_pattern": "undocumented",
        "p_first_txn_id": txn_ids[0] if txn_ids else "", "p_n_txns": len(txn_ids),
        "p_exposure": 0.0, "p_actions_taken": "BLOCK_CARD|MONITOR_CONNECTED_CARDS",
        "p_report_filed": "False",
        "p_analyst_notes": body.note or "Device profile blacklisted from the console.",
        "p_txn_ids": txn_ids, "p_connected_cards": ring["cards"][:50],
    })
    return {"closed_case_id": case_id, "written_to_graph": written,
            "cards_now_flagged": ring["cards"][:50], "transactions": len(txn_ids)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ring_size(sig) -> int | None:
    """The component size out of the ring_component claim, for the diff."""
    if sig is None:
        return None
    import re
    m = re.search(r"component of ([\d,]+) cards|component \(([\d,]+) cards\)", sig.claim)
    if not m:
        return None
    return int((m.group(1) or m.group(2)).replace(",", ""))


def _write_closed_case(params) -> bool:
    b = backend()
    if not hasattr(b, "conn"):
        return False          # duckdb mirror: nothing to write to
    b.conn.runInstalledQuery("write_closed_case", params)
    return True


def _signal_names(c: Case) -> dict[str, str]:
    """The signals currently in evidence, name -> claim.

    Read off the investigation's own Signal objects, never recovered from the answer
    file. An earlier version matched claim text against keywords and mis-identified the
    region signal as the match-flag one, because the region claim happens to end "...but
    every card-detail match flag passed". Claims talk about each other; names do not.
    """
    if c.signals is None:
        rerun(c)
    return {s.name: s.claim for s in c.signals
            if s.source == "graph" and not s.name.startswith("withdrawn:")}


# Signal names as patterns.py emits them. An objection withdraws the premise it names.
_OBJECTIONS = [
    (("region", "holiday", "travel", "travelling", "abroad", "trip", "vacation"),
     ("region_new_bad", "region_new_travel")),
    (("new phone", "handset", "upgraded", "new device"), ("device_new",)),
    (("risk score", "model score", "score"), ("risk_score", "risk_score_mid",
                                              "risk_score_high")),
    (("match flag", "m1", "mflag", "match flags"), ("m_flags",)),
    (("burst", "volume", "busy"), ("burst",)),
    (("amount", "large", "outlier"), ("amount_outlier",)),
    (("channel",), ("channel_odd",)),
    (("subscription", "recurring", "monthly"), ("recurring",)),
    (("proxy", "vpn", "ip"), ("proxy", "proxy_noted", "proxy_new_device")),
    (("prior case", "previous case", "past case", "older case"),
     ("prior_fraud", "prior_cleared")),
    (("ring", "component", "shared device"), ("device_ring", "ring_component")),
]


def _match_signals(text: str, names: dict[str, str]) -> list[str]:
    """Map an objection onto the signals in evidence.

    Keyword matching first, because it is deterministic and covers the phrasings an
    analyst actually uses. The LLM is the fallback for the ones it does not, and even
    then its answer is intersected with the signals really present -- it can only choose
    among them, never invent one.
    """
    low = text.lower()
    hit = {n for words, targets in _OBJECTIONS if any(w in low for w in words)
           for n in targets if n in names}
    if hit or not (os.getenv("OPENAI_API_KEY") or os.getenv("SARVAM_API_KEY")):
        return sorted(hit)
    try:
        from llm import LLM
        listing = "\n".join(f"- {n}: {claim[:160]}" for n, claim in names.items())
        out = LLM()._chat(
            "You map a fraud analyst's objection onto the evidence signal it withdraws. "
            "Reply with ONE name from the list, or NONE. Never invent a name, and never "
            "reply with more than one.",
            f"SIGNALS:\n{listing}\n\nOBJECTION: {text}", 40) or ""
        got = sorted({n.strip() for n in out.replace("\n", ",").split(",")} & set(names))
        # An objection withdraws a premise, not the case. A model answering with half
        # the evidence has not understood the question, and acting on it would let an
        # offhand remark gut an investigation, so a wide answer is treated as no answer.
        return got if len(got) <= 2 else []
    except Exception as e:
        print(f"  [console] llm match skipped: {e}")
        return []


load()
