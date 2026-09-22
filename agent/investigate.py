"""The investigation loop.

Trigger -> gather -> assess -> request evidence -> re-assess -> act -> explain -> remember.
Each numbered step is a real graph call; `steps` is what `asked_after_step` refers to.
"""
from __future__ import annotations
import datetime as dt, math, time

import episode as ep
import external as X
import patterns as P
import policy as pol
import retrieve as R


def is_recurring_charge(f) -> bool:
    """Policy R7: "the same merchant, same amount, monthly".

    No merchant field ships with the data, so product code stands in for it. The test
    that matters is cadence: on a card holding ten thousand transactions any round
    amount repeats dozens of times by chance, so a raw count says nothing. A genuine
    subscription shows up as the same amount under the same product code, in three or
    more distinct months, spaced roughly a month apart.
    """
    gap = f.get("same_amount_gap_days")
    if gap is None or gap != gap:
        return False
    return (f.get("same_amount_months", 0) >= 3
            and f.get("same_amount_n", 0) >= 3
            and 20 <= float(gap) <= 45)


def device_link_is_meaningful(f) -> bool:
    """Is this device profile actually a link, or just a common configuration?

    'Windows | Windows 10 | edge 16.0 | 1366x768' is shared by 208 cards because that is
    what a Windows laptop looks like. 'SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0
    | 1920x1080' shared by 52 cards is a machine being reused. Named hardware earns a
    wider threshold; a profile that is mostly 'unknown' earns a narrower one.
    """
    dp = f.get("device_profile")
    if not dp or not f.get("dev_specific"):
        return False
    cards = int(f.get("dev_cards") or 0)
    unknowns = int(f.get("dev_unknowns") or 0)
    if unknowns >= 3:
        return cards <= 10
    if f.get("dev_named_hw"):
        return cards <= 80
    return cards <= 20


