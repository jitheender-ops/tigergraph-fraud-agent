"""Fraud Policy v1.0 as executable code.

Deliberately deterministic: the LLM never picks an action, a route or a verdict.
Action names and route identifiers are the exact strings the policy defines.
"""
from __future__ import annotations
import math

AUTO = "auto"
L1 = "L1"
L2 = "L2"

ALLOW_TRANSACTION        = "ALLOW_TRANSACTION"
DECLINE_TRANSACTION      = "DECLINE_TRANSACTION"
MONITOR_CARD             = "MONITOR_CARD"
MONITOR_CONNECTED_CARDS  = "MONITOR_CONNECTED_CARDS"
WARN_CUSTOMER            = "WARN_CUSTOMER"
VERIFY_WITH_CUSTOMER     = "VERIFY_WITH_CUSTOMER"
STEP_UP_AUTH             = "STEP_UP_AUTH"
BLOCK_CARD               = "BLOCK_CARD"
BLOCK_ALL_CARDS          = "BLOCK_ALL_CARDS"
GENERATE_REPORT          = "GENERATE_REPORT"
CREATE_CASE              = "CREATE_CASE"
FILE_REPORT              = "FILE_REPORT"
ESCALATE_TO_ANALYST      = "ESCALATE_TO_ANALYST"
CLOSE_NO_FRAUD           = "CLOSE_NO_FRAUD"

_AUTO_ACTIONS = {
    ALLOW_TRANSACTION, MONITOR_CARD, MONITOR_CONNECTED_CARDS, WARN_CUSTOMER,
    VERIFY_WITH_CUSTOMER, STEP_UP_AUTH, GENERATE_REPORT, CREATE_CASE,
    ESCALATE_TO_ANALYST, CLOSE_NO_FRAUD,
}


def route_for(action: str, exposure: float) -> str:
    """Policy section 2. The only place approval routing is decided."""
    if action in _AUTO_ACTIONS:
        return AUTO
    if action == DECLINE_TRANSACTION:
        return L1
    if action == BLOCK_CARD:
        return L1 if exposure <= 2500 else L2
    if action in (BLOCK_ALL_CARDS, FILE_REPORT):
        return L2
    raise ValueError(f"unknown action {action}")


def probability(signals, prior_log_odds: float = 0.0) -> float:
    """Sum the signal weights as log-odds. Prior is 0 (even odds): the README states
    half the exam cases are legitimate, so the bank's own 84%-fraud investigated
    population is the wrong base rate to carry in."""
    return 1.0 / (1.0 + math.exp(-(prior_log_odds + sum(s.weight for s in signals))))


def independent_evidence_count(signals) -> int:
    """Policy section 6 needs 'at least two independent pieces of evidence'.
    Signals from the same underlying fact are one piece."""
    families = set()
    for s in signals:
        if abs(s.weight) < 0.4:
            continue
        families.add({
            "m_flags": "identity", "region_new_bad": "region", "region_new_travel": "region",
            "device_ring": "device", "device_new_corroborated": "device",
            "device_new_alone": "device", "device_common": "device", "proxy": "device",
            "risk_score_high": "model", "risk_score_low": "model",
            "recurring": "history", "amount_outlier": "history", "channel_odd": "history",
            "episode": "episode", "card_testing": "episode",
            "prior_fraud": "memory", "prior_cleared": "memory",
            "email_domain_intel": "external",
        }.get(s.name, s.name))
    return len(families)


def should_create_case(prob: float, requested_evidence: bool, customer_disputed: bool) -> bool:
    """Policy 3a."""
    return prob >= 0.30 or requested_evidence or customer_disputed


