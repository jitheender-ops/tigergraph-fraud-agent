"""Graph tools the agent calls.

Two backends behind one surface. TigerGraphBackend runs the installed GSQL queries in
graph/queries.gsql; DuckDBBackend runs the same traversals in SQL so the pipeline is
runnable and testable without a live workspace. Every tool call is counted and logged,
which is what `tool_calls` in the answer file reports.
"""
from __future__ import annotations
import datetime as dt, json, os

CASE_LOG = "build/graph_cases.jsonl"
# Cases an analyst closes from the console while it runs on the DuckDB mirror. The mirror
# is opened read-only, so they land here and a temp view folds them into closed_case --
# every memory query then retrieves them exactly as TigerGraph retrieves a ClosedCase.
CLOSED_LOG = os.path.join(os.getenv("CONSOLE_STATE_DIR", "build"), "console_closed_cases.jsonl")
_CLOSED_COLS = {
    "case_id": "VARCHAR", "customer_id": "VARCHAR", "card_id": "VARCHAR",
    "opened_at": "TIMESTAMP", "closed_at": "TIMESTAMP", "outcome": "VARCHAR",
    "pattern": "VARCHAR", "first_fraud_txn_id": "BIGINT", "txn_ids": "VARCHAR",
    "n_txns": "BIGINT", "exposure_usd": "DOUBLE", "connected_card_ids": "VARCHAR",
    "actions_taken": "VARCHAR", "report_filed": "BOOLEAN", "analyst_notes": "VARCHAR",
}


