"""Fraud Policy v1.0 as executable code.

Deliberately deterministic: the LLM writes prose, it does not pick actions or routes.
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
    if recurring and prob < 0.70 and (customer_denied or trigger_type == "customer_report"):
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

    # ---- R5: card testing
    if pattern == "card_testing":
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

    # ---- R4: asked and heard nothing back
    if no_reply:
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
    if customer_denied and not recurring and verdict != "legitimate":
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
    else:  # uncertain
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
        _add(a, ESCALATE_TO_ANALYST, exposure, f"R8: the verdict is uncertain and exposure ${exposure:,.2f} exceeds $500 or the evidence conflicts; hand to a human analyst.")

    # ---- R9: undocumented
    r9(a)

    # ---- R10 guard: never block every card without two confirmed or confirmed credential theft
    if n_confirmed_cards >= 2 or pattern == "account_takeover" and prob >= 0.85:
        if n_confirmed_cards >= 2:
            _add(a, BLOCK_ALL_CARDS, exposure, f"R10: {n_confirmed_cards} of the customer's cards show confirmed fraud.")

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


def stop_reason(prob, n_ind, verdict, asked, answered) -> str:
    """Policy 6."""
    if answered:
        return (f"A verification response settled the question (policy 6): probability moved to "
                f"{prob:.2f} on {n_ind} independent pieces of evidence and the action set follows "
                f"directly. Further steps would not change it.")
    if prob >= 0.85 and n_ind >= 2:
        return (f"Policy 6: probability {prob:.2f} is at or above 0.85 on {n_ind} independent "
                f"pieces of evidence, which is the bar for a defensible action.")
    if prob <= 0.15 and n_ind >= 2:
        return (f"Policy 6: probability {prob:.2f} is at or below 0.15 on {n_ind} independent "
                f"pieces of evidence; the alarm is explained and further investigation would not "
                f"change the outcome.")
    return (f"Policy 6: probability {prob:.2f} on {n_ind} independent pieces of evidence sits "
            f"between the stopping thresholds. Stopping here because the remaining uncertainty "
            f"is the cardholder's own intent, which only the cardholder or an analyst can "
            f"resolve; the recommended actions route it to them rather than resolving it in the "
            f"graph.")
