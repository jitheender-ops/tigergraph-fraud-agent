#!/usr/bin/env python
"""Held-out backtest: is the agent better than a threshold?

The weights in agent/patterns.py were *fitted* on all 5,565 closed cases and never
*evaluated*, which is the same criticism this project makes of the bank's own model. So:
recalibrate on July-September only, score October, and compare against the policies
anybody would try first.

    train   Jul-Sep   3,437 confirmed  756 cleared
    test    Oct       1,203 confirmed  144 cleared

Two numbers matter and neither is accuracy. 89% of the test set is fraud, so "call
everything fraud" scores 89% and is useless -- the same trap eval harnesses for payment
retries hit, where a free retry makes brute force optimal by construction. What separates
the policies is what they cost: a missed fraud costs its exposure, a false block costs
goodwill nobody can price. So the table reports both error counts and the break-even --
how expensive a false block would have to be before a policy stops winning.

  uv run python eval/backtest.py
  uv run python eval/backtest.py --insample   # also score with the shipped weights
"""
from __future__ import annotations
import argparse, math, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

import duckdb
import pandas as pd

import external as X
import policy as pol
import patterns as P
from features import FEATURE_SQL

SPLIT = "2016-10-01"

# The conditions calibrate.py measures, and the weight each one becomes. Damping is the
# project's own, reproduced here rather than re-decided: every cleared case in the
# history was selected from a high model score, so anything correlated with *how alerts
# were chosen* is confounded and is pulled toward zero rather than trusted.
#   measured -> used
DAMP = {
    "m_flags_2plus":     lambda w: w,
    "m_flags_1":         lambda w: min(w, 1.00),   # non-monotonic on small n; flattened
    "risk_high_085":     lambda w: max(w, -0.80),  # measured -2.12, selection-inflated
    "dev_new":           lambda w: max(w, -1.00),  # measured -1.53, same reason
    "region_new_mflag":  lambda w: w,
    "region_new_clean":  lambda w: w,
    "amount_outlier_4x": lambda w: w / 2,
    "channel_odd":       lambda w: w / 2,
    "burst_10plus":      lambda w: w,
    "small_auths_3plus": lambda w: w,
}
# measured +6.38 on 1,526 fraud and ZERO cleared -- an artefact of which alerts were ever
# opened, not a fact about fraud. Dropped in the shipped model, dropped here.
DROPPED = {"risk_low_030"}

CONDS = {
    "m_flags_2plus":     lambda d: d.m_false_n >= 2,
    "m_flags_1":         lambda d: d.m_false_n == 1,
    "risk_high_085":     lambda d: d.risk_score >= 0.85,
    "risk_low_030":      lambda d: d.risk_score <= 0.30,
    "dev_new":           lambda d: d.dev_new == 1,
    "region_new_mflag":  lambda d: (d.prior_in_region == 0) & (d.m_false_n >= 1),
    "region_new_clean":  lambda d: (d.prior_in_region == 0) & (d.m_false_n == 0),
    "amount_outlier_4x": lambda d: (d.amt_vs_median >= 4) & (d.amt_over_p95 == 1),
    "channel_odd":       lambda d: d.channel_odd == 1,
    "burst_10plus":      lambda d: d.burst_48h >= 10,
    "small_auths_3plus": lambda d: d.small_auths_24h >= 3,
}

# Which W key each measured condition feeds.
TO_W = {
    "m_flags_2plus": "m_flags_2plus", "m_flags_1": "m_flags_1",
    "risk_high_085": "risk_high", "dev_new": "dev_new",
    "region_new_mflag": "region_new_mflag", "region_new_clean": "region_new_clean",
    "amount_outlier_4x": "amount_outlier", "channel_odd": "channel_odd",
    "burst_10plus": "burst", "small_auths_3plus": "card_testing",
}


def measure(train: pd.DataFrame) -> dict[str, float]:
    """Log-likelihood ratios on the training half, Laplace-smoothed."""
    fraud = train[train.outcome == "confirmed_fraud"]
    clear = train[train.outcome != "confirmed_fraud"]
    out = {}
    for name, fn in CONDS.items():
        if name in DROPPED:
            continue
        pf = (int(fn(fraud).sum()) + 0.5) / (len(fraud) + 1)
        pc = (int(fn(clear).sum()) + 0.5) / (len(clear) + 1)
        out[TO_W[name]] = round(DAMP[name](math.log(pf / pc)), 2)
    return out


