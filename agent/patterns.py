"""Pattern classification and fraud scoring.

Every weight here was read off the 5,565 closed cases (agent/../prep/calibrate.py),
not invented. The signatures below are the per-pattern means that separated them:

                      dev_specific dev_new m_false dev_cards risk  burst48
  out_of_region_use       0.00      0.00    2.08      0.0    0.47   18.2
  account_takeover        0.07      0.03    1.77     12.4    0.49   14.9
  card_not_present        0.95      0.00    0.00    195.8    0.47    5.7
  cnp_new_device          0.98      0.74    0.00    208.6    0.47    9.1
  card_testing            1.00      0.19    0.00    225.8    0.54   52.6
  undocumented            1.00      0.78    0.00    183.4    0.18    1.7  (proxy .44, channel_odd .44)
  cleared / none          0.85      0.83    0.29    149.1    0.88    7.7

Two of those columns invert naive intuition and drive most of the accuracy:
  * risk_score is HIGHER on cleared alarms (0.88) than on confirmed fraud (0.47).
  * a New device is mostly a CLEARED signal (0.83 vs 0.18) -- people buy phones.
Neither is usable as a fraud signal on its own; both are usable as exculpatory ones.
"""
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# WEIGHTS
#
# Log-odds contributions, measured as ln of the likelihood ratio
#   P(condition | confirmed_fraud) / P(condition | cleared)
# over the 4,665 confirmed and 900 cleared closed cases. Reproduce with
# `uv run python prep/calibrate.py`.
#
# Three measured conditions are deliberately NOT used at their measured value,
# because the closed-case sample is not a random sample of legitimate activity.
# Every cleared case in the history was opened from a high model score and closed
# as travel or a new handset, so anything correlated with *how alarms were
# selected* is confounded:
#
#   risk_score <= 0.30   measured +6.38  (1,526 fraud cases, ZERO cleared) -- DROPPED.
#                        No cleared case exists below 0.30 because none was ever
#                        opened below 0.30. Using it would mark every quiet
#                        transaction as fraud.
#   risk_score >= 0.85   measured -2.12  -- DAMPED to -0.80. The direction is
#                        independently asserted by the dataset README ("above 0.7,
#                        most flagged transactions turn out to be legitimate"), so
#                        it is real, but the magnitude is inflated by selection.
#   device New           measured -1.53  -- DAMPED to -1.00, same reasoning.
#
# Four of the weights originally written here by intuition had the WRONG SIGN
# against measurement and were corrected: a corroborated new device (-1.85, not
# +1.0), proxy use (-1.39, not +1.2), a 4x amount outlier (-0.97, not +0.6) and
# odd channel (-0.99, not +0.8). A rare shared device profile measured +0.06
# marginally and a >=3-card ring measured -0.32, so neither carries weight; the
# ring is still reported as evidence because policy R6 asks for the shared element
# to be named, but it does not move the probability on its own.
W = {
    "m_flags_2plus":     1.05,   # measured
    "m_flags_1":         1.00,   # measured +1.24, flattened: non-monotonic on small n
    "risk_high":        -0.80,   # >=0.85: measured -2.12, damped (selection)
    "risk_mid":         -0.40,   # 0.70-0.85: measured, and the ONLY band besides the one
                                 # above where both classes are present. Every one of the
                                 # 900 cleared cases scored 0.70 or higher -- the bank never
                                 # opened a "cleared" investigation on a quiet transaction --
                                 # so below 0.70 there is no negative class to measure
                                 # against and no weight can honestly be inferred either way.
    "dev_new":          -1.00,   # measured -1.53, damped (selection)
    "region_new_mflag":  0.75,   # measured
    "region_new_clean": -1.26,   # measured
    "amount_outlier":   -0.50,   # measured -0.97, damped
    "channel_odd":      -0.50,   # measured -0.99, damped
    "burst":             0.62,   # measured
    "card_testing":      0.60,   # measured +0.55; policy R5 drives the action anyway
    "device_ring":       0.00,   # measured -0.32: named as evidence, no weight
    "proxy":             0.00,   # measured -1.39 on n=48/37; conflicts with README, held at 0
    "proxy_new_device":  1.50,   # measured +2.73 vs cleared, but on only 9 undocumented
                                 # cases (4 of 9 hit it), so damped hard. Only 190 of the
                                 # 590,742 transactions carry proxy + New device + named
                                 # hardware at all, which is why it still counts for something.
    "analyst_request":   1.00,   # a trained analyst already saw cross-card activity
    "recurring":         0.00,   # measured +0.26: policy R7 drives the action, not the score
    # Case memory, measured by prep/calibrate.py against only the cases that had CLOSED
    # before each one opened -- an outcome is not known while a case is still being worked.
    "prior_fraud":       0.95,   # measured +0.95 (3,146 fraud / 234 cleared); was a guessed 0.30
    "prior_cleared":     0.00,   # measured +6.46, the WRONG sign for a guessed -0.30: a card
                                 # with an earlier cleared alarm was later confirmed as fraud 1,646
                                 # times and cleared 0. That is how cases were reopened, not a fact
                                 # about fraud, so it is cited as memory and scored at zero.
    "device_prior_fraud": 1.32,  # measured +1.32 (396 / 20); was a guessed 1.00. Cutting on
                                 # opened_at had read +1.67 off cases not yet closed.
    "customer_report":   1.00,   # an independent statement by the cardholder (R2)
    # A SIMULATED reply is not an observation. The dataset ships no customer or analyst
    # responses, so these are damped well below what a real reply would justify: assuming
    # an answer and then scoring it as if it had been received would manufacture
    # confidence the investigation has not earned.
    "customer_denied":   1.20,
    "customer_confirmed": -1.20,
    # a simulated step-up is derived from the match flags, which are already scored, so
    # it carries half a customer reply rather than counting that fact at full weight again
    "step_up_passed":    -0.60,
    "step_up_failed":     0.60,
    "episode":           0.00,   # cleared cases are single-transaction BY CONSTRUCTION;
                                 # scoring episode size would be scoring our own choice
}