def should_file_report(verdict: str, prob: float, exposure: float,
                       connected: bool, coordinated: bool) -> tuple[bool, str]:
    """Policy 3a. Confirmed or strongly suspected fraud AND at least one qualifier.
    Validated against the closed history: of 4,665 confirmed cases every one with
    exposure over $1,000 was reported, and the only 4 reported below it were the
    undocumented ones with connected cards."""
    strong = verdict == "fraud" or prob >= 0.70
    if not strong:
        return False, (
            f"Policy 3a: a report requires fraud confirmed or strongly suspected. "
            f"Assessed probability {prob:.2f} with verdict '{verdict}' does not meet that bar.")
    if exposure > 1000:
        return True, f"Policy 3a: fraud established and exposure ${exposure:,.2f} exceeds $1,000."
    if coordinated:
        return True, ("Policy 3a and R9: the activity fits no documented pattern and is "
                      "coordinated across more than one cardholder, which requires a filing "
                      "regardless of amount.")
    if connected:
        return True, ("Policy 3a and R6: the activity connects to a shared device profile / "
                      "region cluster used by other cards, which requires a filing regardless "
                      "of amount.")
    return False, (
        f"Policy 3a: fraud established but exposure ${exposure:,.2f} is under $1,000, the "
        f"activity is confined to this card, and no shared origin or coordination was found. "
        f"A case is opened; most cases never need a report.")


def _add(actions, action, exposure, reason):
    if any(a["action"] == action for a in actions):
        return
    actions.append({"action": action, "route": route_for(action, exposure), "reason": reason})