class Investigation:
    def __init__(self, backend, trigger, llm=None, now=None,
                 suppress=None, analyst_signals=None, ring_cap=None):
        """suppress / analyst_signals are how a human steers the investigation.

        An analyst who says "ignore the out-of-region flag, the customer is on holiday"
        is not overruling the arithmetic, they are withdrawing a premise from it. So the
        challenge removes named signals and may add an analyst-sourced one, and the same
        deterministic scorer runs again over what is left. The LLM's only job in that
        loop is mapping free text onto a signal name -- a classification, not a
        judgement -- because a probability the model wrote is a probability nobody can
        audit.

        ring_cap widens the device-sharing expansion when an analyst asks for a deeper
        look; it is passed straight to the graph algorithm.
        """
        self.b = backend
        self.t = trigger                      # row from case_pack
        self.llm = llm
        self.now = now or trigger["opened_at"]
        self.steps: list[str] = []
        self.signals: list[P.Signal] = []
        self.suppress = set(suppress or ())
        self.analyst_signals = list(analyst_signals or ())
        self.ring_cap = ring_cap
        self._docs_seen: set[str] = set()
        self.t0 = time.time()

    def step(self, label):
        self.steps.append(label)
        return len(self.steps)

    # -----------------------------------------------------------------------
    def run(self) -> dict:
        t = self.t
        card_id, txn_id = t["card_id"], int(t["flagged_txn_id"])

        # 1. the flagged transaction and its features
        self.step("pull flagged transaction and derive signals")
        f = self.b.features(card_id, txn_id)
        flagged = {"txn_id": txn_id, "ts": f["ts"], "amount": f["amount"],
                   "device_profile": f["device_profile"], "addr1": f["addr1"]}

        # 2. what normal looks like for this card
        self.step("read the card's own baseline")
        self.b.card_baseline(card_id)

        # 3. shared-origin expansion through the device profile
        self.step("expand through the device profile to other cards")
        lo = self.now - dt.timedelta(days=30)
        ring = {"cards": [], "customers": [], "n_txns": 0}
        if device_link_is_meaningful(f):
            ring = self.b.device_neighbors(f["device_profile"], lo, self.now)
            ring = {"cards": [c for c in ring["cards"] if c != card_id],
                    "customers": ring["customers"], "n_txns": ring["n_txns"]}

        # 4. region history
        self.step("check the card's history in this billing region")
        if f["addr1"] is not None and f["addr1"] == f["addr1"]:
            self.b.region_history(card_id, f["addr1"], f["ts"])

        # 5. card-testing probe
        self.step("probe for a card-testing sequence")
        self.b.card_testing_probe(card_id, f["ts"], 24)

        # 6. case memory: prior investigations on this card, and on this device
        self.step("retrieve prior closed cases for this card and device")
        prior_df = self.b.prior_cases_for_card(card_id, before=self.now)
        prior = prior_df.to_dict("records") if len(prior_df) else []
        dev_prior = []
        if f["device_profile"] and f["dev_specific"] and f["dev_cards"] <= 50:
            d = self.b.prior_cases_for_device(f["device_profile"], before=self.now)
            dev_prior = d.to_dict("records") if len(d) else []

        # 6b. the one source outside the bank: what kind of email domain this is.
        self.step("look the purchaser email domain up with external intelligence")
        intel = self.b.email_intel(f.get("p_email"))

        # ---- assess -------------------------------------------------------
        pattern, pattern_desc = P.classify(f, {"txn_ids": [str(txn_id)]}, ring)
        self.step("reconstruct the episode and size the exposure")
        episode = ep.build(self.b, card_id, flagged, pattern, f)
        pattern, pattern_desc = P.classify(f, episode, ring)

        self.signals = P.score_signals(f, episode, ring, prior)
        if intel["class"] != "unknown":
            self.signals.append(P.Signal(
                "email_domain_intel", intel["weight"], X.claim(intel),
                [str(intel["domain"])], "external:email_domain_intel", source="external"))
        # --- human steering, applied before anything is scored off the signal set ---
        if self.suppress:
            kept, dropped = [], []
            for x in self.signals:
                (dropped if x.name in self.suppress else kept).append(x)
            self.signals = kept
            for x in dropped:
                self.signals.append(P.Signal(
                    f"withdrawn:{x.name}", 0.0,
                    f"WITHDRAWN BY ANALYST. {x.claim}", x.entity_ids, x.ref,
                    source="external"))
        self.signals.extend(self.analyst_signals)
        if dev_prior:
            conf = [d for d in dev_prior if d.get("outcome") == "confirmed_fraud"]
            if conf:
                self.signals.append(P.Signal("device_prior_fraud", P.W["device_prior_fraud"],
                    f"This exact device profile already appears in {len(conf)} confirmed-fraud "
                    f"case(s) the bank closed ({', '.join(c['case_id'] for c in conf[:4])})",
                    [c["case_id"] for c in conf[:6]], "query:prior_cases_for_device"))

        prob = pol.probability(self.signals)
        trigger_type = t["trigger_type"]
        customer_disputed = trigger_type == "customer_report"

        if trigger_type == "analyst_request":
            self.signals.append(P.Signal("analyst_request", P.W["analyst_request"],
                "A fraud analyst opened this review after seeing several cards transacting "
                "through the same unusual device profile. A trained reviewer identifying a "
                "cross-card pattern is independent evidence, not a restatement of the model score",
                [str(txn_id)], "trigger:analyst_request", source="external"))
            prob = pol.probability(self.signals)

        # a customer report is itself evidence: the cardholder has already denied it.
        if customer_disputed:
            self.signals.append(P.Signal("customer_report", P.W["customer_report"],
                "The cardholder contacted the bank stating they did not make this purchase",
                [str(txn_id)], "trigger:customer_report", source="customer"))
            prob = pol.probability(self.signals)

        # Policy R7 is "same merchant, same amount, monthly". On these cards a raw
        # repeat count means nothing -- some hold 10,000 transactions, so any round
        # amount repeats. Cadence across distinct months under the same product code
        # is the test that actually separates a subscription from a coincidence.
        recurring = is_recurring_charge(f)
        n_ind = pol.independent_evidence_count(self.signals)
        verdict = self._verdict(prob)

        # exposure only means something if we think something went wrong
        exposure = episode["exposure"] if verdict != "legitimate" else 0.0
        connected = sorted(ring["cards"])
        shared_element = f"device profile '{f['device_profile']}'" if connected else ""

        # 7. graph algorithm: the transitive device-sharing component. One hop says who
        # else touched this handset; the component says how far the sharing reaches
        # before it stops. Reported because R6 asks for the shared element to be named;
        # given no weight, because it percolates (see prep/rings.py).
        self.step("run connected components over the device-sharing graph")
        comp = (self.b.ring_component(card_id, self.ring_cap) if self.ring_cap
                else self.b.ring_component(card_id))
        if comp["ring_size"] >= 2:
            if comp["ring_size"] <= 10:
                claim = (f"Connected components over the device-sharing graph place this card "
                         f"in a bounded component of {comp['ring_size']} cards "
                         f"({', '.join(comp['members'][:6])}), linked transitively through "
                         f"device profiles each used by no more than 8 cards. A component this "
                         f"small is a genuine shared origin rather than a common configuration.")
            else:
                claim = (f"Connected components place this card in the graph's giant "
                         f"device-sharing component ({comp['ring_size']:,} cards). Transitive "
                         f"device sharing percolates at every threshold tested, so membership "
                         f"is not evidence of a ring and carries no weight here; it is recorded "
                         f"because policy R6 asks for the shared element to be named.")
            self.signals.append(P.Signal("ring_component", 0.0, claim,
                                         comp["members"][:8], "query:ring_component"))
        # A bounded component is a shared origin even where no single device links the
        # cards directly, so its members join the set R6 asks to be monitored. The giant
        # component never does: monitoring 2,500 cards is not an action, it is a denial
        # of service on the fraud desk.
        if 2 <= comp["ring_size"] <= 10:
            connected = sorted(set(connected) | set(comp["members"]))
            shared_element = (shared_element or
                              f"device-sharing component of {comp['ring_size']} cards")
        connected = connected[:25]

        # 8. GraphRAG, document half. The graph says what happened; the typology and the
        # policy rule say what it is called and what may be done about it. Retrieved
        # BEFORE the action decision, not after it, so the rule is grounding rather than
        # post-hoc justification.
        self.step("retrieve the governing typology and policy rule")
        self._retrieve_docs(
            pattern=pattern, verdict=verdict, connected=bool(connected),
            customer_denied=customer_disputed and not recurring, customer_confirmed=False,
            recurring=recurring, no_reply=False,
            uncertain_and_exposed=verdict == "uncertain" and exposure > 500, filing=False)

        # ---- initial recommendation (before any requested evidence) --------
        initial = pol.decide_actions(
            prob=prob, verdict=verdict, exposure=exposure, signals=self.signals,
            pattern=pattern if verdict != "legitimate" else "none", trigger_type=trigger_type,
            customer_denied=customer_disputed and not recurring, customer_confirmed=False,
            recurring=recurring, connected_cards=connected, shared_element=shared_element,
            n_confirmed_cards=0, phase="initial")
        file0, why0 = pol.should_file_report(verdict, prob, exposure, bool(connected),
                                             pattern == P.P_UNDOC and bool(connected))
        initial = pol.apply_sar(initial, file0, exposure, why0)

        # ---- gather more evidence if the policy calls for it ---------------
        requests, answered = self._request_evidence(initial, prob, recurring, f, verdict)
        if requests:
            prob, verdict, exposure = self._reassess(prob, requests, episode, verdict)

        n_ind = pol.independent_evidence_count(self.signals)
        customer_confirmed = any(r["_confirmed"] for r in requests)
        no_reply = any(r.get("_no_reply") for r in requests)
        # the cardholder's report is a denial, but a later confirmation supersedes it:
        # the two must never both be live or the action set contradicts itself.
        customer_denied = (any(r["_denied"] for r in requests)
                           or (customer_disputed and not recurring)) and not customer_confirmed
        # A confirmation from the cardholder settles it: the case closes as legitimate and
        # nothing may still be counted as exposure.
        # A simulated confirmation closes the case only if the probability actually lands
        # in the legitimate band. Flipping the verdict while the score still says 0.45
        # would assert more than the evidence carries.
        if customer_confirmed and verdict != "fraud" and prob <= 0.30:
            verdict = "legitimate"
        # A denial the graph evidence contradicts is a conflict, not an acquittal. Closing
        # it as legitimate would tell a cardholder who reported fraud that they are wrong;
        # policy R8 sends conflicting evidence to a human instead.
        if customer_denied and verdict == "legitimate":
            verdict = "uncertain"
        # exposure is settled only after the verdict is final, so a case flipped to
        # uncertain still carries the amount at risk rather than zero.
        if verdict == "legitimate":
            exposure = 0.0
            episode = {**episode, "txn_ids": [], "first_txn_id": ""}
        else:
            exposure = episode["exposure"]

        final = pol.decide_actions(
            prob=prob, verdict=verdict, exposure=exposure, signals=self.signals,
            pattern=pattern if verdict != "legitimate" else "none", trigger_type=trigger_type,
            customer_denied=customer_denied, customer_confirmed=customer_confirmed,
            recurring=recurring, connected_cards=connected, shared_element=shared_element,
            n_confirmed_cards=0, phase="final", no_reply=no_reply)
        file1, why1 = pol.should_file_report(verdict, prob, exposure, bool(connected),
                                             pattern == P.P_UNDOC and bool(connected))
        final = pol.apply_sar(final, file1, exposure, why1)

        # the filing decision is now settled, so the guidance the narrative must satisfy
        # can be retrieved before answer.py writes a word of it.
        if file1:
            self.step("retrieve FinCEN narrative guidance for the filing")
            self._retrieve_docs(pattern=pattern, verdict=verdict, connected=bool(connected),
                                customer_denied=customer_denied,
                                customer_confirmed=customer_confirmed, recurring=recurring,
                                no_reply=no_reply, uncertain_and_exposed=False, filing=True)

        return {
            "f": f, "flagged": flagged, "episode": episode, "pattern": pattern,
            "pattern_desc": pattern_desc, "signals": self.signals, "prob": prob,
            "verdict": verdict, "exposure": exposure, "connected": connected,
            "devices": [f["device_profile"]] if f["device_profile"] and f["dev_specific"] else [],
            "prior": prior, "dev_prior": dev_prior, "ring": ring,
            "initial": initial, "final": final, "requests": requests,
            "sar_file": file1, "sar_reason": why1, "n_ind": n_ind, "steps": self.steps,
            "recurring": recurring, "answered": bool(requests),
            "latency_s": round(time.time() - self.t0, 2),
        }


    def _retrieve_docs(self, **situation):
        """Run the case's retrieval plan and fold the passages in as `document` evidence.

        Weight zero throughout: a policy rule is not evidence that this cardholder
        committed fraud, it is the rule the recommendation has to satisfy. It belongs in
        the evidence list because the answer spec asks for document-sourced claims and
        because the LLM is grounded on the retrieved passage rather than on a rule
        paraphrased into a prompt -- but it must never move the probability.
        """
        # the documented typology is a lookup, not a search (see retrieve.PATTERN_CHUNK)
        for ev in R.to_evidence(R.typology(situation["pattern"]), self._docs_seen):
            self.signals.append(P.Signal(f"doc:{ev['doc_id']}", 0.0, ev["claim"], [],
                                         ev["ref"], source="document"))
        for query, sources, k in R.queries_for(**situation):
            try:
                hits = self.b.doc_search(query, k=k, sources=sources)
            except FileNotFoundError as e:
                print(f"  [rag] skipped: {e}")
                return
            for ev in R.to_evidence(hits, self._docs_seen):
                self.signals.append(P.Signal(f"doc:{ev['doc_id']}", 0.0, ev["claim"],
                                             [], ev["ref"], source="document"))

    # -----------------------------------------------------------------------
    @staticmethod
    def _verdict(prob):
        if prob >= 0.70:
            return "fraud"
        if prob <= 0.30:
            return "legitimate"
        return "uncertain"

    def _request_evidence(self, initial, prob, recurring, f, verdict):
        """Policy 5: the agent may ask the customer, request step-up auth, or ask an
        analyst without approval. Replies are not provided in this round, so the
        response is simulated from the evidence already in hand and the assumption is
        stated in full."""
        wanted = {a["action"] for a in initial}
        reqs = []
        step_no = len(self.steps)

        # A customer report IS the cardholder's denial. Asking them the same question
        # again and counting the same answer twice would inflate the probability off one
        # fact. The exception is a recurring charge: "is this subscription yours?" is a
        # genuinely different question from "did you make this purchase?".
        already_denied = self.t["trigger_type"] == "customer_report" and not recurring
        if pol.VERIFY_WITH_CUSTOMER in wanted and not already_denied:
            # The assumed reply must not be a function of our own probability, or it only
            # amplifies the prior and adds no information. It is derived instead from a
            # feature the cardholder's answer would actually turn on, and where nothing
            # independent points either way the honest assumption is no reply at all --
            # which policy R4 already covers.
            strong = sum(1 for x in self.signals if x.weight >= 0.7)
            if recurring:
                resp = ("Cardholder, shown the charge history, recognises the amount as a "
                        "recurring charge they had forgotten and confirms it is theirs. "
                        "ASSUMPTION: no replies ship with this dataset. This one is drawn "
                        f"from an independent feature of the data - {int(f['same_amount_n'])} "
                        f"charges of this exact amount across "
                        f"{int(f['same_amount_months'])} months, a median "
                        f"{float(f['same_amount_gap_days']):.0f} days apart - not from the "
                        "agent's own probability.")
                conf, den, reply = True, False, True
            elif strong >= 2:
                resp = ("Cardholder states they did not make the transaction and still holds "
                        "the card. ASSUMPTION: no replies ship with this dataset. This one is "
                        f"drawn from {strong} independent incriminating findings in the graph, "
                        "not from the agent's own probability.")
                conf, den, reply = False, True, True
            else:
                resp = ("No reply within 24 hours. ASSUMPTION: no replies ship with this "
                        "dataset, and nothing independent in the evidence points to what the "
                        "cardholder would say. Inventing an answer here would manufacture "
                        "certainty, so the agent assumes the case policy R4 is written for - "
                        "the customer does not respond - and acts accordingly.")
                conf, den, reply = False, False, False
            reqs.append({"type": "customer_validation", "asked_after_step": step_no,
                         "assumed_response": resp, "_confirmed": conf, "_denied": den,
                         "_no_reply": not reply})

        if pol.STEP_UP_AUTH in wanted and not reqs:
            passed = prob < 0.55
            reqs.append({"type": "step_up_auth", "asked_after_step": step_no, "_no_reply": False,
                         "assumed_response": (
                             "One-time passcode completed successfully from the cardholder's "
                             "registered number. ASSUMPTION: simulated; no authentication "
                             "responses ship with the dataset."
                             if passed else
                             "Step-up authentication was not completed; the challenge expired "
                             "unanswered. ASSUMPTION: simulated; no authentication responses "
                             "ship with the dataset."),
                         "_confirmed": passed, "_denied": not passed})

        if pol.ESCALATE_TO_ANALYST in wanted:
            reqs.append({"type": "analyst_info", "asked_after_step": step_no, "_no_reply": False,
                         "assumed_response": (
                             "Analyst confirms no merchant-side chargeback or law-enforcement "
                             "notice is attached to these transactions, so the graph evidence "
                             "stands as gathered. ASSUMPTION: simulated; no analyst replies "
                             "ship with the dataset."),
                         "_confirmed": False, "_denied": False})
        return reqs, bool(reqs)

    def _reassess(self, prob, requests, episode, verdict):
        """Fold the simulated responses back in as evidence and re-score."""
        for r in requests:
            if r["_denied"]:
                self.signals.append(P.Signal("customer_denied", P.W["customer_denied"],
                    "Cardholder denies the transaction on contact", [], f"evidence_request:{r['type']}",
                    source="customer"))
            elif r["_confirmed"]:
                self.signals.append(P.Signal("customer_confirmed", P.W["customer_confirmed"],
                    "Cardholder confirms the transaction was theirs", [], f"evidence_request:{r['type']}",
                    source="customer"))
        prob = pol.probability(self.signals)
        verdict = self._verdict(prob)
        exposure = episode["exposure"] if verdict != "legitimate" else 0.0
        return prob, verdict, exposure