def reset_case_log(path: str = CASE_LOG):
    """Truncate the case log. Called by the runner that owns a sweep, not by the
    backend: run.py owns the twenty benchmark cases and resets, monitor.py appends its
    self-opened cases to them, because in a live deployment both land in one graph."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").close()


class ToolLog:
    def __init__(self):
        self.calls: list[dict] = []

    def record(self, name, params, n_rows):
        self.calls.append({"tool": name, "params": params, "rows": n_rows})

    @property
    def count(self):
        return len(self.calls)


class _Lookups:
    """The two tools that are lookups, not traversals, so both backends share them."""

    def similar_cases(self, f, before=None, k=5):
        """Nearest closed cases by transaction profile. A lookup over an offline index of the closed cases, not a traversal."""
        import similar
        hits = similar.nearest(f, before, k)
        self.log.record("similar_cases", {"txn_id": str(f.get("txn_id"))}, len(hits))
        return hits

    def email_intel(self, domain):
        """The one evidence source outside the institution."""
        import external
        hit = external.lookup(domain)
        self.log.record("email_intel", {"domain": domain}, 1 if hit["class"] != "unknown" else 0)
        return hit


class DuckDBBackend(_Lookups):
    """Same traversals as the GSQL, expressed over the derived tables."""
    name = "duckdb"

    def __init__(self, path="build/fraud.db", log: ToolLog | None = None):
        import duckdb
        self.con = duckdb.connect(path, read_only=True)
        self.log = log or ToolLog()
        os.makedirs(os.path.dirname(CLOSED_LOG), exist_ok=True)
        open(CLOSED_LOG, "a").close()
        db = self.con.sql("SELECT current_database()").fetchone()[0]
        cols = "{" + ", ".join(f"'{k}': '{v}'" for k, v in _CLOSED_COLS.items()) + "}"
        self.con.execute(f"""
            CREATE TEMP VIEW closed_case AS
            SELECT * FROM "{db}".main.closed_case
            UNION ALL BY NAME
            SELECT * FROM read_json('{CLOSED_LOG}', columns={cols},
                                    format='newline_delimited')""")

    def write_closed_case(self, p) -> bool:
        """The console's close/blacklist write, in the mirror's own column names."""
        row = {
            "case_id": p["p_case_id"], "customer_id": p["p_customer_id"],
            "card_id": p["p_card_id"], "opened_at": p["p_opened_at"],
            "closed_at": p["p_closed_at"], "outcome": p["p_outcome"],
            "pattern": p["p_pattern"],
            "first_fraud_txn_id": int(p["p_first_txn_id"]) if p["p_first_txn_id"] else None,
            "txn_ids": "|".join(p["p_txn_ids"]), "n_txns": p["p_n_txns"],
            "exposure_usd": p["p_exposure"],
            "connected_card_ids": "|".join(p["p_connected_cards"]) or None,
            "actions_taken": p["p_actions_taken"],
            "report_filed": p["p_report_filed"] == "True",
            "analyst_notes": p["p_analyst_notes"],
        }
        with open(CLOSED_LOG, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        self.log.record("write_closed_case", {"case_id": row["case_id"]}, 1)
        return True

    def _df(self, name, sql, params, rows_from=None):
        df = self.con.execute(sql, params).df()
        self.log.record(name, params, len(df))
        return df

    # 1 --------------------------------------------------------------------
    def card_window(self, card_id, t_from, t_to):
        return self._df("card_window", """
            SELECT txn_id, ts, amount, product_cd, channel, risk_score, addr1, p_email,
                   device_profile, id_15, id_23, M1,M2,M3,M4,M5,M6,M7,M8,M9
            FROM tx WHERE card_id = ? AND ts BETWEEN ? AND ? ORDER BY ts
        """, [card_id, t_from, t_to])

    # 2 --------------------------------------------------------------------
    def card_baseline(self, card_id):
        df = self._df("card_baseline",
                      "SELECT * FROM card_profile WHERE card_id = ?", [card_id])
        return df.iloc[0].to_dict() if len(df) else {}

    # 3 --------------------------------------------------------------------
    def device_neighbors(self, device_profile, t_from, t_to):
        df = self._df("device_neighbors", """
            SELECT txn_id, card_id, customer_id, ts, amount, channel, risk_score
            FROM tx WHERE device_profile = ? AND ts BETWEEN ? AND ? ORDER BY ts
        """, [device_profile, t_from, t_to])
        return {"cards": sorted(df.card_id.unique().tolist()),
                "customers": sorted(df.customer_id.unique().tolist()),
                "n_txns": len(df), "txns": df}

    # 4 --------------------------------------------------------------------
    def region_history(self, card_id, region, before):
        df = self._df("region_history", """
            SELECT count(*) AS n_prior, min(ts) AS first_use, max(ts) AS last_prior
            FROM tx WHERE card_id = ? AND addr1 IS NOT DISTINCT FROM ? AND ts < ?
        """, [card_id, region, before])
        return df.iloc[0].to_dict()

    # 5 --------------------------------------------------------------------
    def card_testing_probe(self, card_id, anchor, hours=24):
        return self._df("card_testing_probe", """
            SELECT txn_id, ts, amount, channel, product_cd, device_profile
            FROM tx WHERE card_id = ? AND ts BETWEEN ? - INTERVAL 1 HOUR * ? AND ?
            ORDER BY ts
        """, [card_id, anchor, hours, anchor])

    # 6 --------------------------------------------------------------------
    def prior_cases_for_card(self, card_id, before=None):
        sql = """SELECT case_id, customer_id, card_id, opened_at, outcome, pattern,
                        exposure_usd, n_txns, actions_taken, report_filed, analyst_notes
                 FROM closed_case
                 WHERE (card_id = ? OR connected_card_ids LIKE ?)"""
        p = [card_id, f"%{card_id}%"]
        # an outcome is known only once the case has CLOSED; filtering on opened_at let
        # an investigation read the verdict of a case still being worked
        if before is not None:
            sql += " AND closed_at < ?"
            p.append(before)
        return self._df("prior_cases_for_card", sql + " ORDER BY opened_at DESC", p)

    # 7 --------------------------------------------------------------------
    def prior_cases_for_device(self, device_profile, before=None):
        sql = """SELECT DISTINCT c.case_id, c.outcome, c.pattern, c.opened_at,
                        c.exposure_usd, c.card_id, c.analyst_notes
                 FROM closed_case c, unnest(str_split(c.txn_ids,'|')) AS u(tid)
                 JOIN tx t ON t.txn_id = TRY_CAST(u.tid AS BIGINT)
                 WHERE t.device_profile = ?"""
        p = [device_profile]
        if before is not None:
            sql += " AND c.closed_at < ?"
            p.append(before)
        return self._df("prior_cases_for_device", sql + " ORDER BY c.opened_at DESC LIMIT 25", p)

    # 6b -------------------------------------------------------------------
    def customer_confirmed_cards(self, customer_id, before=None):
        """Policy R10: this customer's cards with a confirmed-fraud closed case."""
        sql = """SELECT DISTINCT card_id FROM closed_case
                 WHERE customer_id = ? AND outcome = 'confirmed_fraud'"""
        p = [customer_id]
        if before is not None:
            sql += " AND closed_at < ?"
            p.append(before)
        return sorted(self._df("customer_confirmed_cards", sql, p).card_id.tolist())

    # helper: full feature row -------------------------------------------------
    def features(self, card_id, txn_id):
        from features import FEATURE_SQL
        self.con.execute("CREATE OR REPLACE TEMP TABLE anchors AS SELECT 'x' AS key_id, ? AS txn_id",
                         [int(txn_id)])
        df = self.con.execute(FEATURE_SQL).df()
        self.log.record("features", {"card_id": card_id, "txn_id": txn_id}, len(df))
        return df.iloc[0].to_dict()

    # 10 -------------------------------------------------------------------
    def ring_component(self, card_id, cap=None):
        """Graph algorithm: the transitive device-sharing component this card sits in.

        TigerGraph runs `tg_conn_comp`; here the same components are precomputed by
        prep/rings.py. Size is reported, never weighted -- see that module for why.
        """
        if cap:   # an analyst asked to look wider than the calibrated cap
            return self._ring_at_cap(card_id, cap)
        df = self._df("ring_component",
                      "SELECT ring_id, ring_size FROM card_ring WHERE card_id = ?", [card_id])
        if not len(df):
            return {"ring_id": "", "ring_size": 1, "members": []}
        row = df.iloc[0]
        m = self.con.execute(
            "SELECT card_id FROM card_ring WHERE ring_id = ? AND card_id <> ? ORDER BY 1 LIMIT 25",
            [row.ring_id, card_id]).df()
        return {"ring_id": str(row.ring_id), "ring_size": int(row.ring_size),
                "members": m.card_id.tolist()}

    def _ring_at_cap(self, card_id, cap):
        """The same component, recomputed with a wider tolerance for how many cards a
        device may touch before it stops counting as a link. prep/rings.py precomputes
        only the calibrated cap of 8, so a deeper look is computed on demand."""
        df = self._df("ring_component", f"""
            WITH RECURSIVE d AS (
              SELECT device_profile FROM tx
              WHERE device_profile IS NOT NULL AND device_profile <> ''
                AND device_profile NOT LIKE 'unknown | unknown | unknown%'
              GROUP BY 1 HAVING count(DISTINCT card_id) BETWEEN 2 AND {int(cap)}
            ),
            e AS (SELECT DISTINCT t.card_id, t.device_profile FROM tx t JOIN d USING (device_profile)),
            reach(card_id) AS (
              SELECT ? UNION
              SELECT e2.card_id FROM reach r
                JOIN e e1 ON e1.card_id = r.card_id
                JOIN e e2 ON e2.device_profile = e1.device_profile
            )
            SELECT card_id FROM reach ORDER BY 1
        """, [card_id])
        members = [c for c in df.card_id.tolist() if c != card_id]
        return {"ring_id": card_id, "ring_size": len(members) + 1, "members": members[:25]}

    def cross_case_entities(self, min_cases=2):
        """Entities recurring across investigations. The SQL mirror reads the same two
        case tables the GSQL walks, so the two answer alike."""
        df = self._df("cross_case_entities", """
            WITH closed AS (
              SELECT card_id AS entity, 'card' AS kind, case_id, pattern FROM closed_case
              UNION ALL
              SELECT trim(u.cid), 'card', c.case_id, c.pattern FROM closed_case c,
                     unnest(str_split(c.connected_card_ids,'|')) AS u(cid)
              WHERE c.connected_card_ids IS NOT NULL AND trim(u.cid) <> ''
              UNION ALL
              -- same ring-grade filter the GSQL applies: a profile on hundreds of
              -- cards recurs across cases because it is Chrome, not because it is a ring
              SELECT t.device_profile, 'device', c.case_id, c.pattern
              FROM closed_case c, unnest(str_split(c.txn_ids,'|')) AS u(tid)
              JOIN tx t ON t.txn_id = TRY_CAST(u.tid AS BIGINT)
              JOIN (SELECT device_profile FROM tx
                    WHERE device_profile IS NOT NULL AND device_profile <> ''
                      AND device_profile NOT LIKE 'unknown | unknown | unknown%'
                    GROUP BY 1 HAVING count(DISTINCT card_id) BETWEEN 2 AND 80) rd
                ON rd.device_profile = t.device_profile
              WHERE t.device_profile IS NOT NULL AND t.device_profile <> ''
            )
            SELECT entity, any_value(kind) AS kind,
                   count(DISTINCT case_id) AS n_cases,
                   string_agg(DISTINCT case_id, '|') AS cases,
                   string_agg(DISTINCT pattern, '|') AS patterns
            FROM closed WHERE entity IS NOT NULL AND entity <> ''
            GROUP BY entity HAVING n_cases >= ?
            ORDER BY n_cases DESC LIMIT 60
        """, [int(min_cases)])
        return df.to_dict("records")

    def closed_case(self, case_id):
        """One closed investigation, for the console's memory drill-down."""
        df = self._df("closed_case", """
            SELECT case_id, customer_id, card_id, opened_at, closed_at, outcome, pattern,
                   n_txns, exposure_usd, actions_taken, report_filed, analyst_notes
            FROM closed_case WHERE case_id = ?""", [case_id])
        return df.iloc[0].to_dict() if len(df) else {}

    # 11 -------------------------------------------------------------------
    def doc_search(self, query, k=2, sources=None):
        """GraphRAG, document half: the policy and regulatory passages that govern
        this case. Local vector search over build/corpus.npz."""
        import retrieve
        hits = retrieve.search(query, k, sources)
        self.log.record("doc_search", {"query": query, "sources": list(sources or ())}, len(hits))
        return hits

    # 12 -------------------------------------------------------------------
    def write_case(self, payload):
        """No live graph: persist to build/graph_cases.jsonl so the write is still
        auditable and the UI can show what would land in TigerGraph."""
        os.makedirs("build", exist_ok=True)
        with open(CASE_LOG, "a") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
        self.log.record("write_case", {"graph_case_id": payload["graph_case_id"]}, 1)
        return payload["graph_case_id"]