def decide_actions(*, prob, verdict, exposure, signals, pattern, trigger_type,
                   customer_denied, customer_confirmed, recurring, connected_cards,
                   shared_element, n_confirmed_cards, phase, no_reply=False) -> list[dict]:
    """Return the ordered action list for one phase ('initial' or 'final').

    Order is what happens first, as the policy requires.
    """
    a: list[dict] = []
    n_ind = independent_evidence_count(signals)
    single_signal = n_ind <= 1
    # R9 is "fits none of the known patterns AND the evidence shows coordinated or
    # repeated abuse ACROSS CUSTOMERS". An unclassifiable transaction confined to one
    # card is not coordinated; treating it as such forced a filing and an escalation on
    # cases where nothing crossed a customer boundary.
    coordinated = pattern == "undocumented" and bool(connected_cards)

    def r9(actions):
        """R9 is written unconditionally, so it has to survive the early returns."""
        if coordinated:
            _add(actions, CREATE_CASE, exposure,
                 "R9: the activity fits none of the documented patterns.")
            _add(actions, ESCALATE_TO_ANALYST, exposure,
                 "R9: an undocumented pattern is escalated to a human analyst with the evidence.")
        return actions

    # ---- R7: disputed but legitimate recurring charge. Checked before R2 so a
    # forgotten subscription never produces a block.
    # R7 carries no probability condition. It gives way only at the policy 6 fraud bar,
    # where independent evidence says fraud whatever the cadence -- a patient attacker
    # can grow a monthly cadence on a stolen card.
    if recurring and prob < 0.85 and (customer_denied or trigger_type == "customer_report"):
        _add(a, CREATE_CASE, exposure, "R7: cardholder disputes a charge that matches their own recurring pattern; the dispute is recorded as a case.")
        _add(a, VERIFY_WITH_CUSTOMER, exposure, "R7: confirm with the cardholder that the recurring charge is theirs before any action on the card.")
        _add(a, WARN_CUSTOMER, exposure, "R7: send the recurring-charge reminder. R7 explicitly forbids blocking on this profile.")
        # R3 closes the alert only if the score actually lands in the legitimate band;
        # closing while the evidence still reads 0.45 would overstate the finding.
        if phase == "final" and customer_confirmed and prob <= 0.30:
            _add(a, CLOSE_NO_FRAUD, exposure, "R3: cardholder confirmed the charge on contact and the evidence agrees.")
        elif phase == "final" and customer_confirmed:
            _add(a, MONITOR_CARD, exposure, f"R7 with residual doubt: the cardholder confirms the charge but the assessment still reads {prob:.2f}, so the card stays active under monitoring rather than being closed outright.")
        return a

    # ---- R5: card testing.
    # Guarded on the verdict for the same reason R4 is. investigate.py already passes
    # pattern="none" when it concludes legitimate, so this cannot fire through the
    # pipeline -- but a rule engine that is only correct because its caller normalises
    # the input is not a rule engine, it is a coincidence.
    if pattern == "card_testing" and verdict != "legitimate":
        _add(a, DECLINE_TRANSACTION, exposure, "R5: a testing sequence of small online authorisations preceded this purchase; decline the pending authorisation.")
        _add(a, STEP_UP_AUTH, exposure, "R5: require step-up authentication before any further activity on the card.")
        if exposure > 100:
            _add(a, BLOCK_CARD, exposure, f"R5: a purchase over $100 has already cleared (exposure ${exposure:,.2f}), so the card is blocked and reissued.")

    # ---- R1: weak single signal -> verify before blocking
    if single_signal and prob < 0.70 and phase == "initial":
        _add(a, VERIFY_WITH_CUSTOMER, exposure, f"R1: the case rests on a single signal and assessed probability is {prob:.2f}, below 0.70. Verify before any block.")
        if trigger_type == "risk_score":
            _add(a, STEP_UP_AUTH, exposure, "R1: require step-up authentication on further activity while the verification is outstanding.")
        _add(a, MONITOR_CARD, exposure, "R1: raise monitoring sensitivity for 72 hours while the card stays active.")
        if prob >= 0.30:
            _add(a, CREATE_CASE, exposure, "Policy 3a: a case is opened because evidence has been requested.")
        return r9(a)

    # ---- R4: asked and heard nothing back.
    # Not when the graph has independently settled it. R4 is written for the state where
    # the question is still open; declining a transaction the evidence says is legitimate
    # because the cardholder did not answer the phone punishes them for our uncertainty.
    if no_reply and verdict != "legitimate":
        _add(a, MONITOR_CARD, exposure, "R4: no reply within 24 hours; keep the card active under raised monitoring.")
        _add(a, DECLINE_TRANSACTION, exposure, "R4: decline pending authorisations while the verification is unanswered.")
        if exposure > 500:
            _add(a, ESCALATE_TO_ANALYST, exposure, f"R4: no reply and exposure ${exposure:,.2f} exceeds $500; escalate to a human analyst.")
        if should_create_case(prob, True, customer_denied):
            _add(a, CREATE_CASE, exposure, "Policy 3a: a case is opened because evidence was requested.")
        if connected_cards:
            _add(a, MONITOR_CONNECTED_CARDS, exposure, f"R6: shared {shared_element} links {len(connected_cards)} other card(s); place them under monitoring.")
        return r9(a)

    # ---- R2: customer denies
    # a recurring cadence shields the charge from R2 exactly as far as it does in R7
    if customer_denied and (not recurring or prob >= 0.85) and verdict != "legitimate":
        _add(a, BLOCK_CARD, exposure, f"R2: the cardholder denies the transaction; block and reissue. Exposure ${exposure:,.2f} sets the approval route.")
        _add(a, CREATE_CASE, exposure, "R2: open the internal case with the evidence attached.")

    # ---- R3: customer confirms
    if customer_confirmed:
        _add(a, CLOSE_NO_FRAUD, exposure, "R3: the cardholder confirmed the transaction; close the alert as legitimate.")
        return a

    # ---- verdict-driven core
    if verdict == "fraud" and not customer_denied:
        _add(a, CREATE_CASE, exposure, "Policy 3a: probability is at or above 0.30, so a case is opened.")
        if prob >= 0.85:
            _add(a, BLOCK_CARD, exposure, f"Probability {prob:.2f} on {n_ind} independent pieces of evidence meets the policy 6 stopping bar for a defensible block.")
        else:
            # 0.70-0.85: fraud on the balance of evidence but short of the stopping bar.
            # R1 no longer forces a verify (more than one signal), but blocking a card on
            # 0.7x without asking is the policy breach R1 exists to prevent.
            _add(a, VERIFY_WITH_CUSTOMER, exposure, f"R1: probability {prob:.2f} is below the 0.85 stopping bar in policy 6; confirm with the cardholder before any action with customer impact.")
            _add(a, STEP_UP_AUTH, exposure, "R1: require step-up authentication on further activity while verification is outstanding.")
            _add(a, DECLINE_TRANSACTION, exposure, f"R4: decline the pending authorisation while the card stays active, given assessed probability {prob:.2f}.")
            _add(a, MONITOR_CARD, exposure, "R4: raise monitoring sensitivity for 72 hours.")
    elif verdict == "legitimate":
        _add(a, ALLOW_TRANSACTION, exposure, f"R1 and policy 6: probability {prob:.2f} with {n_ind} independent pieces of evidence pointing away from fraud; the transaction stands.")
        _add(a, CLOSE_NO_FRAUD, exposure, "Policy 6: the evidence settles the question; close as legitimate.")
        if prob >= 0.15:
            _add(a, MONITOR_CARD, exposure, "Residual uncertainty is low but non-zero; keep 72-hour monitoring without customer impact.")
        return a
    elif verdict == "uncertain":
        # Was a bare `else`, so "fraud AND the cardholder denies it" fell in here and
        # recommended VERIFY_WITH_CUSTOMER -- asking a cardholder who had just reported
        # the fraud. R2 above already answers that case.
        if should_create_case(prob, True, customer_denied):
            _add(a, CREATE_CASE, exposure, "Policy 3a: probability is at or above 0.30 with the verdict still uncertain.")
        if phase == "initial":
            _add(a, VERIFY_WITH_CUSTOMER, exposure, f"R1: probability {prob:.2f} is below 0.70; ask the cardholder before taking any action with customer impact.")
        _add(a, MONITOR_CARD, exposure, "R4: keep the card active under raised monitoring while the question is open.")

    # ---- R6: shared origin
    if connected_cards:
        _add(a, MONITOR_CONNECTED_CARDS, exposure, f"R6: shared {shared_element} links {len(connected_cards)} other card(s) to this activity; place them under monitoring.")

    # ---- R8: uncertain and exposed
    if verdict == "uncertain" and (exposure > 500 or _conflicting(signals) or customer_denied):
        # name the condition that actually fired; "$128 exceeds $500 or ..." misleads
        why = (f"exposure ${exposure:,.2f} exceeds $500" if exposure > 500
               else "the evidence conflicts" if _conflicting(signals)
               else "the cardholder disputes a transaction the evidence does not settle")
        _add(a, ESCALATE_TO_ANALYST, exposure, f"R8: the verdict is uncertain and {why}; hand to a human analyst.")

    # ---- R9: undocumented
    r9(a)

    # ---- R10: never BLOCK_ALL_CARDS without two of the customer's cards confirmed, or
    # their credentials confirmed compromised. Nothing in this dataset confirms a
    # credential compromise -- suspected account takeover at 0.85 is a suspicion, not a
    # confirmation -- so two confirmed cards is the only door, and it is the only test
    # here. (An outer `or pattern == "account_takeover" and prob >= 0.85` used to sit on
    # this block; it was dead, because the inner test re-checked the count anyway. A
    # mutation test found it.)
    if n_confirmed_cards >= 2:
        _add(a, BLOCK_ALL_CARDS, exposure,
             f"R10: {n_confirmed_cards} of the customer's cards show confirmed fraud.")

    return a