def measure_memory(train: pd.DataFrame, by_case: dict) -> dict[str, float]:
    """prior_fraud / prior_cleared on the training half: an earlier closed case on the
    same card, as the agent retrieves it. prior_cleared stays pinned at zero whatever it
    measures, for the selection reason patterns.W gives; it is printed so it can be seen."""
    has = lambda outcome: train.key_id.map(
        lambda k: any(p["outcome"] == outcome for p in by_case.get(k, [])))
    fraud = train.outcome == "confirmed_fraud"
    out = {}
    for name, outcome in (("prior_fraud", "confirmed_fraud"), ("prior_cleared", "cleared")):
        hit = has(outcome)
        pf = (int((hit & fraud).sum()) + 0.5) / (int(fraud.sum()) + 1)
        pc = (int((hit & ~fraud).sum()) + 0.5) / (int((~fraud).sum()) + 1)
        out[name] = round(math.log(pf / pc), 2)
    out["_prior_cleared_measured"], out["prior_cleared"] = out["prior_cleared"], 0.0
    return out


def refit_email(train: pd.DataFrame) -> dict[str, float]:
    """The external source's weights, refit on the training half too.

    external.CLASS_WEIGHT was measured on all 5,565 closed cases -- October included --
    so scoring held-out October with it would be leakage, and the backtest would be
    marking its own homework. privacy_or_niche keeps the same halving it gets in the
    shipped module, for the same reason: the base is thin.
    """
    cls = train.p_email.map(lambda d: X._intel().get(str(d).strip().lower(), "unknown")
                            if d else "unknown")
    fraud, clear = train.outcome == "confirmed_fraud", train.outcome != "confirmed_fraud"
    F, C = int(fraud.sum()), int(clear.sum())
    out = {}
    for c in ("isp_tied", "masked", "free_webmail", "privacy_or_niche"):
        pf = (int((fraud & (cls == c)).sum()) + 0.5) / (F + 1)
        pc = (int((clear & (cls == c)).sum()) + 0.5) / (C + 1)
        w = math.log(pf / pc)
        out[c] = round(w / 2 if c == "privacy_or_niche" else
                       0.0 if c == "free_webmail" else w, 2)
    out["unknown"] = 0.0
    return out


def score(row, prior, email_w) -> float:
    """The shipped scorer, on one case, including the external lookup -- otherwise the
    backtest evaluates a different agent from the one that ships. No episode and no live
    device ring: a closed case has neither, and inventing them would be the same mistake
    in the other direction."""
    return score_full(row, prior, email_w)[0]


def score_full(row, prior, email_w):
    """score(), plus the signals -- R8's conflict test needs them."""
    sig = P.score_signals(row, {"txn_ids": [str(row["txn_id"])]},
                          {"cards": []}, prior)
    total = sum(s.weight for s in sig)
    dom = row.get("p_email")
    cls = X._intel().get(str(dom).strip().lower(), "unknown") if dom else "unknown"
    total += email_w.get(cls, 0.0)
    return 1.0 / (1.0 + math.exp(-total)), sig


def verdict(p, lo=0.30, hi=0.70):
    return "fraud" if p >= hi else "legitimate" if p <= lo else "uncertain"


BANDS = [(lo / 100, hi / 100) for lo in range(15, 50, 5) for hi in range(55, 90, 5)]


def fit_band(probs, truth, exposure, review_cost, block_cost):
    """The verdict band that minimises what the mistakes cost on the TRAINING half:
    missed loss + false blocks x block_cost + cases sent for review x review_cost.
    Both costs are inputs, not facts -- nobody in this dataset prices them -- so they
    are printed beside the answer rather than buried in it."""
    best = None
    for lo, hi in BANDS:
        c = confusion([verdict(p, lo, hi) for p in probs], truth, exposure)
        cost = c["missed"] + c["fp"] * block_cost + (c["unc_f"] + c["unc_c"]) * review_cost
        if best is None or cost < best[0]:
            best = (cost, lo, hi)
    return best


