#!/usr/bin/env python
"""Build the GraphRAG document corpus: chunk, embed, and store.

Two kinds of text matter to a fraud investigation and neither is in the transaction
graph: the bank's own policy (what the agent is allowed to do and when) and the
regulators' guidance (how a SAR narrative must read). Both are chunked, embedded and
stored so the agent retrieves the passage that governs the case it is looking at,
rather than having the policy hard-coded into a prompt.

Vectors land in build/corpus.npz for local use and are pushed to TigerGraph's vector
store by graph/load.py --docs when a workspace is configured.
"""
from __future__ import annotations
import json, pathlib, re, sys

import numpy as np

DOCS = pathlib.Path("data/docs")
OUT = pathlib.Path("build")
MODEL = "BAAI/bge-small-en-v1.5"          # 384-dim, ONNX, no torch
CHUNK_WORDS, OVERLAP = 220, 40


def readme_sections() -> list[dict]:
    """The policy and the pattern definitions live in the dataset README. They are the
    most load-bearing documents the agent has, so they are chunked by heading rather
    than by word count: a rule must never be split across two chunks."""
    txt = (pathlib.Path("data/README.md")).read_text()
    out = []

    # the five known patterns, one chunk each
    m = re.search(r"## The five known fraud patterns(.*?)\n## ", txt, re.S)
    if m:
        for pm in re.finditer(r"\*\*(\d)\. ([^*]+)\.\*\*(.*?)(?=\n\*\*\d\.|\Z)", m.group(1), re.S):
            out.append({
                "source": "fraud_patterns", "section": pm.group(2).strip(),
                "title": f"Known fraud pattern {pm.group(1)}: {pm.group(2).strip()}",
                "text": f"{pm.group(2).strip()}. {pm.group(3).strip()}",
            })

    # the policy: split on its numbered rules and sections
    pol = txt[txt.index("# Fraud Policy"):txt.index("# Answer Format")]
    # R10's heading ends "...BLOCK_ALL_CARDS**" with no full stop inside the bold, so the
    # trailing period is optional or the last rule in the policy silently never gets indexed.
    for rm in re.finditer(r"\*\*(R\d+)\.\s*(.+?)\.?\*\*(.*?)(?=\n\*\*R\d+\.|\n###|\Z)", pol, re.S):
        out.append({
            "source": "fraud_policy", "section": rm.group(1),
            "title": f"Policy rule {rm.group(1)}: {rm.group(2).strip()}",
            "text": f"{rm.group(1)}. {rm.group(2).strip()}. {rm.group(3).strip()}",
        })
    for sm in re.finditer(r"\n### (\d+[a-z]?\.? [^\n]+)\n(.*?)(?=\n### |\Z)", pol, re.S):
        body = sm.group(2).strip()
        if len(body) > 80:
            out.append({"source": "fraud_policy", "section": sm.group(1).strip(),
                        "title": f"Fraud Policy - {sm.group(1).strip()}", "text": body})
    return out


def pdf_chunks() -> list[dict]:
    from pypdf import PdfReader
    out = []
    for p in sorted(DOCS.glob("*.pdf")):
        try:
            pages = [(i + 1, (pg.extract_text() or "")) for i, pg in enumerate(PdfReader(str(p)).pages)]
        except Exception as e:
            print(f"  ! {p.name}: {e}")
            continue
        text = "\n".join(t for _, t in pages)
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()
        words = text.split()
        step = CHUNK_WORDS - OVERLAP
        n = 0
        for i in range(0, len(words), step):
            chunk = " ".join(words[i:i + CHUNK_WORDS])
            if len(chunk) < 200:
                continue
            n += 1
            out.append({"source": p.stem, "section": f"chunk {n}",
                        "title": p.stem.replace("_", " "), "text": chunk})
        print(f"  {p.stem:30} {len(pages):>4} pages -> {n:>4} chunks")
    return out


def main():
    OUT.mkdir(exist_ok=True)
    chunks = readme_sections()
    print(f"policy + patterns: {len(chunks)} chunks")
    chunks += pdf_chunks()
    print(f"total: {len(chunks)} chunks")

    from fastembed import TextEmbedding
    print(f"embedding with {MODEL} ...")
    emb = TextEmbedding(model_name=MODEL)
    texts = [f"{c['title']}. {c['text']}" for c in chunks]
    vecs = np.array(list(emb.embed(texts)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

    for i, c in enumerate(chunks):
        c["doc_id"] = f"{c['source']}::{c['section']}".replace(" ", "_")
        c["idx"] = i
    np.savez_compressed(OUT / "corpus.npz", vectors=vecs)
    (OUT / "corpus.json").write_text(json.dumps(chunks, indent=1))
    print(f"wrote build/corpus.npz {vecs.shape} and build/corpus.json")


if __name__ == "__main__":
    main()