class TigerGraphBackend(_Lookups):
    """Runs the installed GSQL queries in graph/queries.gsql."""
    name = "tigergraph"

    def __init__(self, conn, log: ToolLog | None = None):
        self.conn = conn
        self.log = log or ToolLog()

    def _run(self, name, params):
        res = self.conn.runInstalledQuery(name, params)
        # a PRINT block holds either a vertex set or a scalar accumulator; only the
        # former has a length, and card_baseline prints nine scalars.
        n = sum(len(v) if isinstance(v, (list, dict)) else 1
                for blk in res for v in blk.values())
        self.log.record(name, params, n)
        return res

    # RESTPP returns every attribute as JSON, so a DATETIME arrives as a string and the
    # callers -- which were written against the DuckDB mirror's typed frames -- do
    # arithmetic on it. Coerce once, here, rather than in every caller.
    _DATE_COLS = ("ts", "opened_at", "closed_at")
    _NUM_COLS = ("amount", "risk_score", "exposure_usd", "n_txns", "dist1")

    @classmethod
    def _frame(cls, res, key):
        import pandas as pd
        df = pd.DataFrame(cls._rows(res, key))
        for c in cls._DATE_COLS:
            if c in df:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        for c in cls._NUM_COLS:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @staticmethod
    def _rows(res, key):
        """Vertex attributes out of a PRINT result.

        `PRINT T[T.amount]` names the attribute "T.amount", after the set variable, so
        every key carries an alias prefix that the DuckDB mirror's columns do not. Strip
        it once here rather than in six callers."""
        out = []
        for blk in res:
            for v in blk.get(key, []):
                out.append({k.split(".", 1)[-1] if "." in k else k: val
                            for k, val in v["attributes"].items()})
        return out

    @staticmethod
    def _ts(x):
        return x.strftime("%Y-%m-%d %H:%M:%S") if isinstance(x, (dt.datetime, dt.date)) else str(x)

    def card_window(self, card_id, t_from, t_to):
        r = self._run("card_window", {"p_card_id": card_id, "p_from": self._ts(t_from),
                                      "p_to": self._ts(t_to)})
        return self._frame(r, "txns")

    def card_baseline(self, card_id):
        r = self._run("card_baseline", {"p_card_id": card_id})
        out = {}
        for blk in r:
            out.update(blk)
        return out

    def device_neighbors(self, device_profile, t_from, t_to):
        r = self._run("device_neighbors", {"p_device_profile": device_profile,
                                           "p_from": self._ts(t_from), "p_to": self._ts(t_to)})
        cards, custs = [], []
        for blk in r:
            cards += blk.get("card_ids", [])
            custs += blk.get("customer_ids", [])
        txns = self._frame(r, "txns")
        return {"cards": sorted(set(cards)), "customers": sorted(set(custs)),
                "n_txns": len(txns), "txns": txns}

    def region_history(self, card_id, region, before):
        r = self._run("region_history", {"p_card_id": card_id, "p_region": str(region),
                                         "p_before": self._ts(before)})
        out = {}
        for blk in r:
            out.update(blk)
        return {"n_prior": out.get("n_prior_txns_in_region", 0),
                "first_use": out.get("first_ever_use"), "last_prior": out.get("last_prior_use")}

    def card_testing_probe(self, card_id, anchor, hours=24):
        r = self._run("card_testing_probe", {"p_card_id": card_id, "p_anchor": self._ts(anchor),
                                             "p_hours": hours})
        return self._frame(r, "sequence")

    def prior_cases_for_card(self, card_id, before=None):
        import pandas as pd
        r = self._run("prior_cases_for_card", {"p_card_id": card_id})
        df = self._frame(r, "prior_cases")
        if before is not None and len(df):
            df = df[df.closed_at < pd.Timestamp(before)]
        # the SQL mirror orders newest first and the agent cites the top eight, so
        # without this the two backends cite different cases, not just a different order.
        return df.sort_values("opened_at", ascending=False) if len(df) else df

    def prior_cases_for_device(self, device_profile, before=None):
        import pandas as pd
        r = self._run("prior_cases_for_device", {"p_device_profile": device_profile})
        df = self._frame(r, "device_cases")
        if before is not None and len(df):
            df = df[df.closed_at < pd.Timestamp(before)]
        return (df.sort_values("opened_at", ascending=False).head(25)
                if len(df) else df)

    def customer_confirmed_cards(self, customer_id, before=None):
        import pandas as pd
        df = self._frame(self._run("customer_confirmed_cards",
                                   {"p_customer_id": customer_id}), "confirmed")
        if before is not None and len(df):
            df = df[df.closed_at < pd.Timestamp(before)]
        return sorted(set(df.card_id)) if len(df) else []

    def ring_component(self, card_id, cap=None):
        params = {"p_card_id": card_id}
        if cap:
            params["p_max_cards_per_device"] = int(cap)
        r = self._run("ring_component", params)
        out = {}
        for blk in r:
            out.update(blk)
        # a SetAccum comes back in whatever order the engine built it; the SQL mirror
        # orders ascending, and both truncate to 25, so an unsorted set would name a
        # different 25 cards out of the same component.
        members = sorted(c for c in out.get("members", []) if c != card_id)
        return {"ring_id": out.get("ring_id", ""),
                "ring_size": int(out.get("ring_size", 1) or 1), "members": members[:25]}

    def cross_case_entities(self, min_cases=2):
        r = self._run("cross_case_entities", {"p_min_cases": int(min_cases)})
        blk = {}
        for b in r:
            blk.update(b)
        rows = []
        for kind, ck, pk in (("card", "card_cases", "card_patterns"),
                             ("device", "dev_cases", "dev_patterns")):
            pats = blk.get(pk, {})
            for entity, ids in blk.get(ck, {}).items():
                if len(ids) < min_cases:
                    continue
                rows.append({
                    "entity": entity, "kind": kind, "n_cases": len(ids),
                    "cases": "|".join(sorted(ids)),
                    "patterns": "|".join(sorted({p for p in pats.get(entity, []) if p}))})
        rows.sort(key=lambda x: -x["n_cases"])
        return rows[:60]

    def closed_case(self, case_id):
        # getVerticesById RAISES for an id that does not exist rather than returning
        # nothing, so absence arrived at the console as "the database is down". The
        # DuckDB mirror returns {} for a case it does not have, and the two backends
        # have to answer the same question the same way.
        try:
            v = self.conn.getVerticesById("ClosedCase", case_id)
        except Exception as e:                   # noqa: BLE001 - re-raised unless 601
            if "not a valid vertex id" in str(e):
                self.log.record("closed_case", {"case_id": case_id}, 0)
                return {}
            raise
        self.log.record("closed_case", {"case_id": case_id}, len(v))
        return v[0]["attributes"] if v else {}

    def doc_search(self, query, k=2, sources=None):
        """TigerGraph vector search over the DocChunk vertices loaded by
        graph/load.py --docs. Same embedding model as the local mirror, so the two
        return the same passages."""
        import retrieve
        r = self.conn.runInstalledQuery("doc_search", {
            "p_query": retrieve.embed(query).tolist(), "p_k": k,
            "p_sources": list(sources or ())})
        hits = self._rows(r, "hits")
        self.log.record("doc_search", {"query": query, "sources": list(sources or ())}, len(hits))
        return [{"source": h["source"], "section": h["section"], "title": h["title"],
                 "text": h["text"], "doc_id": h["doc_id"],
                 "score": round(float(h.get("score", 0.0)), 4)} for h in hits]

    # ---- feature extraction over the graph ------------------------------
    # The DuckDB mirror computes the feature row in one SQL pass. There is no SQL here,
    # and rewriting that pass as a 90-line GSQL query would bury the calibration in the
    # database. Instead the card's whole history comes back through card_window -- a tool
    # the agent already has -- and the same derivations run over it. The result is
    # asserted equal to the DuckDB row for all twenty cases by prep/parity.py, which is
    # the only reason to believe the two backends agree.
    FLAG_IDX = (0, 1, 2, 4, 5, 6, 7, 8)   # M1,M2,M3,M5..M9; M4 is categorical, not a flag

    def features(self, card_id, txn_id):
        import numpy as np, pandas as pd

        h = self.card_window(card_id, "1970-01-01 00:00:00", "2100-01-01 00:00:00")
        if not len(h):
            raise LookupError(f"card {card_id} has no transactions in the graph")
        h = h.copy()
        h["txn_id"] = h.txn_id.astype(str)
        h["ts"] = pd.to_datetime(h.ts)
        h["amount"] = h.amount.astype(float)
        for c in ("product_cd", "addr1", "p_email", "device_profile", "id_15", "id_23",
                  "m_flags", "channel", "customer_id"):
            h[c] = h[c].fillna("") if c in h else ""
        h = h.sort_values("ts")

        hit = h[h.txn_id == str(txn_id)]
        if not len(hit):
            raise LookupError(f"transaction {txn_id} is not on card {card_id}")
        a = hit.iloc[0]

        before = h[h.ts < a.ts]
        # same amount within 2%, which is the recurring-charge tell (policy R7)
        same_amt = h[(h.amount - a.amount).abs() <= 0.02 * a.amount]
        same_amt_prod = same_amt[same_amt.product_cd == a.product_cd].sort_values("ts")
        # SQL's date_diff('day', a, b) counts day boundaries crossed, not the fractional
        # difference, so the gap is measured between normalised dates or the two backends
        # disagree in the last decimal on every card.
        gaps = same_amt_prod.ts.dt.normalize().diff().dt.days
        w48 = h[(h.ts >= a.ts - dt.timedelta(hours=48)) & (h.ts <= a.ts)]
        w24 = h[(h.ts >= a.ts - dt.timedelta(hours=24)) & (h.ts <= a.ts)]

        dp = a.device_profile or ""
        # DeviceInfo is the first component of the profile, 'X | OS | browser | screen'.
        # The source column is NULL where the identity record had no DeviceInfo and the
        # profile spells that as the literal 'unknown', so it maps back to None to keep
        # the two backends' rows identical.
        device_info = dp.split(" | ")[0] if dp else ""
        if device_info == "unknown":
            device_info = ""
        online = (h.channel == "online")
        flags = str(a.m_flags or "").split(",")

        return {
            "key_id": "x", "txn_id": str(txn_id), "card_id": card_id,
            "customer_id": a.customer_id or None,
            "ts": a.ts.to_pydatetime(), "amount": float(a.amount),
            "product_cd": a.product_cd or None, "channel": a.channel,
            "risk_score": float(a.risk_score) if a.risk_score == a.risk_score else float("nan"),
            "addr1": a.addr1 or None, "p_email": a.p_email or None,
            "device_profile": dp or None, "device_info": device_info or None,
            "id_15": a.id_15 or None, "id_23": a.id_23 or None,

            "n_txns": int(len(h)),
            "amt_median": float(h.amount.median()),
            "amt_p95": float(h.amount.quantile(0.95)),
            "amt_max": float(h.amount.max()),
            "online_share": float(online.sum()) / len(h),
            "n_regions": int(h.loc[h.addr1 != "", "addr1"].nunique()),
            "n_devices": int(h.loc[h.device_profile != "", "device_profile"].nunique()),

            "amt_vs_median": (float(a.amount) / float(h.amount.median())
                              if h.amount.median() else None),
            "amt_over_p95": int(a.amount > h.amount.quantile(0.95)),
            "prior_in_region": int((before.addr1 == a.addr1).sum()),
            "prior_product": int((before.product_cd == a.product_cd).sum()),
            "prior_email": int((before.p_email == a.p_email).sum()),
            "same_amount_n": int(len(same_amt)),
            "same_amount_months": int(same_amt_prod.ts.dt.to_period("M").nunique()),
            "same_amount_gap_days": float(gaps.median()) if gaps.notna().any() else float("nan"),
            "burst_48h": int(len(w48)),
            "small_auths_24h": int(((w24.channel == "online") & (w24.amount < 5)).sum()),

            "dev_cards": self.device_reach(dp) if dp else 0,
            "dev_unknowns": dp.count("unknown"),
            "dev_named_hw": int(bool(device_info)
                                and device_info not in ("Windows", "MacOS", "iOS Device",
                                                        "Trident/7.0", "Linux", "other")
                                and not device_info.startswith("unknown")),
            "dev_specific": int(bool(dp) and not dp.startswith("unknown | unknown | unknown")),
            "dev_new": int(a.id_15 == "New"),
            "proxy": int(bool(a.id_23) and a.id_23 != "IP_PROXY:TRANSPARENT"),
            "channel_odd": int((a.channel == "online" and float(online.sum()) / len(h) < 0.1)
                               or (a.channel == "in_person"
                                   and float(online.sum()) / len(h) > 0.9)),
            # M1..M9 are booleans in the source, so a failed match is the string
            # "false" once packed, not "F". SQL's `M1 = 'F'` means the same thing:
            # DuckDB reads 'F' as the boolean false.
            "m_false_n": sum(1 for i in self.FLAG_IDX
                             if i < len(flags) and flags[i].strip().lower() == "false"),
        }

    def device_reach(self, device_profile):
        r = self._run("device_reach", {"p_device_profile": device_profile})
        for row in self._rows(r, "device"):
            return int(row.get("n_cards") or 0)
        return 0

    def write_closed_case(self, params) -> bool:
        self._run("write_closed_case", params)
        return True

    def write_case(self, payload):
        self._run("write_case", {
            "p_graph_case_id": payload["graph_case_id"],
            "p_source_case_id": payload["source_case_id"],
            "p_customer_id": payload["customer_id"], "p_card_id": payload["card_id"],
            "p_opened_at": self._ts(payload["opened_at"]), "p_status": payload["status"],
            "p_verdict": payload["verdict"],
            "p_fraud_probability": payload["fraud_probability"],
            "p_pattern": payload["pattern"],
            "p_pattern_description": payload["pattern_description"],
            "p_exposure": payload["exposure_usd"], "p_summary": payload["summary"],
            "p_stop_reason": payload["stop_reason"],
            "p_actions_final": payload["actions_final"], "p_sar_filed": payload["sar_filed"],
            "p_txn_ids": payload["txn_ids"], "p_connected_cards": payload["connected_cards"],
            "p_devices": payload["devices"], "p_prior_cases": payload["prior_cases"],
        })
        return payload["graph_case_id"]
