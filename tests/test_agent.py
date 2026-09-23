"""pytest wrapper over the checks this repo already has, plus the console flow.

The pure checks (policy rules, permission boundary, MCP parsing, external lookup) run
anywhere, including CI. The rest need build/fraud.db, which is derived from the
dataset and not committed, so they skip cleanly without it.

  uv run pytest -q
"""
from __future__ import annotations
import importlib, json, os, pathlib, shutil, sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))
HAVE_DATA = (ROOT / "build" / "fraud.db").exists()
needs_data = pytest.mark.skipif(not HAVE_DATA, reason="build/fraud.db not built")


def test_policy_rules():
    importlib.import_module("policy").demo()


def test_permission_boundary(tmp_path, monkeypatch):
    execute = importlib.import_module("execute")
    monkeypatch.setattr(execute, "LEDGER", tmp_path / "ledger.jsonl")
    execute.demo()


def test_mcp_envelope_parsing():
    importlib.import_module("mcp_backend").demo()


def test_external_lookup():
    importlib.import_module("external").demo()


@needs_data
def test_answer_files_validate(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert importlib.import_module("validate").main("cases") in (0, None)


@pytest.fixture(scope="module")
def console(tmp_path_factory):
    """The real server on the DuckDB mirror, writing only into a temp state dir."""
    state = tmp_path_factory.mktemp("state")
    shutil.copy(ROOT / "build" / "triggers.json", state / "triggers.json")
    os.chdir(ROOT)
    os.environ.update(CONSOLE_BACKEND="duckdb", CONSOLE_STATE_DIR=str(state),
                      ANALYST_TOKEN="t-analyst", APPROVER_TOKEN_L1="t-l1",
                      APPROVER_TOKEN_L2="t-l2")
    for m in ("server", "execute", "backend"):
        sys.modules.pop(m, None)
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app), server, state


A = {"X-Analyst-Token": "t-analyst"}


@needs_data
def test_writes_need_a_token(console):
    cl, _, _ = console
    assert cl.get("/api/cases").status_code == 200, "reads stay open"
    assert cl.post("/api/case/HHG-004/reset").status_code == 401
    assert cl.post("/api/case/HHG-004/reset", headers={"X-Analyst-Token": "wrong"}).status_code == 401
    assert cl.post("/api/case/HHG-004/reset", headers=A).status_code == 200


@needs_data
def test_every_request_says_why_and_the_loop_says_where_it_stopped(console):
    cl, _, _ = console
    for c in cl.get("/api/cases").json():
        for q in c["evidence_requests"]:
            assert q["reason"], (c["case_id"], q)
        assert c["stop_reason"].startswith(("Policy 6", "A verification response"))


@needs_data
def test_real_reply_replaces_the_simulation(console):
    cl, _, _ = console
    # whichever case the agent actually asked its cardholder about
    cid, q = next((c, d["evidence_requests"]) for c in (f"HHG-{i:03d}" for i in range(1, 21))
                  for d in [cl.get(f"/api/case/{c}").json()]
                  if d["evidence_requests"] and d["evidence_requests"][0]["type"] == "customer_validation")
    assert q[0]["simulated"]
    r = cl.post(f"/api/case/{cid}/reply", headers=A,
                json={"type": "customer_validation", "outcome": "confirmed", "note": "it was me"})
    assert r.status_code == 200, r.text
    got = r.json()["case"]["evidence_requests"][0]
    assert not got["simulated"] and "REPLY RECEIVED" in got["assumed_response"]
    assert r.json()["changed"]["probability"][1] < r.json()["changed"]["probability"][0]
    asked = {x["type"] for x in r.json()["case"]["evidence_requests"]}
    never = next(t for t in ("analyst_info", "step_up_auth") if t not in asked)
    bad = cl.post(f"/api/case/{cid}/reply", headers=A, json={"type": never, "outcome": "denied"})
    assert bad.status_code == 409, "a reply to a request never made is refused"


@needs_data
def test_close_updates_status_and_survives_restart(console):
    cl, server, state = console
    assert cl.post("/api/case/HHG-004/close", headers=A,
                   json={"outcome": "cleared"}).status_code == 200
    assert cl.get("/api/case/HHG-004").json()["case"]["status"] == "closed_legitimate"
    server.STORE.clear()
    server.load()
    d = cl.get("/api/case/HHG-004").json()
    assert d["closed"] == "cleared" and d["case"]["status"] == "closed_legitimate"
    assert [e["kind"] for e in d["events"]][-1] == "close", "the record survived"
    assert (state / "case_events.jsonl").exists()


@needs_data
def test_release_respects_the_approval_tier(console):
    cl, _, _ = console
    cid = cl.post("/api/cases", headers=A, json={
        "card_id": "C04172-K2", "txn_id": 3416383, "trigger_type": "customer_report"}).json()["case_id"]
    held = cl.post(f"/api/case/{cid}/decision", headers=A,
                   json={"decision": "approve"}).json()["pending_approval"]
    l2 = next(h["action"] for h in held if h["route"] == "L2")
    body = {"action": l2, "approver": "judge"}
    assert cl.post(f"/api/case/{cid}/release", json=body,
                   headers={"X-Approver-Token": "t-l1"}).status_code == 403
    r = cl.post(f"/api/case/{cid}/release", json=body, headers={"X-Approver-Token": "t-l2"})
    assert r.json()["result"]["status"] == "executed"