def confusion(pred, truth, exposure):
    """Uncertain is not a wrong answer -- policy R8 routes it to a human -- so it is
    counted separately rather than folded into either error."""
    tp = fp = fn = tn = unc_f = unc_c = 0
    missed = 0.0
    for p, t, x in zip(pred, truth, exposure):
        fraud = t == "confirmed_fraud"
        if p == "uncertain":
            unc_f += fraud
            unc_c += not fraud
        elif p == "fraud":
            tp += fraud
            fp += not fraud
        else:
            fn += fraud
            tn += not fraud
            missed += x if fraud else 0.0
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, unc_f=unc_f, unc_c=unc_c, missed=missed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--insample", action="store_true",
                    help="also score with the shipped weights, to size the overfit")
    ap.add_argument("--review-cost", type=float, default=25.0,
                    help="assumed cost of one case reviewed (analyst or customer contact)")
    ap.add_argument("--false-block-cost", type=float, default=300.0,
                    help="assumed cost of blocking a legitimate cardholder")
    args = ap.parse_args()

    con = duckdb.connect("build/fraud.db", read_only=True)
    con.execute("""CREATE OR REPLACE TEMP TABLE anchors AS
      SELECT case_id AS key_id,
             coalesce(first_fraud_txn_id::VARCHAR, split_part(txn_ids,'|',1))::BIGINT AS txn_id
      FROM closed_case""")
    con.execute(f"CREATE OR REPLACE TEMP TABLE feat AS {FEATURE_SQL}")
    df = con.sql("""SELECT f.*, cc.outcome, cc.opened_at, cc.exposure_usd
                    FROM feat f JOIN closed_case cc ON cc.case_id = f.key_id""").df()
    df["opened_at"] = pd.to_datetime(df.opened_at)

    train = df[df.opened_at < SPLIT]
    test = df[df.opened_at >= SPLIT].reset_index(drop=True)
    print(f"train {len(train):>6,}  ({(train.outcome=='confirmed_fraud').sum():,} fraud, "
          f"{(train.outcome!='confirmed_fraud').sum():,} cleared)   before {SPLIT}")
    print(f"test  {len(test):>6,}  ({(test.outcome=='confirmed_fraud').sum():,} fraud, "
          f"{(test.outcome!='confirmed_fraud').sum():,} cleared)   from   {SPLIT}\n")

    # prior closed cases on the same card, as the agent would have retrieved them
    priors = con.sql("""
        SELECT a.case_id, b.case_id AS prior_id, b.outcome, b.pattern
        FROM closed_case a JOIN closed_case b
          ON b.card_id = a.card_id AND b.closed_at < a.opened_at
    """).df()
    by_case: dict[str, list] = {}
    for r in priors.itertuples():
        by_case.setdefault(r.case_id, []).append(
            {"case_id": r.prior_id, "outcome": r.outcome, "pattern": r.pattern})

    fitted = measure(train)
    memory = measure_memory(train, by_case)
    print(f"memory on the training half: prior_fraud {memory['prior_fraud']:+.2f} "
          f"[{P.W['prior_fraud']:+.2f}], prior_cleared measured "
          f"{memory.pop('_prior_cleared_measured'):+.2f}, used 0.00 (selection)")
    fitted.update(memory)
    email_w = refit_email(train)
    print("weights refitted on the training half (shipped value in brackets):")
    for k, v in fitted.items():
        shipped = P.W[k]
        flag = "" if abs(v - shipped) < 0.15 else "   <- moved"
        print(f"  {k:20} {v:>+6.2f}   [{shipped:>+5.2f}]{flag}")
    for k, v in email_w.items():
        if k == "unknown":
            continue
        shipped = X.CLASS_WEIGHT[k]
        flag = "" if abs(v - shipped) < 0.15 else "   <- moved"
        print(f"  email:{k:14} {v:>+6.2f}   [{shipped:>+5.2f}]{flag}")

    rows = test.to_dict("records")
    truth = [r["outcome"] for r in rows]
    exposure = [float(r["exposure_usd"] or 0) for r in rows]

    def run_agent(weights, label, email_w=email_w):
        original = dict(P.W)
        P.W.update(weights)
        try:
            preds = [verdict(score(r, by_case.get(r["key_id"], []), email_w)) for r in rows]
        finally:
            P.W.clear(); P.W.update(original)
        return label, preds

    policies = [
        ("always fraud", ["fraud"] * len(rows)),
        ("never fraud", ["legitimate"] * len(rows)),
        ("risk score >= 0.70",
         ["fraud" if (r["risk_score"] or 0) >= 0.70 else "legitimate" for r in rows]),
        ("risk score >= 0.85",
         ["fraud" if (r["risk_score"] or 0) >= 0.85 else "legitimate" for r in rows]),
        run_agent(fitted, "agent, weights from train only"),
    ]
    if args.insample:
        policies.append(run_agent({}, "agent, shipped weights (in-sample)",
                                  email_w=X.CLASS_WEIGHT))

    hdr = (f"{'policy':34}{'caught':>8}{'missed':>8}{'false':>7}{'uncertain':>11}"
           f"{'$ missed':>13}")
    print(f"\n{hdr}\n{'-' * len(hdr)}")
    results = []
    for label, preds in policies:
        c = confusion(preds, truth, exposure)
        results.append((label, c))
        print(f"{label:34}{c['tp']:>8,}{c['fn']:>8,}{c['fp']:>7,}"
              f"{c['unc_f'] + c['unc_c']:>11,}{c['missed']:>13,.0f}")

    # --- what a false block would have to cost ---------------------------------
    # Direction matters and is easy to get backwards. A policy that blocks MORE than the
    # agent trades false blocks for recovered losses, so it wins while a false block is
    # CHEAP. A policy that blocks less trades the other way and wins only once a false
    # block is EXPENSIVE. Printing one sentence for both cases invites the wrong reading.
    agent = next(c for l, c in results if l.startswith("agent, weights from train"))
    print(f"\nagent, out of sample: {agent['tp']:,} caught, {agent['fn']:,} missed, "
          f"{agent['fp']:,} fraud verdicts on legitimate cases, ${agent['missed']:,.0f} of loss "
          f"let through,\n"
          f"and {agent['unc_f'] + agent['unc_c']:,} of {len(rows):,} "
          f"({(agent['unc_f'] + agent['unc_c']) / len(rows):.0%}) judged too close to call "
          f"(split below into analyst and automatic verification).\n")
    print("break-even against it (every agent fraud verdict counted as a block, which "
          "overstates it --\nsee the action table below):\n")
    for label, c in results:
        if label.startswith("agent, weights from train"):
            continue
        d_missed = c["missed"] - agent["missed"]     # extra loss the policy lets through
        d_false = c["fp"] - agent["fp"]              # extra false blocks it makes
        if d_false == 0 and abs(d_missed) < 1:
            print(f"  {label:34} identical")
        elif d_false > 0 and d_missed < 0:
            print(f"  {label:34} wins while a false block costs under "
                  f"${-d_missed / d_false:>9,.0f}")
        elif d_false < 0 and d_missed > 0:
            print(f"  {label:34} wins once a false block costs over  "
                  f"${d_missed / -d_false:>9,.0f}")
        elif d_missed >= 0 and d_false >= 0:
            print(f"  {label:34} loses on both axes -- more false blocks AND more loss")
        else:
            print(f"  {label:34} dominates on both axes")

    # --- where "uncertain" actually goes ------------------------------------------
    # Uncertain is not "sent to a human". R8 escalates only when exposure is over $500
    # or the evidence conflicts; the rest are verified with the cardholder or step-up
    # first, automatically. Counting both as analyst load overstated it.
    original = dict(P.W)
    P.W.update(fitted)
    try:
        full = [score_full(r, by_case.get(r["key_id"], []), email_w) for r in rows]
    finally:
        P.W.clear(); P.W.update(original)
    probs = [p for p, _ in full]
    unc = [(r, sig) for r, (p, sig) in zip(rows, full) if verdict(p) == "uncertain"]
    to_analyst = sum(1 for r, sig in unc
                     if float(r["exposure_usd"] or 0) > 500 or pol._conflicting(sig))
    print(f"\nof the {len(unc):,} uncertain: {to_analyst:,} ({to_analyst / len(rows):.0%} of all "
          f"cases) meet R8 and go to an analyst; {len(unc) - to_analyst:,} "
          f"({(len(unc) - to_analyst) / len(rows):.0%}) are verified automatically first.")

    # --- what the policy actually does to each customer ----------------------------
    # The table above counts VERDICTS. A fraud verdict below 0.85 does not block a card:
    # R1 declines the pending authorisation and asks the cardholder first. So "false"
    # above is fraud verdicts on legitimate cases, not blocked cardholders -- this is.
    impact = {"confirmed_fraud": {}, "cleared": {}}
    for r, (p, sig) in zip(rows, full):
        v = verdict(p)
        pat, _ = P.classify(r, {"txn_ids": [str(r["txn_id"])]}, {"cards": []})
        acts = {a["action"] for a in pol.decide_actions(
            prob=p, verdict=v, exposure=float(r["exposure_usd"] or 0), signals=sig,
            pattern=pat if v != "legitimate" else "none", trigger_type="risk_score",
            customer_denied=False, customer_confirmed=False, recurring=False,
            connected_cards=[], shared_element="", n_confirmed_cards=0, phase="initial")}
        worst = ("card blocked" if acts & {"BLOCK_CARD", "BLOCK_ALL_CARDS"} else
                 "authorisation declined" if "DECLINE_TRANSACTION" in acts else
                 "sent to an analyst" if "ESCALATE_TO_ANALYST" in acts else
                 "cardholder asked to verify" if acts & {"VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"} else
                 "allowed, monitored" if "MONITOR_CARD" in acts else "allowed")
        side = impact["confirmed_fraud" if r["outcome"] == "confirmed_fraud" else "cleared"]
        side[worst] = side.get(worst, 0) + 1
    order = ["card blocked", "authorisation declined", "sent to an analyst",
             "cardholder asked to verify", "allowed, monitored", "allowed"]
    print("\nwhat the agent's first actions do, held-out month (strongest action per case):")
    print(f"  {'':28}{'legitimate':>11}{'fraud':>8}")
    for k in order:
        print(f"  {k:28}{impact['cleared'].get(k, 0):>11,}{impact['confirmed_fraud'].get(k, 0):>8,}")
    print("  a legitimate cardholder is blocked outright only at 0.85+ on two independent "
          "pieces of evidence\n  (policy 6); below that the agent declines and asks.")

    # --- calibration ---------------------------------------------------------------
    # Prior 0 (even odds) is chosen for the exam set, where half the cases are legitimate.
    # This history is 89% fraud, so the probabilities SHOULD read low against it; the
    # table shows by how much, bin by bin, rather than asserting it.
    print("\ncalibration on the held-out month (prior 0 is set for a 50/50 exam set):")
    print(f"  {'predicted':>11} {'cases':>7} {'observed fraud':>15}")
    for b in range(10):
        idx = [i for i, p in enumerate(probs) if b / 10 <= p < (b + 1) / 10 or (b == 9 and p == 1)]
        if idx:
            obs = sum(truth[i] == "confirmed_fraud" for i in idx) / len(idx)
            print(f"  {b / 10:.1f}-{(b + 1) / 10:.1f} {len(idx):>7,} {obs:>15.0%}")
    brier = sum((p - (t == "confirmed_fraud")) ** 2 for p, t in zip(probs, truth)) / len(probs)
    print(f"  Brier score {brier:.3f}")

    # --- the verdict band, fitted rather than assumed -------------------------------
    tr_rows = train.to_dict("records")
    P.W.update(fitted)
    try:
        tr_probs = [score(r, by_case.get(r["key_id"], []), email_w) for r in tr_rows]
    finally:
        P.W.clear(); P.W.update(original)
    _, lo, hi = fit_band(tr_probs, [r["outcome"] for r in tr_rows],
                         [float(r["exposure_usd"] or 0) for r in tr_rows],
                         args.review_cost, args.false_block_cost)
    print(f"\nverdict band fitted on the training half at ${args.review_cost:,.0f}/review and "
          f"${args.false_block_cost:,.0f}/false block: {lo:.2f}-{hi:.2f} "
          f"(shipped 0.30-0.70). On the held-out month:")
    for label, (a, b) in (("shipped 0.30-0.70", (0.30, 0.70)), (f"fitted {lo:.2f}-{hi:.2f}", (lo, hi))):
        c = confusion([verdict(p, a, b) for p in probs], truth, exposure)
        print(f"  {label:20} fraud verdicts on legit {c['fp']:>4}  missed ${c['missed']:>9,.0f}  "
              f"uncertain {(c['unc_f'] + c['unc_c']) / len(rows):>4.0%}")

    # --- memory by resemblance, held out ---------------------------------------------
    # Neighbours are drawn only from cases closed before each test case opened.
    try:
        import similar
        hits = [similar.nearest(r, r["opened_at"], 5) for r in rows]
        knn = ["fraud" if sum(h["outcome"] == "confirmed_fraud" for h in hs) >= 3 else "legitimate"
               for hs in hits]
        c = confusion(knn, truth, exposure)
        print(f"\nsimilar-case memory alone (majority of 5 nearest earlier cases): "
              f"{c['tp']:,} caught, {c['fp']:,} false blocks, ${c['missed']:,.0f} missed. "
              f"Cited, not scored: it reads the features the score already uses.")
    except FileNotFoundError as e:
        print(f"\n(similar-case memory skipped: {e})")

    # Computed, not written down: a closing line with the numbers typed into it goes
    # stale the first time a signal changes, and then the summary is a lie.
    thr = next(c for l, c in results if l == "risk score >= 0.70")
    print(f"\nThe agent is not competing to catch the most fraud -- blocking every card "
          f"catches all of\nit. It competes on what the mistakes cost: "
          f"{impact['cleared'].get('card blocked', 0)} legitimate card blocked (and "
          f"{agent['fp']} fraud verdicts, which decline and ask) against the {thr['fp']} "
          f"blocks at the bank's threshold, and "
          f"{agent['missed'] / thr['missed']:.0%} of the loss\nthe bank's own 0.70 "
          f"threshold would have let through, while declining "
          f"{(agent['unc_f'] + agent['unc_c']) / len(rows):.0%} of the\nset as too "
          f"close to call.")


if __name__ == "__main__":
    main()
