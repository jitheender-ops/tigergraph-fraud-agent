"""GraphRAG, document half.

The graph answers "what happened". It cannot answer "what is the bank allowed to do
about it" or "what must a regulator be told" -- those live in prose: the bank's fraud
policy, its five documented typologies, and FinCEN's SAR guidance. Both are retrieved
into the same evidence list, so a claim sourced `document` sits beside a claim sourced
`graph` and the LLM sees the governing passage rather than a hard-coded prompt.

Chunks and vectors are built by prep/build_corpus.py. Retrieval runs locally over
build/corpus.npz, and against TigerGraph's vector store when a workspace is configured
(graph/load.py --docs pushes the same vectors, graph/queries.gsql:doc_search reads them).
Both paths return the same shape, exactly as the graph tools do.
"""
from __future__ import annotations
import json, pathlib

import numpy as np

MODEL = "BAAI/bge-small-en-v1.5"
_CORPUS: tuple[np.ndarray, list[dict]] | None = None
_EMB = None


def _load():
    """Corpus and embedder are loaded once per process, not once per case."""
    global _CORPUS, _EMB
    if _CORPUS is None:
        base = pathlib.Path("build")
        if not (base / "corpus.npz").exists():
            raise FileNotFoundError(
                "build/corpus.npz is missing. Run: uv run python prep/build_corpus.py")
        vecs = np.load(base / "corpus.npz")["vectors"]
        chunks = json.loads((base / "corpus.json").read_text())
        _CORPUS = (vecs, chunks)
    if _EMB is None:
        from fastembed import TextEmbedding
        _EMB = TextEmbedding(model_name=MODEL)
    return _CORPUS, _EMB


def embed(text: str) -> np.ndarray:
    (_, _), emb = _load()
    v = np.array(next(iter(emb.embed([text]))), dtype=np.float32)
    return v / np.linalg.norm(v)


def search(query: str, k: int = 3, sources: tuple[str, ...] | None = None) -> list[dict]:
    """Cosine search over the corpus. `sources` restricts to e.g. the policy only, which
    matters because the FinCEN PDFs are 284 of the 309 chunks and would otherwise drown
    a nine-rule policy on any query phrased in regulatory language."""
    (vecs, chunks), _ = _load()
    q = embed(query)
    sims = vecs @ q
    idx = np.argsort(-sims)
    out = []
    for i in idx:
        c = chunks[int(i)]
        if sources and c["source"] not in sources:
            continue
        out.append({**c, "score": round(float(sims[int(i)]), 4)})
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# What a case needs to retrieve.
#
# Three questions, each answered by a different part of the corpus, and each asked in
# the language of the document rather than the language of the graph:
#   - which typology does this activity match?          -> fraud_patterns
#   - which rule governs the action taken?              -> fraud_policy
#   - what must the filing contain?                     -> FinCEN (only when filing)
# ---------------------------------------------------------------------------
POLICY = ("fraud_policy",)
PATTERNS = ("fraud_patterns",)
FINCEN = ("fincen_sar_narrative", "fincen_sar_complete", "fincen_sar_faqs",
          "fincen_sar_tti", "fincen_sar_supporting_docs", "fincen_identity")

# The five typologies are an enum with five chunks, so they are looked up, not searched.
# Cosine similarity over five near-identical paragraphs misfiled pattern 3 as pattern 2
# one time in five; an exact mapping cannot. Vector search is kept for the two corpora
# where it earns its place: the policy (a situation described in graph terms has to find
# the rule that governs it) and FinCEN (284 chunks of continuous prose).
PATTERN_CHUNK = {
    "card_testing": "Card testing",
    "card_not_present_fraud": "Card-not-present fraud",
    "card_not_present_new_device": "Card-not-present fraud from a new device",
    "out_of_region_use": "Out-of-region use",
    "account_takeover": "Account takeover",
}


def typology(pattern: str) -> list[dict]:
    """The documented pattern definition, by name."""
    section = PATTERN_CHUNK.get(pattern)
    if not section:
        return []
    (_, chunks), _ = _load()
    return [{**c, "score": 1.0} for c in chunks
            if c["source"] == "fraud_patterns" and c["section"] == section]


