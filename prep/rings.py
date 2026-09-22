#!/usr/bin/env python
"""Connected components over the device-sharing card graph.

`connected_cards` answers "who else touched this device". That is one hop, and one hop
under-reads a ring: card A shares a handset with B, B shares a different handset with C,
and C never appears. Connected components close the transitive hull, which is what policy
R6 is actually asking -- how far does this compromise reach.

TigerGraph runs this with the algorithm library (graph/queries.gsql:ring_component wraps
`tg_conn_comp` over the Card -> Transaction -> DeviceProfile projection). This module is
the mirror that keeps the pipeline runnable without a workspace, and it is the
calibration harness the finding below came out of.

THE FINDING, and why ring_size carries no weight
------------------------------------------------
Transitive device sharing percolates. Sweeping the cap on how many cards a device may
touch before it stops counting as a link:

    cap    cards   components   largest   log-LR of the largest component
      3    1,483         113     1,229    +0.35
      5    2,051          73     1,888    +0.22
      8    2,616          52     2,500    +0.14
     20    3,584          37     3,504    +0.07

At every threshold one giant component swallows the population, and its separation
against the closed cases decays towards zero as it grows. Membership of the giant
component is not "this card is in a fraud ring", it is "this card has ever shared a
browser fingerprint", which describes most of the book. So component size is reported as
evidence -- R6 asks for the shared element to be named -- and given zero weight, exactly
as the one-hop device ring was (measured -0.32, held at 0 in patterns.py).

What the algorithm IS good for is the other tail: bounded components of 4-10 cards on
named hardware, which at cap=8 run 15 confirmed against 2 cleared. Those are rings. They
are what monitor.py sweeps for when the agent investigates on its own initiative.

Run: uv run python prep/rings.py           (writes card_ring into build/fraud.db)
     uv run python prep/rings.py --measure (also prints the separation table above)
"""
from __future__ import annotations
import argparse, collections

import duckdb

DB = "build/fraud.db"

# A device profile only forms an edge if it plausibly describes a machine rather than a
# configuration. Above this many cards it is 'what a Windows laptop looks like'.
MAX_CARDS_PER_DEVICE = 8

RING_DEVICES = f"""
SELECT device_profile FROM tx
WHERE device_profile IS NOT NULL AND device_profile <> ''
  AND device_profile NOT LIKE 'unknown | unknown | unknown%'
GROUP BY 1 HAVING count(DISTINCT card_id) BETWEEN 2 AND {MAX_CARDS_PER_DEVICE}
"""


def components(con) -> dict[str, str]:
    """Union-find over cards joined by a ring-grade device profile."""
    edges = con.sql(f"""
        WITH d AS ({RING_DEVICES})
        SELECT DISTINCT t.device_profile, t.card_id
        FROM tx t JOIN d USING (device_profile)
    """).df()

    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    first: dict[str, str] = {}
    for dev, card in zip(edges.device_profile, edges.card_id):
        if dev in first:
            union(first[dev], card)
        else:
            first[dev] = card
        find(card)
    return {c: find(c) for c in parent}


def build(measure=False):
    con = duckdb.connect(DB)
    comp = components(con)
    size = collections.Counter(comp.values())

    con.execute("CREATE OR REPLACE TABLE card_ring "
                "(card_id VARCHAR, ring_id VARCHAR, ring_size INT)")
    con.executemany("INSERT INTO card_ring VALUES (?,?,?)",
                    [(c, r, size[r]) for c, r in comp.items()])
    con.execute("CREATE INDEX IF NOT EXISTS card_ring_card ON card_ring(card_id)")
    print(f"{len(comp):,} cards in {len(size):,} components; "
          f"largest {max(size.values()):,}")
    print(con.sql("""
        SELECT CASE WHEN ring_size<=3 THEN '2-3' WHEN ring_size<=10 THEN '4-10'
                    WHEN ring_size<=50 THEN '11-50' ELSE '50+' END AS ring,
               count(DISTINCT ring_id) AS components, count(*) AS cards
        FROM card_ring GROUP BY 1 ORDER BY 3 DESC"""))

    if measure:
        print("\nseparation on the 5,565 closed cases (ln likelihood ratio, fraud vs cleared):")
        print(con.sql("""
            WITH j AS (SELECT c.outcome, coalesce(r.ring_size,1) AS rs
                       FROM closed_case c LEFT JOIN card_ring r USING (card_id)),
            b AS (SELECT CASE WHEN rs=1 THEN '1' WHEN rs<=3 THEN '2-3'
                              WHEN rs<=10 THEN '4-10' ELSE '11+' END AS ring, outcome FROM j)
            SELECT ring,
                   sum(CASE WHEN outcome='confirmed_fraud' THEN 1 ELSE 0 END) AS fraud,
                   sum(CASE WHEN outcome<>'confirmed_fraud' THEN 1 ELSE 0 END) AS cleared,
                   round(ln((sum(CASE WHEN outcome='confirmed_fraud' THEN 1.0 ELSE 0 END)
                             / (SELECT count(*) FROM b WHERE outcome='confirmed_fraud'))
                          / nullif(sum(CASE WHEN outcome<>'confirmed_fraud' THEN 1.0 ELSE 0 END)
                             / (SELECT count(*) FROM b WHERE outcome<>'confirmed_fraud'), 0)
                           ), 2) AS log_lr
            FROM b GROUP BY 1 ORDER BY 1"""))
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true")
    build(**vars(ap.parse_args()))
