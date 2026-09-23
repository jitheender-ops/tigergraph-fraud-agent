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
import datetime as dt, hashlib, hmac, json, os, pathlib, sys, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))

from dotenv import load_dotenv
load_dotenv()
import math

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import answer as ans
import execute as X
import patterns as P
import policy as pol
from backend import ToolLog
from investigate import Investigation, step_up_passes
from run import get_backend

BACKEND = os.getenv("CONSOLE_BACKEND", "tigergraph")
CASES, MONITORING = pathlib.Path("cases"), pathlib.Path("monitoring")
# Everything the console writes lives under one directory, so tests and e2e runs can
# point it somewhere disposable instead of at the real case record.
STATE = pathlib.Path(os.getenv("CONSOLE_STATE_DIR", "build"))
TRIGGERS = STATE / "triggers.json"
API_CASES = STATE / "api_cases"
# The case record -- every challenge, deepen, approve, override, release and close. It
# lived only in memory, so a restart erased the decision history the brief asks for.
EVENTS = STATE / "case_events.jsonl"

app = FastAPI(title="Fraud Investigation Console")
# The console is served through the Vite proxy, so a browser never needs cross-origin
# access. Anything else that wants it has to be named.
app.add_middleware(CORSMiddleware, allow_methods=["GET", "POST"],
                   allow_origins=[o for o in os.getenv(
                       "CONSOLE_ORIGINS", "http://localhost:5173").split(",") if o],
                   allow_headers=["content-type", "x-analyst-token", "x-approver-token"])


def _token_ok(token: str | None) -> bool:
    """An analyst token, or an approver's -- an approver is also an analyst."""
    wants = [os.getenv(k) for k in ("ANALYST_TOKEN", "APPROVER_TOKEN_L1", "APPROVER_TOKEN_L2")]
    return bool(token) and any(w and hmac.compare_digest(token, w) for w in wants)