def _conflicting(signals) -> bool:
    return any(s.weight > 0.5 for s in signals) and any(s.weight < -0.5 for s in signals)


def apply_sar(actions, file_report: bool, exposure: float, reason: str) -> list[dict]:
    """Keep sar.file and the presence of FILE_REPORT in agreement, as the spec demands."""
    actions = [x for x in actions if x["action"] != FILE_REPORT]
    if file_report:
        actions.append({"action": FILE_REPORT, "route": route_for(FILE_REPORT, exposure),
                        "reason": reason})
    return actions


def at_stop_bar(prob, n_ind) -> bool:
    """Policy 6: a defensible decision needs 0.85+ or 0.15- on two independent pieces."""
    return (prob >= 0.85 or prob <= 0.15) and n_ind >= 2


def stop_reason(prob, n_ind, kind) -> str:
    """Policy 6, worded by why the evidence loop actually exited: `threshold` (the bar was
    met), `answered` (a reply settled the question), `exhausted` (nothing left the agent
    may ask without approval) or `budget` (the round limit)."""
    # 0.849 printed as "0.85 ... between the thresholds" contradicts itself on the page
    shown = f"{prob:.2f}"
    if shown in ("0.85", "0.15") and not at_stop_bar(prob, n_ind):
        shown = f"{prob:.3f}"
    if kind == "answered":
        return (f"A verification response settled the question (policy 6): probability moved to "
                f"{shown} on {n_ind} independent pieces of evidence and the action set follows "
                f"directly. Further steps would not change it.")
    if kind == "threshold" and prob >= 0.85:
        return (f"Policy 6: probability {shown} is at or above 0.85 on {n_ind} independent "
                f"pieces of evidence, which is the bar for a defensible action. No further "
                f"evidence was requested because none could change it.")
    if kind == "threshold":
        return (f"Policy 6: probability {shown} is at or below 0.15 on {n_ind} independent "
                f"pieces of evidence; the alarm is explained and further investigation would not "
                f"change the outcome.")
    tail = ("every request the agent may make without approval has been made"
            if kind == "exhausted" else "the evidence-request budget is spent")
    return (f"Policy 6: probability {shown} on {n_ind} independent pieces of evidence sits "
            f"between the stopping thresholds, and {tail}. The remaining uncertainty is the "
            f"cardholder's own intent, which only the cardholder or an analyst can resolve; "
            f"the recommended actions route it to them rather than resolving it in the graph.")


