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


def score(row, prior) -> float:
    """The shipped scorer, on one case. No episode and no live device ring: a closed case
    has neither, and inventing them would score a different agent than the one that ships."""
    sig = P.score_signals(row, {"txn_ids": [str(row["txn_id"])]},
                          {"cards": []}, prior)
    return 1.0 / (1.0 + math.exp(-sum(s.weight for s in sig)))


def verdict(p):
    return "fraud" if p >= 0.70 else "legitimate" if p <= 0.30 else "uncertain"


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
          ON b.card_id = a.card_id AND b.opened_at < a.opened_at
    """).df()
    by_case: dict[str, list] = {}
    for r in priors.itertuples():
        by_case.setdefault(r.case_id, []).append(
            {"case_id": r.prior_id, "outcome": r.outcome, "pattern": r.pattern})

    fitted = measure(train)
    print("weights refitted on the training half (shipped value in brackets):")
    for k, v in fitted.items():
        shipped = P.W[k]
        flag = "" if abs(v - shipped) < 0.15 else "   <- moved"
        print(f"  {k:20} {v:>+6.2f}   [{shipped:>+5.2f}]{flag}")

    rows = test.to_dict("records")
    truth = [r["outcome"] for r in rows]
    exposure = [float(r["exposure_usd"] or 0) for r in rows]

    def run_agent(weights, label):
        original = dict(P.W)
        P.W.update(weights)
        try:
            preds = [verdict(score(r, by_case.get(r["key_id"], []))) for r in rows]
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
        policies.append(run_agent({}, "agent, shipped weights (in-sample)"))

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
          f"{agent['fp']:,} false blocks, ${agent['missed']:,.0f} of loss let through,\n"
          f"and {agent['unc_f'] + agent['unc_c']:,} of {len(rows):,} "
          f"({(agent['unc_f'] + agent['unc_c']) / len(rows):.0%}) declined and routed to "
          f"a human under R8.\n")
    print("break-even against it:\n")
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

    print("\nThe agent is not competing to catch the most fraud. Blocking every card "
          "catches all\nof it. It is competing on what the mistakes cost, and on how "
          "often it admits it does\nnot know: 3 false blocks against 144, and a fifth "
          "of the losses the bank's own 0.70\nthreshold would have let through.")


if __name__ == "__main__":
    main()