P_CARD_TESTING = "card_testing"
P_CNP          = "card_not_present_fraud"
P_CNP_NEW_DEV  = "card_not_present_new_device"
P_OUT_REGION   = "out_of_region_use"
P_ATO          = "account_takeover"
P_UNDOC        = "undocumented"
P_NONE         = "none"


@dataclass
class Signal:
    """One scored piece of evidence. `claim` is what goes in the answer file."""
    name: str
    weight: float          # log-odds contribution, + = fraud
    claim: str
    entity_ids: list = field(default_factory=list)
    ref: str = ""
    source: str = "graph"


def score_signals(f, episode, device_ring, prior) -> list[Signal]:
    """f: feature row. episode: reconstructed txn episode. device_ring: shared-device
    expansion. prior: retrieved closed cases. Returns weighted signals.

    Every claim here is written to stand up in a case file: it says what was observed,
    and where the weight comes from, without pretending to know what an unnamed Vesta
    feature means."""
    s: list[Signal] = []

    # --- match flags. The one condition that survives calibration cleanly: an F on
    # M1..M9 means a stated card detail did not match.
    if f["m_false_n"] >= 2:
        s.append(Signal("m_flags", W["m_flags_2plus"],
            f"{int(f['m_false_n'])} of the card-detail match flags M1-M9 came back F on this "
            f"transaction. Measured across the bank's closed cases, two or more failed flags is "
            f"2.9x more likely on confirmed fraud than on a cleared alarm (22.7% vs 7.9%)",
            [str(f["txn_id"])], "query:card_window"))
    elif f["m_false_n"] == 1:
        s.append(Signal("m_flags", W["m_flags_1"],
            "One card-detail match flag (M1-M9) came back F on this transaction. A single "
            "failed flag is 3.5x more likely on confirmed fraud than on a cleared alarm",
            [str(f["txn_id"])], "query:card_window"))

    # --- model score, used only in the direction the evidence supports
    rs = f["risk_score"]
    if rs is not None and rs == rs and 0.70 <= rs < 0.85:
        s.append(Signal("risk_score_mid", W["risk_mid"],
            f"Model risk score {rs:.2f}. In the 0.70-0.85 band the bank's closed history holds "
            f"817 confirmed frauds against 234 cleared alarms, a likelihood ratio of 0.67 - "
            f"mildly exculpatory, consistent with the dataset note that above 0.70 most "
            f"flagged transactions turn out to be legitimate",
            [str(f["txn_id"])], "document:README#risk-score"))
    elif rs is not None and rs == rs and rs >= 0.85:
        s.append(Signal("risk_score_high", W["risk_high"],
            f"Model risk score {rs:.2f}. 74% of the alarms the bank cleared scored at or above "
            f"0.85 against 9% of confirmed fraud, so a score this high is evidence AGAINST "
            f"fraud rather than for it. The weight is damped from the measured value because "
            f"every cleared case in the history was selected from a high score to begin with",
            [str(f["txn_id"])], "document:README#risk-score"))

    # --- device new to the account
    if f["dev_new"]:
        s.append(Signal("device_new", W["dev_new"],
            "The identity record marks this device New for the account. 83% of cleared alarms "
            "had a New device against 18% of confirmed fraud: on its own a new device is the "
            "signature of a cardholder who has just bought a phone, not of a compromise",
            [str(f["txn_id"])], "document:README#pattern-3"))

    # --- region novelty, split by whether the card details matched
    if f["prior_in_region"] == 0 and f["addr1"] is not None and f["addr1"] == f["addr1"]:
        if f["m_false_n"] >= 1:
            s.append(Signal("region_new_bad", W["region_new_mflag"],
                f"The card has no billing history in region {f['addr1']} AND the card-detail "
                f"match flags failed on the same transaction. That combination is 2.1x more "
                f"likely on confirmed fraud than on a cleared alarm - it separates a cloned "
                f"card from a cardholder on a trip",
                [str(f["txn_id"])], f"query:region_history({f['addr1']})"))
        else:
            s.append(Signal("region_new_travel", W["region_new_clean"],
                f"The card has no billing history in region {f['addr1']}, but every card-detail "
                f"match flag passed. 716 of the 900 alarms the bank cleared were exactly this: "
                f"a cardholder travelling, confirmed on contact",
                [str(f["txn_id"])], f"query:region_history({f['addr1']})"))

    # --- amount and channel: both measured as mildly EXCULPATORY, against intuition
    if f["amt_vs_median"] and f["amt_vs_median"] >= 4 and f["amt_over_p95"]:
        s.append(Signal("amount_outlier", W["amount_outlier"],
            f"${f['amount']:,.2f} is {f['amt_vs_median']:.1f}x this card's median and above its "
            f"95th percentile. Counter-intuitively this is mildly exculpatory: large outliers "
            f"appear on 16% of cleared alarms and 6% of confirmed fraud, because confirmed "
            f"fraud tends to stay inside the cardholder's normal range to avoid attention",
            [str(f["txn_id"])], "query:card_baseline"))

    if f["channel_odd"]:
        s.append(Signal("channel_odd", W["channel_odd"],
            f"A {f['channel'].replace('_',' ')} transaction on a card whose history is "
            f"{f['online_share']*100:.0f}% online. Measured on the closed cases this is mildly "
            f"exculpatory (11.9% of cleared vs 4.5% of confirmed fraud)",
            [str(f["txn_id"])], "query:card_baseline"))

    if f["burst_48h"] >= 10:
        s.append(Signal("burst", W["burst"],
            f"{int(f['burst_48h'])} transactions on this card in the 48 hours ending at the "
            f"flagged one. A burst of ten or more is 1.9x more likely on confirmed fraud",
            episode["txn_ids"][:12], "query:card_window"))

    if f["small_auths_24h"] >= 3:
        s.append(Signal("card_testing", W["card_testing"],
            f"{int(f['small_auths_24h'])} online authorisations under $5 on this card in the 24h "
            f"before the flagged transaction, followed by a larger purchase - the testing "
            f"sequence policy R5 describes",
            episode["txn_ids"][:12], "query:card_testing_probe"))

    # --- shared origin: reported because R6 requires the shared element to be named,
    # but carries no weight (a >=3-card ring measured LR 0.73 against the closed cases)
    if device_ring.get("cards"):
        s.append(Signal("device_ring", W["device_ring"],
            f"Device profile '{f['device_profile']}' is shared with "
            f"{len(device_ring['cards'])} other card(s) inside the 30-day window, across "
            f"{len(set(device_ring.get('customers', [])))} customer(s). Named here because "
            f"policy R6 requires the shared element to be identified; it carries no weight in "
            f"the probability, because device sharing at this scale measured no better than "
            f"chance against the bank's closed cases",
            sorted(device_ring["cards"])[:12], "query:device_neighbors"))

    if f["proxy"] and f["dev_new"] and f.get("dev_named_hw"):
        s.append(Signal("proxy_new_device", W["proxy_new_device"],
            f"The connection came through an anonymising proxy ({f['id_23']}) from a device "
            f"marked New for the account on named hardware ({f['device_info']}). Only 190 of "
            f"the 590,742 transactions in the book carry that combination. It appears on 4 of "
            f"the 9 cases the bank's analysts confirmed as fraud but could not fit to any "
            f"documented pattern. The weight is damped hard because 9 cases is a thin base",
            [str(f["txn_id"])], "query:card_window"))
    elif f["proxy"]:
        s.append(Signal("proxy_noted", W["proxy"],
            f"The connection came through a proxy ({f['id_23']}). Recorded as context: proxy "
            f"use measured slightly more common on cleared alarms than on confirmed fraud in "
            f"this history, so it is not scored either way",
            [str(f["txn_id"])], "query:card_window"))

    gap = f.get("same_amount_gap_days")
    if (gap is not None and gap == gap and f.get("same_amount_months", 0) >= 3
            and f.get("same_amount_n", 0) >= 3 and 20 <= float(gap) <= 45):
        s.append(Signal("recurring", W["recurring"],
            f"This exact amount (${f['amount']:,.2f}) has been charged to this card "
            f"{int(f['same_amount_n'])} times across {int(f['same_amount_months'])} distinct "
            f"months under the same product code, a median of {float(gap):.0f} days apart. That "
            f"is the cadence of a subscription, and policy R7 forbids blocking on it",
            [str(f["txn_id"])], "query:card_window"))

    # --- case memory
    conf = [p for p in prior if p.get("outcome") == "confirmed_fraud"]
    if conf:
        s.append(Signal("prior_fraud", W["prior_fraud"],
            f"The bank has {len(conf)} confirmed-fraud case(s) already closed on this card "
            f"({', '.join(p['case_id'] for p in conf[:4])}), pattern(s): "
            f"{', '.join(sorted({p['pattern'] for p in conf}))}",
            [p["case_id"] for p in conf[:6]], "query:prior_cases_for_card"))
    cleared_prior = [p for p in prior if p.get("outcome") == "cleared"]
    if cleared_prior and not conf:
        s.append(Signal("prior_cleared", W["prior_cleared"],
            f"{len(cleared_prior)} previous alarm(s) on this card were investigated and cleared "
            f"({', '.join(p['case_id'] for p in cleared_prior[:4])})",
            [p["case_id"] for p in cleared_prior[:6]], "query:prior_cases_for_card"))
    return s