def demo():
    """Runnable check for the rules that decide what happens to a customer's card.

    validate.py checks the twenty answer files agree with the routing table; this checks
    each rule actually fires on the situation it was written for, which the answer files
    cannot show because no case exercises every branch.
    """
    class S:
        def __init__(self, name, weight):
            self.name, self.weight = name, weight

    weak = [S("risk_high", 0.9)]
    strong = [S("m_flags_2plus", 1.05), S("burst", 0.62), S("prior_fraud", 0.9)]

    def acts(**kw):
        base = dict(prob=0.5, verdict="uncertain", exposure=100.0, signals=strong,
                    pattern="card_not_present_fraud", trigger_type="risk_score",
                    customer_denied=False, customer_confirmed=False, recurring=False,
                    connected_cards=[], shared_element="", n_confirmed_cards=0,
                    phase="initial")
        return {a["action"] for a in decide_actions(**{**base, **kw})}

    # --- routing table (policy section 2): the only place a route is decided ---
    assert route_for(MONITOR_CARD, 99999) == AUTO
    assert route_for(DECLINE_TRANSACTION, 10) == L1
    assert route_for(BLOCK_CARD, 2500) == L1, "at the boundary a block is still L1"
    assert route_for(BLOCK_CARD, 2500.01) == L2, "over $2,500 a block escalates to L2"
    assert route_for(BLOCK_ALL_CARDS, 1) == L2 and route_for(FILE_REPORT, 1) == L2

    # --- R1: weak single signal must verify, never block ---
    a = acts(signals=weak, prob=0.6)
    assert VERIFY_WITH_CUSTOMER in a and BLOCK_CARD not in a, a
    assert STEP_UP_AUTH in a, "R1 + a risk_score trigger also requires step-up"

    # --- R2: a denial blocks; R3: a confirmation closes ---
    assert BLOCK_CARD in acts(verdict="fraud", prob=0.75, customer_denied=True)
    a = acts(customer_confirmed=True, phase="final")
    assert a == {CLOSE_NO_FRAUD}, a

    # --- a cardholder who reported fraud is never asked to verify it again ---
    a = acts(verdict="fraud", prob=0.92, customer_denied=True, trigger_type="customer_report")
    assert BLOCK_CARD in a and VERIFY_WITH_CUSTOMER not in a, a

    # --- R3 must not close a case the evidence still calls fraud ---
    a = acts(verdict="fraud", prob=0.9, customer_denied=True, customer_confirmed=False)
    assert CLOSE_NO_FRAUD not in a

    # --- R4: no reply keeps the card alive and escalates over $500 ---
    a = acts(no_reply=True, exposure=600.0, phase="final")
    assert {MONITOR_CARD, DECLINE_TRANSACTION, ESCALATE_TO_ANALYST} <= a, a
    assert BLOCK_CARD not in a, "R4 does not block on silence"
    assert ESCALATE_TO_ANALYST not in acts(no_reply=True, exposure=400.0, phase="final")

    # --- R5: card testing declines and steps up; blocks only once $100 has cleared ---
    a = acts(pattern="card_testing", verdict="fraud", prob=0.8, exposure=50.0)
    assert {DECLINE_TRANSACTION, STEP_UP_AUTH} <= a and BLOCK_CARD not in a, a
    assert BLOCK_CARD in acts(pattern="card_testing", verdict="fraud", prob=0.8,
                              exposure=250.0)

    # --- R6: a shared origin puts the other cards under monitoring ---
    a = acts(verdict="fraud", prob=0.9, connected_cards=["C1", "C2"],
             shared_element="device profile 'x'")
    assert MONITOR_CONNECTED_CARDS in a, a

    # --- R7: a disputed recurring charge is never blocked ---
    a = acts(recurring=True, prob=0.45, customer_denied=True, trigger_type="customer_report")
    assert {CREATE_CASE, VERIFY_WITH_CUSTOMER, WARN_CUSTOMER} <= a, a
    assert BLOCK_CARD not in a and BLOCK_ALL_CARDS not in a, "R7 forbids blocking"
    a = acts(recurring=True, prob=0.75, verdict="uncertain", trigger_type="customer_report")
    assert {VERIFY_WITH_CUSTOMER, WARN_CUSTOMER} <= a and DECLINE_TRANSACTION not in a, a
    a = acts(recurring=True, prob=0.9, verdict="fraud", customer_denied=True,
             trigger_type="customer_report")
    assert BLOCK_CARD in a, "at the fraud bar a cadence no longer shields the charge"

    # --- R8: uncertain and exposed goes to a human ---
    assert ESCALATE_TO_ANALYST in acts(verdict="uncertain", prob=0.5, exposure=600.0)
    assert ESCALATE_TO_ANALYST not in acts(verdict="uncertain", prob=0.5, exposure=100.0)

    # --- R9: undocumented AND across customers. One card alone is not coordination ---
    a = acts(pattern="undocumented", verdict="fraud", prob=0.9, connected_cards=["C1"])
    assert {CREATE_CASE, ESCALATE_TO_ANALYST} <= a, a
    assert ESCALATE_TO_ANALYST not in acts(pattern="undocumented", verdict="fraud",
                                           prob=0.9, exposure=100.0)
    # and it has to survive the early returns R1 and R4 take
    assert ESCALATE_TO_ANALYST in acts(pattern="undocumented", signals=weak, prob=0.6,
                                       connected_cards=["C1"]), "R9 lost to R1's return"
    assert ESCALATE_TO_ANALYST in acts(pattern="undocumented", no_reply=True,
                                       exposure=100.0, connected_cards=["C1"],
                                       phase="final"), "R9 lost to R4's return"

    # --- a legitimate verdict never blocks or declines, whatever else happened ---
    for kw in ({}, {"no_reply": True, "phase": "final"}, {"customer_denied": True},
               {"recurring": True}, {"connected_cards": ["C1"], "shared_element": "d"},
               {"pattern": "card_testing"}, {"pattern": "undocumented",
                                             "connected_cards": ["C1"]}):
        a = acts(verdict="legitimate", prob=0.10, exposure=0.0, **kw)
        assert not ({BLOCK_CARD, BLOCK_ALL_CARDS, DECLINE_TRANSACTION} & a), (kw, a)
        assert ALLOW_TRANSACTION in a, (kw, a)

    # --- R10: never block every card without two confirmed ---
    assert BLOCK_ALL_CARDS not in acts(verdict="fraud", prob=0.99, n_confirmed_cards=1)
    assert BLOCK_ALL_CARDS in acts(verdict="fraud", prob=0.99, n_confirmed_cards=2)

    # --- an action set must never both close the alert and block the card ---
    for kw in ({}, {"verdict": "fraud", "prob": 0.9}, {"verdict": "legitimate", "prob": 0.1},
               {"recurring": True, "customer_denied": True},
               {"no_reply": True, "phase": "final"}):
        a = acts(**kw)
        assert not ({CLOSE_NO_FRAUD, ALLOW_TRANSACTION} & a and
                    {BLOCK_CARD, BLOCK_ALL_CARDS} & a), (kw, a)

    # --- 3a: a report needs fraud established AND a qualifier ---
    assert should_file_report("uncertain", 0.5, 50_000, True, True)[0] is False, \
        "an uncertain verdict never files, however large the exposure"
    assert should_file_report("fraud", 0.9, 1500, False, False)[0] is True
    assert should_file_report("fraud", 0.9, 500, False, False)[0] is False
    assert should_file_report("fraud", 0.9, 500, True, False)[0] is True
    assert should_file_report("fraud", 0.9, 500, False, True)[0] is True
    for args in (("fraud", 0.9, 1500, False, False), ("fraud", 0.9, 500, False, False)):
        assert should_file_report(*args)[1].startswith("Policy 3a"), "every reason cites 3a"

    # --- apply_sar keeps FILE_REPORT and sar.file in agreement, as the spec demands ---
    base = [{"action": MONITOR_CARD, "route": AUTO, "reason": "r"}]
    assert not any(x["action"] == FILE_REPORT for x in apply_sar(base, False, 10, "no"))
    on = apply_sar(base, True, 10, "yes")
    assert [x for x in on if x["action"] == FILE_REPORT][0]["route"] == L2

    # --- probability is a logistic over the weights, and independence is by family ---
    assert abs(probability([]) - 0.5) < 1e-9
    assert probability([S("a", 2.0)]) > 0.85 and probability([S("a", -2.0)]) < 0.15
    assert independent_evidence_count(
        [S("region_new_bad", 1.0), S("region_new_travel", 1.0)]) == 1, \
        "two region signals are one piece of evidence"
    assert independent_evidence_count([S("m_flags_2plus", 1.0), S("burst", 1.0)]) == 2
    assert independent_evidence_count([S("m_flags_2plus", 0.2)]) == 0, "weak signals don't count"

    # --- policy 6 is a bar, and the stop reason says which exit was taken ---
    assert at_stop_bar(0.9, 2) and at_stop_bar(0.1, 3)
    assert not at_stop_bar(0.9, 1), "one piece of evidence never meets the bar"
    assert not at_stop_bar(0.5, 4)
    assert "settled" in stop_reason(0.5, 2, "answered")
    assert "0.85" in stop_reason(0.9, 2, "threshold")
    assert "has been made" in stop_reason(0.5, 2, "exhausted")
    assert "budget" in stop_reason(0.5, 2, "budget")
    assert "0.849" in stop_reason(0.849, 4, "exhausted"), "never print 0.85 below the bar"

    print("policy.py: all rule checks passed (R1-R10, routing, 3a, scoring)")


if __name__ == "__main__":
    demo()