@app.middleware("http")
async def writes_need_a_token(request: Request, call_next):
    """Every write -- steer, decide, close, blacklist, open, reply, release -- needs a
    token. Checked here, once, so an endpoint added later cannot forget it. Reads stay
    open: the console is a local tool, and a 401 on the case list helps nobody."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        tok = request.headers.get("x-analyst-token") or request.headers.get("x-approver-token")
        if not _token_ok(tok):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={
                "detail": "a valid X-Analyst-Token is required for changes "
                          "(set ANALYST_TOKEN in .env)"})
    return await call_next(request)

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
            "cases": len(STORE), "closed": sum(1 for c in STORE.values() if c.closed),
            "actions_executed": sum(r["status"] == "executed" for r in X.history()),
            "actions_awaiting_approval": sum(r["status"] != "executed" for r in X.history())}


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
        self.features = None       # the feature row, for the step-up simulation
        self.replies: dict[str, dict] = {}   # real replies, by evidence-request type

        # Steering re-runs the investigation and writes back onto this object; two
        # requests for one case must not interleave. FastAPI runs sync endpoints in a
        # threadpool, so this is not theoretical.
        self.lock = threading.Lock()

    def log(self, kind, detail, persist=True, **extra):
        ev = {"at": dt.datetime.now().isoformat(timespec="seconds"),
              "kind": kind, "detail": detail, **extra}
        self.events.append(ev)
        if persist:
            EVENTS.parent.mkdir(exist_ok=True)
            with open(EVENTS, "a") as fh:
                fh.write(json.dumps({"case_id": self.answer["case_id"], **ev}, default=str) + "\n")
        return ev

    def mark_closed(self, outcome):
        """Closing is a status change, not just a flag beside the case file."""
        self.closed = outcome
        self.answer["case"]["status"] = ("closed_fraud" if outcome == "confirmed_fraud"
                                         else "closed_legitimate")


STORE: dict[str, Case] = {}


def load():
    tp = TRIGGERS
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
    for folder, source in ((CASES, "cases"), (MONITORING, "monitoring"), (API_CASES, "api")):
        for p in sorted(folder.glob("*.json")):
            a = json.loads(p.read_text())
            cid = a["case_id"]
            if cid in triggers:
                STORE[cid] = Case(a, triggers[cid], source)
    if EVENTS.exists():
        for line in EVENTS.read_text().splitlines():
            ev = json.loads(line) if line.strip() else {}
            if ev.get("case_id") in STORE:
                c = STORE[ev.pop("case_id")]
                c.events.append(ev)
                # a recorded reply is evidence, so it survives a restart with the case
                if ev.get("kind") == "reply":
                    c.replies[ev["type"]] = {k: ev[k] for k in ("outcome", "note", "at")}
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
            c.mark_closed(row.get("outcome"))
            if not any(e["kind"] == "close" for e in c.events):
                c.log("recovered", f"already closed as {c.closed} in a previous session",
                      persist=False)


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
                      analyst_signals=c.analyst_signals, ring_cap=c.ring_cap,
                      replies=c.replies).run()
    out = ans.build(c.trigger, r, backend=b)
    out["tool_calls"], out["tokens"] = log.count, 0
    c.signals, c.features = r["signals"], r["f"]
    ring = next((x for x in r["signals"] if x.name == "ring_component"), None)
    before_ring, c.ring_size = getattr(c, "ring_size", None), _ring_size(ring)
    before = c.answer
    c.answer = out
    if c.closed:                 # a re-run recomputes status; the analyst's close stands
        c.mark_closed(c.closed)
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
    with c.lock:
        if c.features is None:
            rerun(c)
        passed = step_up_passes(c.features)     # same rule the offline loop uses
        c.analyst_signals = [x for x in c.analyst_signals if x.name != "step_up"]
        c.analyst_signals.append(P.Signal(
            "step_up", P.W["step_up_passed"] if passed else P.W["step_up_failed"],
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


@app.get("/api/intelligence")
def intelligence(min_cases: int = 2):
    """What recurs across cases, rather than what is known about one.

    prior_cases_for_card and prior_cases_for_device are lookups: they answer "what does
    the bank already know about THIS card". A lookup cannot see a pattern. This walks
    every closed case and every case the agent opened and reports the cards and device
    profiles that appear in more than one -- which is the difference between memory you
    can query and memory that tells you something.
    """
    rows = backend().cross_case_entities(min_cases)
    for r in rows:
        r["cases"] = [x for x in str(r.get("cases", "")).split("|") if x]
        r["patterns"] = [x for x in str(r.get("patterns", "")).split("|") if x]
        r["n_cases"] = int(r["n_cases"])
    return {"min_cases": min_cases, "entities": rows,
            "devices": sum(r["kind"] == "device" for r in rows),
            "cards": sum(r["kind"] == "card" for r in rows)}


@app.get("/api/case/{cid}/ledger")
def ledger(cid: str):
    """Everything this case actually did to the world, and everything it is still waiting
    on a human for. Read from build/action_ledger.jsonl, which survives a restart."""
    rows = X.history(cid)
    return {"ledger": rows,
            "executed": sum(r["status"] == "executed" for r in rows),
            "awaiting_approval": sum(r["status"] != "executed" for r in rows)}


def _ctx(c: Case) -> dict:
    """The substitutions the simulated systems fill their messages with."""
    a, t = c.answer, c.trigger
    return {"txn": str(t["flagged_txn_id"]), "card": t["card_id"],
            "customer": t["customer_id"], "case": c.answer["case_id"],
            "n_connected": len(a["case"]["connected_card_ids"])}


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
        edges.append({"from": card, "to": pc, "kind": "RETRIEVED"})

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
    if c.closed:
        raise HTTPException(409, "case is closed")
    final = c.answer["next_best_actions"]["final"]
    if body.decision == "approve":
        done = [X.execute(cid, a, _ctx(c)) for a in final]
        ran = [r for r in done if r["status"] == "executed"]
        held = [r for r in done if r["status"] != "executed"]
        c.log("approve",
              f"{len(ran)} action(s) executed, {len(held)} held for approval",
              actions=[a["action"] for a in final])
        return {"executed": ran, "pending_approval": held, "ledger": X.history(cid),
                "events": c.events}
    if not body.action:
        raise HTTPException(400, "an override needs an action")
    try:
        route = pol.route_for(body.action, c.answer["case"]["exposure_usd"])
    except ValueError:
        raise HTTPException(400, f"{body.action} is not a policy action")
    rec = X.execute(cid, {"action": body.action, "route": route,
                          "reason": body.note or "analyst override"}, _ctx(c))
    c.log("override", body.note or f"analyst overrode to {body.action}",
          action=body.action, route=route,
          replaced=[a["action"] for a in final])
    return {"action": body.action, "route": route, "result": rec,
            "ledger": X.history(cid), "events": c.events}


class Reply(BaseModel):
    type: str = Field(..., pattern="^(customer_validation|step_up_auth|analyst_info)$")
    outcome: str = Field(..., pattern="^(confirmed|denied|no_reply)$")
    note: str = Field("", max_length=500)


@app.post("/api/case/{cid}/reply")
def reply(cid: str, body: Reply):
    """A real answer to a request the agent made. It replaces the simulated one and the
    case is re-investigated on it -- the reply queue the dataset could not supply."""
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    asked = {q["type"] for q in c.answer.get("evidence_requests", [])}
    if body.type not in asked:
        raise HTTPException(409, f"the agent did not request {body.type} on {cid}; "
                                 f"open requests: {sorted(asked) or 'none'}")
    at = dt.datetime.now().isoformat(timespec="seconds")
    with c.lock:
        c.replies[body.type] = {"outcome": body.outcome, "note": body.note.strip(), "at": at}
        res = rerun(c)
    c.log("reply", f"{body.type}: {body.outcome}", type=body.type, outcome=body.outcome,
          note=body.note.strip(), at=at, moved=res["changed"]["probability"])
    return res


class Release(BaseModel):
    action: str
    approver: str = Field(..., min_length=1, max_length=80)


def _approver_level(token: str | None) -> str | None:
    """The approval tier comes from the credential, never from the request body: an
    analyst who could simply say "I am L2" would make the routing table decorative.
    ponytail: static shared tokens from .env; swap for SSO roles before real use."""
    for level in ("L2", "L1"):
        want = os.getenv(f"APPROVER_TOKEN_{level}")
        if want and token and hmac.compare_digest(token, want):
            return level
    return None


@app.post("/api/case/{cid}/release")
def release(cid: str, body: Release, x_approver_token: str | None = Header(None)):
    """Release one action the policy held for L1/L2 approval. Until this existed an
    approved case's BLOCK_CARD sat at awaiting_approval with no way to run it."""
    c = get(cid)
    if c.closed:
        raise HTTPException(409, "case is closed")
    level = _approver_level(x_approver_token)
    if not level:
        raise HTTPException(401, "a valid X-Approver-Token is required "
                                 "(APPROVER_TOKEN_L1 / APPROVER_TOKEN_L2 in .env)")
    try:
        rec = X.release(cid, body.action, level, body.approver, _ctx(c))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    c.log("release", f"{body.approver} ({level}) released {body.action}",
          action=body.action, approver=body.approver, level=level)
    return {"result": rec, "ledger": X.history(cid), "events": c.events}


