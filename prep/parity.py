#!/usr/bin/env python
"""Assert that the TigerGraph and DuckDB backends produce the same feature row.

The DuckDB mirror computes the features in one SQL pass. TigerGraph has no SQL, so the
same derivations run in Python over what `card_window` returns. Two implementations of
one definition drift the moment nobody checks, and every weight in patterns.py was
calibrated against the SQL one -- so if the graph path disagrees, the calibration does
not apply to it and the whole argument for the numbers collapses.

  uv run python prep/parity.py            # the 20 benchmark anchors
  uv run python prep/parity.py --verbose  # print every field that differs
"""
from __future__ import annotations
import argparse, math, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

import duckdb
from dotenv import load_dotenv
load_dotenv()
from backend import DuckDBBackend, TigerGraphBackend
from tg import connect

# ts comes back from RESTPP without sub-second parts and as a naive local string; the
# rest are compared exactly, or within a float epsilon.
SKIP = {"key_id"}
EPS = 1e-6


def blank(x):
    """DuckDB hands back NaN for a missing string, TigerGraph an empty one; the agent
    tests both with `x is not None and x == x`, so they are the same value."""
    return x is None or x == "" or (isinstance(x, float) and math.isnan(x))


def close(a, b):
    if blank(a) and blank(b):
        return True
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or abs(a - b) <= EPS * max(1.0, abs(a))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= EPS * max(1.0, abs(float(a)))
    return str(a) == str(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect("build/fraud.db", read_only=True)
    anchors = con.sql("SELECT case_id, card_id, flagged_txn_id FROM case_pack ORDER BY 1")\
                 .df().to_dict("records")
    duck, tiger = DuckDBBackend(), TigerGraphBackend(connect())

    bad = 0
    for a in anchors:
        d = duck.features(a["card_id"], int(a["flagged_txn_id"]))
        t = tiger.features(a["card_id"], int(a["flagged_txn_id"]))
        diffs = [(k, d[k], t.get(k)) for k in d
                 if k not in SKIP and not close(d[k], t.get(k))]
        # DuckDB returns numpy/pandas scalars and None; normalise before reporting
        diffs = [(k, dv, tv) for k, dv, tv in diffs
                 if not (dv is None and tv in (None, "", 0))
                 and not (str(dv) == str(tv))]
        mark = "ok " if not diffs else "DIFF"
        print(f"  {mark} {a['case_id']}  {a['card_id']:12} txn {a['flagged_txn_id']}"
              + ("" if not diffs else f"  {len(diffs)} field(s)"))
        if diffs:
            bad += 1
            if args.verbose or len(diffs) <= 6:
                for k, dv, tv in diffs:
                    print(f"        {k:22} duckdb={dv!r}  tigergraph={tv!r}")

    print(f"\n{len(anchors) - bad}/{len(anchors)} feature rows identical across backends")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
