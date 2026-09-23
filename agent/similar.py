"""Case memory by resemblance, not by identity.

prior_cases_for_card and prior_cases_for_device find cases that share an entity with
this one. The brief also asks for "similar past cases when a new alert resembles an old
one" -- and since every cleared case carries pattern 'none', resemblance cannot be read
off the pattern label. It is read off the transaction instead: each closed case's anchor
transaction becomes a vector of the features the scorer uses, and a new alert retrieves
its nearest neighbours among cases closed before it opened.

The index is built offline by prep/case_index.py. Both backends share it, the way they
share the email-domain lookup: it is a lookup over the closed cases, not a traversal.
ponytail: brute-force distance over ~5.6k rows; swap for an ANN index past ~1M cases.
"""
from __future__ import annotations
import math, pathlib

import numpy as np

INDEX = pathlib.Path("build/case_index.npz")
_IDX = None


def vector(f) -> list[float]:
    """The features that separate fraud from cleared in calibrate.py, as numbers."""
    def num(k, default=0.0):
        v = f.get(k)
        return default if v is None or v != v else float(v)
    return [
        num("m_false_n"), num("dev_new"), float(num("prior_in_region", 1) == 0),
        math.log1p(max(num("amt_vs_median", 1.0), 0.0)), num("channel_odd"),
        math.log1p(num("burst_48h")), float(num("small_auths_24h") >= 3), num("proxy"),
        num("risk_score", 0.5), float(f.get("channel") == "online"), num("dev_specific"),
        math.log1p(num("dev_cards")),
    ]


def _load():
    global _IDX
    if _IDX is None:
        if not INDEX.exists():
            raise FileNotFoundError(f"{INDEX} is missing. Run: uv run python prep/case_index.py")
        z = np.load(INDEX, allow_pickle=True)
        _IDX = {k: z[k] for k in z.files}
    return _IDX


def nearest(f, before=None, k=5) -> list[dict]:
    """The k closed cases whose anchor transaction most resembles this one."""
    z = _load()
    q = (np.array(vector(f)) - z["mean"]) / z["std"]
    d = np.sqrt(((z["vecs"] - q) ** 2).sum(axis=1))
    if before is not None:
        d = np.where(z["opened_at"] < np.datetime64(before), d, np.inf)
    out = []
    for i in np.argsort(d)[:k]:
        if not np.isfinite(d[i]):
            break
        out.append({"case_id": str(z["case_ids"][i]), "outcome": str(z["outcomes"][i]),
                    "pattern": str(z["patterns"][i]), "distance": round(float(d[i]), 3)})
    return out


def claim(hits) -> str:
    n_f = sum(h["outcome"] == "confirmed_fraud" for h in hits)
    pats = sorted({h["pattern"] for h in hits if h["outcome"] == "confirmed_fraud"})
    return (f"The {len(hits)} closed cases whose transaction most resembles this one "
            f"({', '.join(h['case_id'] for h in hits)}) closed as {n_f} confirmed fraud"
            + (f" ({', '.join(p.replace('_', ' ') for p in pats)})" if pats else "")
            + f" and {len(hits) - n_f} cleared. Cited as memory; it carries no weight, "
              f"because it resembles on the same features the score already uses")
