"""The simulated core-banking surface, and the ledger of what was done through it.

Policy section 2 says the agent may execute `auto` actions and may only *recommend* the
rest. Until this existed that distinction was a label on a list -- nothing executed, so
nothing demonstrated the permission boundary. The brief allows these to be simulated,
stubbed or mocked; what it does not allow is a system that cannot show the difference
between an action it took and an action it asked for.

Each call returns what the real system would return -- a reference number, a timestamp,
a status -- and is appended to build/action_ledger.jsonl. Append-only, and reloaded at
startup, because a record of decisions that a restart erases is not a record.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, os, pathlib, threading

LEDGER = pathlib.Path(os.getenv("CONSOLE_STATE_DIR", "build")) / "action_ledger.jsonl"
_LOCK = threading.Lock()

# What each action does to the world, in the words the downstream system would use.
# Nothing here reaches a real bank; every line is a stub, and says so in `simulated`.
EFFECTS = {
    "ALLOW_TRANSACTION":       ("authorisation {txn} released to the network", "SWITCH"),
    "DECLINE_TRANSACTION":     ("authorisation {txn} declined at the switch", "SWITCH"),
    "MONITOR_CARD":            ("card {card} flagged for 72h heightened monitoring", "RULES"),
    "MONITOR_CONNECTED_CARDS": ("{n_connected} connected card(s) flagged for monitoring", "RULES"),
    "WARN_CUSTOMER":           ("advisory notice queued to customer {customer}", "COMMS"),
    "VERIFY_WITH_CUSTOMER":    ("verification request sent to customer {customer}", "COMMS"),
    "STEP_UP_AUTH":            ("step-up authentication armed on card {card}", "AUTH"),
    "BLOCK_CARD":              ("card {card} blocked, reissue queued", "CARDS"),
    "BLOCK_ALL_CARDS":         ("every card held by {customer} blocked", "CARDS"),
    "GENERATE_REPORT":         ("internal report generated for case {case}", "CASE"),
    "CREATE_CASE":             ("case {case} opened in the investigation queue", "CASE"),
    "FILE_REPORT":             ("suspicious activity report lodged for case {case}", "REGULATORY"),
    "ESCALATE_TO_ANALYST":     ("case {case} escalated to the fraud analyst queue", "CASE"),
    "CLOSE_NO_FRAUD":          ("alert on card {card} closed as legitimate", "CASE"),
}


def _ref(system: str, case_id: str, action: str) -> str:
    """A reference the downstream system would have handed back. Derived from the inputs
    so re-running a demo produces the same references -- a random one would make every
    screenshot and every transcript disagree with the next."""
    h = hashlib.blake2s(f"{case_id}|{action}".encode(), digest_size=3).hexdigest().upper()
    return f"{system[:3]}-{h}"


def execute(case_id: str, action: dict, ctx: dict, by: str | None = None) -> dict:
    """Run one action, or record that it is waiting for a human.

    `route` decides which: `auto` is the agent's own authority, `L1` and `L2` are not.
    The distinction is enforced here rather than trusted to the caller, because this is
    the only place where something actually happens.
    """
    name, route = action["action"], action["route"]
    template, system = EFFECTS.get(name, ("{action} performed", "UNKNOWN"))
    executed = route == "auto"
    # approving the same case twice must not block a card twice or file two reports
    done = (_find(case_id, name, "executed")
            or _find(case_id, name, "awaiting_approval", route))
    if done:
        return done
    rec = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "case_id": case_id, "action": name, "route": route,
        "status": "executed" if executed else "awaiting_approval",
        "system": system,
        "reference": _ref(system, case_id, name) if executed else None,
        "effect": (template.format(action=name, **ctx) if executed else
                   f"requires {route} approval before {template.format(action=name, **ctx)}"),
        "simulated": True,
        "reason": action.get("reason", ""),
        "requested_by": by,
    }
    append(rec)
    return rec


_CAN_APPROVE = {"L1": {"L1"}, "L2": {"L1", "L2"}}


def release(case_id: str, name: str, approver_level: str, approver: str, ctx: dict) -> dict:
    """A human with the authority the route demands releases a held action.

    L2 may approve L1 work; L1 may never approve L2. Raises PermissionError when the
    approver is below the route, LookupError when nothing is held."""
    done = _find(case_id, name, "executed")
    if done:
        return done
    held = _find(case_id, name, "awaiting_approval")
    if not held:
        raise LookupError(f"no {name} is awaiting approval on {case_id}")
    if held.get("requested_by") and held["requested_by"] == approver:
        raise PermissionError(f"{approver} requested {name}; a second person must release it")
    if held["route"] not in _CAN_APPROVE.get(approver_level, set()):
        raise PermissionError(f"{name} needs {held['route']} approval; "
                              f"{approver_level or 'no level'} cannot release it")
    template, system = EFFECTS.get(name, ("{action} performed", "UNKNOWN"))
    rec = {**held, "at": dt.datetime.now().isoformat(timespec="seconds"),
           "status": "executed", "reference": _ref(system, case_id, name),
           "effect": template.format(action=name, **ctx),
           "approved_by": approver, "approver_level": approver_level}
    append(rec)
    return rec


def _find(case_id, name, status, route=None):
    for r in reversed(history(case_id)):
        if r["action"] == name and r["status"] == status and route in (None, r["route"]):
            return r
    return None


def append(rec: dict) -> None:
    with _LOCK:
        os.makedirs(LEDGER.parent, exist_ok=True)
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps(rec) + "\n")


def history(case_id: str | None = None) -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    return [r for r in rows if case_id is None or r["case_id"] == case_id]


def demo():
    """The permission boundary is the whole point, so it is what gets asserted."""
    ctx = {"txn": "3583368", "card": "C11923-K2", "customer": "C11923",
           "case": "HHG-011", "n_connected": 2}
    auto = execute("_selftest", {"action": "MONITOR_CARD", "route": "auto"}, ctx)
    assert auto["status"] == "executed" and auto["reference"], auto
    assert "C11923-K2" in auto["effect"]

    for route in ("L1", "L2"):
        held = execute("_selftest", {"action": "BLOCK_CARD", "route": route}, ctx)
        assert held["status"] == "awaiting_approval", held
        assert held["reference"] is None, "nothing may get a reference before approval"
        assert route in held["effect"]

    assert all(r["simulated"] for r in history("_selftest"))

    # idempotent: a second approval returns the first execution, not a second one
    n = len(history("_selftest"))
    assert execute("_selftest", {"action": "MONITOR_CARD", "route": "auto"}, ctx) == auto
    assert len(history("_selftest")) == n, "re-approval must not append"

    # release: L1 cannot release L2 work, L2 can; releasing twice is a no-op
    case = f"_selftest-{dt.datetime.now().timestamp()}"
    execute(case, {"action": "FILE_REPORT", "route": "L2"}, ctx)
    try:
        release(case, "FILE_REPORT", "L1", "alice", ctx)
        raise AssertionError("L1 released an L2 action")
    except PermissionError:
        pass
    # four eyes: whoever requested an action cannot release it
    own = f"{case}-own"
    execute(own, {"action": "BLOCK_CARD", "route": "L1"}, ctx, by="carol")
    try:
        release(own, "BLOCK_CARD", "L2", "carol", ctx)
        raise AssertionError("an approver released their own request")
    except PermissionError:
        pass
    r = release(case, "FILE_REPORT", "L2", "bob", ctx)
    assert r["status"] == "executed" and r["reference"] and r["approved_by"] == "bob", r
    assert release(case, "FILE_REPORT", "L2", "bob", ctx) == r
    try:
        release(case, "BLOCK_CARD", "L2", "bob", ctx)
        raise AssertionError("released an action that was never held")
    except LookupError:
        pass
    assert (_ref("CARDS", "HHG-011", "BLOCK_CARD")
            == _ref("CARDS", "HHG-011", "BLOCK_CARD")), "references must be stable"
    assert _ref("CARDS", "HHG-011", "BLOCK_CARD") != _ref("CARDS", "HHG-012", "BLOCK_CARD")
    assert set(EFFECTS) >= {"ALLOW_TRANSACTION", "BLOCK_ALL_CARDS", "FILE_REPORT"}
    print(f"execute.py: {len(EFFECTS)} actions stubbed, permission boundary holds")


if __name__ == "__main__":
    demo()