class NewCase(BaseModel):
    card_id: str
    txn_id: int
    trigger_type: str = Field(..., pattern="^(risk_score|customer_report|analyst_request)$")
    trigger_text: str = ""


@app.post("/api/cases")
def open_case(body: NewCase):
    """Trigger an investigation live. Every other case arrives from a batch run; this is
    the entry point a model alert, a cardholder call or an analyst would actually use."""
    cid = f"API-{body.txn_id}"
    if cid in STORE:
        raise HTTPException(409, f"{cid} is already open")
    log = ToolLog()
    b = backend(log)
    try:
        f = b.features(body.card_id, body.txn_id)
    except (LookupError, IndexError):
        raise HTTPException(404, f"transaction {body.txn_id} is not on card {body.card_id}")
    risk = f.get("risk_score")
    trigger = {
        "case_id": cid, "opened_at": f["ts"], "trigger_type": body.trigger_type,
        "trigger_text": body.trigger_text or f"{body.trigger_type} raised via the console API.",
        "flagged_txn_id": body.txn_id, "card_id": body.card_id,
        "customer_id": f["customer_id"],
        "risk_score": None if risk is None or risk != risk else float(risk),
    }
    r = Investigation(b, trigger).run()
    out = ans.build(trigger, r, backend=b)
    out["tool_calls"], out["tokens"] = log.count, 0
    # persisted beside the other answer files so a restart does not lose the case
    API_CASES.mkdir(parents=True, exist_ok=True)
    (API_CASES / f"{cid}.json").write_text(json.dumps(out, indent=2, default=str))
    have = json.loads(TRIGGERS.read_text()) if TRIGGERS.exists() else {}
    TRIGGERS.write_text(json.dumps({**have, cid: trigger}, indent=1, default=str))
    c = STORE[cid] = Case(out, trigger, "api")
    c.signals, c.features = r["signals"], r["f"]
    c.log("open", f"investigation triggered by {body.trigger_type}")
    return {"case_id": cid, **out}


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
    c.mark_closed(body.outcome)
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
    # Memory queries only count cases opened before the investigation's own date, so a
    # blacklist stamped at year end was invisible to every 2016 case. Stamp it just
    # before the device's first transaction: the verdict covers everything it ran.
    first = (txns.ts.min() - dt.timedelta(seconds=1)) if len(txns) else dt.datetime(2016, 1, 1)
    written = _write_closed_case({
        "p_case_id": case_id, "p_customer_id": "", "p_card_id": "",
        "p_opened_at": first.strftime("%Y-%m-%d %H:%M:%S"),
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
    return backend().write_closed_case(params)


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