def queries_for(pattern, verdict, *, connected, customer_denied, customer_confirmed,
                recurring, no_reply, uncertain_and_exposed, filing) -> list[tuple[str, tuple, int]]:
    """The retrieval plan for one case: (query, source filter, k) triples.

    k is 1 for the policy and the typologies because each query names one situation and
    there is exactly one rule for it -- taking two pulls in the omnibus "3. Rules" chunk,
    which only restates the rule already retrieved. The FinCEN corpus is 284 chunks of
    continuous prose with no such one-to-one mapping, so it takes two.
    """
    qs: list[tuple[str, tuple, int]] = []

    # the rule the action set will actually be justified by
    if recurring:
        rule = "cardholder disputes a charge that matches their own recurring monthly pattern"
    elif customer_confirmed:
        rule = "the customer confirms the transaction was theirs"
    elif customer_denied:
        rule = "the customer denies making the transaction, block and reissue the card"
    elif no_reply:
        rule = "no reply from the customer within 24 hours of a verification request"
    elif pattern == "card_testing":
        rule = "card testing sequence, decline and require step-up authentication"
    elif pattern == "undocumented":
        rule = "activity fits none of the known patterns, describe it and escalate"
    elif uncertain_and_exposed:
        rule = "escalate to a human analyst when the verdict is uncertain and exposure is high"
    elif verdict == "legitimate":
        rule = "stopping: the evidence explains the alarm and further investigation would not change it"
    else:
        rule = "verify with the customer before blocking on a weak single signal"
    qs.append((rule, POLICY, 1))

    if connected:
        qs.append(("shared origin: one device profile or region used by several cards", POLICY, 1))
    if filing:
        qs.append(("what a suspicious activity report narrative must contain: who what when "
                   "where why the activity is suspicious", FINCEN, 2))
    return qs


def to_evidence(hits: list[dict], seen: set[str]) -> list[dict]:
    """Turn retrieved chunks into evidence rows. Deduped by doc_id across queries."""
    out = []
    for h in hits:
        if h["doc_id"] in seen:
            continue
        seen.add(h["doc_id"])
        out.append({
            "claim": f"{h['title']}: {_clip(h['text'])}",
            "ref": f"document:{h['source']}#{h['section']}",
            "doc_id": h["doc_id"],
            "score": h["score"],
        })
    return out


def _clip(text: str, n: int = 420) -> str:
    """A policy rule is short enough to quote whole; a FinCEN page is not. Cut on a
    sentence boundary so the claim never ends mid-clause."""
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    cut = t[:n]
    stop = cut.rfind(". ")
    return (cut[:stop + 1] if stop > n // 2 else cut.rstrip() + " ...")


def demo():
    """Self-check: the rule that governs each situation must be the rule retrieved."""
    for q, want in [
        ("the customer denies making the transaction, block and reissue the card", "R2"),
        ("card testing sequence, decline and require step-up authentication", "R5"),
        ("cardholder disputes a charge that matches their own recurring monthly pattern", "R7"),
        ("escalate to a human analyst when the verdict is uncertain and exposure is high", "R8"),
        ("activity fits none of the known patterns, describe it and escalate", "R9"),
        ("shared origin: one device profile or region used by several cards", "R6"),
    ]:
        top = search(q, k=3, sources=POLICY)
        got = [h["section"] for h in top]
        assert want in got, f"{want!r} not in top-3 for {q!r}: {got}"
        print(f"  ok  {want:4} <- {q[:56]:58} {got}")
    for pat, section in PATTERN_CHUNK.items():
        got = typology(pat)
        assert got and got[0]["section"] == section, (pat, got)
    assert typology("undocumented") == [] and typology("none") == []
    print(f"  ok  typology lookup for all {len(PATTERN_CHUNK)} documented patterns")
    fin = search("what a suspicious activity report narrative must contain", k=1, sources=FINCEN)
    assert fin[0]["source"].startswith("fincen"), fin[0]["source"]
    print(f"  ok  fincen  <- {fin[0]['title']} / {fin[0]['section']}")
    print("retrieve.py: all checks passed")


if __name__ == "__main__":
    demo()
