#!/usr/bin/env python
"""TigerGraph's library algorithm and the pipeline agree on every ring.

Runs tg_wcc -- unmodified, from TigerGraph's graph algorithm library -- over the
RING_DEVICE edges (graph/load.py --rings), and asserts that the components it finds are
exactly the ones in card_ring, which prep/rings.py computes and the agent, the monitoring
sweep and the backtest read. Same partition, not just the same sizes: every pair of cards
in one component in one is in one component in the other.

  uv run python prep/ring_parity.py
"""
from __future__ import annotations
import collections, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import duckdb
from dotenv import load_dotenv
load_dotenv()
from tg import connect


def main():
    res = connect().runInstalledQuery("tg_wcc", {
        "v_type_set": ["Card", "DeviceProfile"], "e_type_set": ["RING_DEVICE"],
        "print_limit": -1, "print_results": True}, timeout=600_000)
    rows = next(b["Start"] for b in res if "Start" in b)
    lib = {v["v_id"]: v["attributes"]["Start.@min_cc_id"] for v in rows if v["v_type"] == "Card"}

    con = duckdb.connect("build/fraud.db", read_only=True)
    ours = dict(con.sql("SELECT card_id, ring_id FROM card_ring").fetchall())

    def groups(label):
        g = collections.defaultdict(set)
        for card, comp in label.items():
            g[comp].add(card)
        return {frozenset(m) for m in g.values() if len(m) > 1}

    a, b = groups({c: lib[c] for c in lib}), groups(ours)
    sizes = sorted((len(m) for m in b), reverse=True)
    print(f"tg_wcc (library): {len(a):,} multi-card components   "
          f"pipeline card_ring: {len(b):,}   largest {sizes[0]:,}")
    missing, extra = b - a, a - b
    if missing or extra:
        sys.exit(f"MISMATCH: {len(missing)} components only in card_ring, "
                 f"{len(extra)} only in tg_wcc")
    print(f"identical partition: {sum(len(m) for m in a):,} cards in {len(a):,} components")


if __name__ == "__main__":
    main()