def classify(f, episode, device_ring) -> tuple[str, str]:
    """Assign the pattern using the empirical signatures. Returns (pattern, description)."""
    online = f["channel"] == "online"
    has_dev = bool(f["dev_specific"]) and f["device_profile"]

    if f["small_auths_24h"] >= 3:
        return P_CARD_TESTING, ""

    # undocumented: proxy or badly mixed channel on an otherwise clean-flagged online txn,
    # or a rare-device ring spanning customers. R9 -- do not force into a known category.
    ring_cards = device_ring.get("cards", [])
    ring_custs = [c for c in set(device_ring.get("customers", []))]
    # the ring test matches the device-link test used to build the ring in the first place:
    # named hardware earns a wider card threshold than a generic platform string
    ring_limit = 80 if f.get("dev_named_hw") else 20
    if len(ring_cards) >= 2 and len(ring_custs) >= 3 and f["dev_cards"] <= ring_limit:
        return P_UNDOC, (
            f"Several cards belonging to different customers transacted through one rare device "
            f"profile inside a short window: {len(ring_cards)} cards across {len(ring_custs)} "
            f"customers on '{f['device_profile']}', a profile seen on only {int(f['dev_cards'])} "
            f"cards bank-wide. This is coordinated use of a single machine across unrelated "
            f"accounts rather than the compromise of one cardholder, so it fits none of the five "
            f"documented patterns. It was found by expanding from the flagged transaction to its "
            f"device profile and back out to every other card that touched it.")
    if f["proxy"] and online:
        return P_UNDOC, (
            f"The transaction was placed through an anonymising proxy ({f['id_23']}) on a device "
            f"marked New for the account, while the card's own match flags passed. Proxy use "
            f"appears in 44% of the cases the bank's analysts confirmed as fraud but could not "
            f"fit to a documented pattern, against 1% of all confirmed fraud. The concealment "
            f"itself, rather than the amount or the merchant, is what marks this activity.")

    if f["m_false_n"] >= 1 and not has_dev:
        # no device record + failed match flags: out-of-region vs takeover
        if f["prior_in_region"] == 0:
            return P_OUT_REGION, ""
        return P_ATO, ""
    if f["channel_odd"] or (f["m_false_n"] >= 1 and f["dev_cards"] <= 20):
        return P_ATO, ""
    if online and f["dev_new"]:
        return P_CNP_NEW_DEV, ""
    if online:
        return P_CNP, ""
    if f["prior_in_region"] == 0:
        return P_OUT_REGION, ""
    return P_CNP, ""
