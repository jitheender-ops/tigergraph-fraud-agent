#!/usr/bin/env python
"""Diff the answer files two backends produce for the same cases.

prep/parity.py proves the feature rows agree. This proves the whole investigation does:
same verdict, same probability, same episode, same evidence, same actions, same SAR.
Run both backends into their own folders first, with --no-llm on both -- the LLM is
non-deterministic prose and would swamp the comparison.

  uv run python run.py --backend duckdb     --no-llm --no-write --out build/cmp/duckdb
  uv run python run.py --backend tigergraph --no-llm --no-write --out build/cmp/tigergraph
  uv run python prep/backend_diff.py build/cmp/duckdb build/cmp/tigergraph
"""
from __future__ import annotations
import json, os, pathlib, sys

# tool_calls differs by design (the graph path makes one extra call for device_reach),
# latency and tokens are wall-clock, and written_to_graph is true only on the graph.
SKIP = {"tool_calls", "latency_s", "tokens", "written_to_graph"}


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in SKIP}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def walk(a, b, path=""):
    """Every leaf that differs, named by its path."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in dict.fromkeys(list(a) + list(b)):
            out += walk(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [(path, f"{len(a)} items", f"{len(b)} items")]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += walk(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [(path, a, b)]


def main():
    left, right = sys.argv[1], sys.argv[2]
    bad = 0
    files = sorted(pathlib.Path(left).glob("*.json"))
    for p in files:
        a = strip(json.loads(p.read_text()))
        q = pathlib.Path(right) / p.name
        if not q.exists():
            print(f"  MISS {p.name} (not in {right})"); bad += 1; continue
        diffs = walk(a, strip(json.loads(q.read_text())))
        print(f"  {'ok  ' if not diffs else 'DIFF'} {p.stem}"
              + ("" if not diffs else f"  {len(diffs)} field(s)"))
        for path, x, y in diffs[:12]:
            bad_once = True
            print(f"        {path}")
            print(f"          {os.path.basename(left):12} {str(x)[:100]}")
            print(f"          {os.path.basename(right):12} {str(y)[:100]}")
        bad += bool(diffs)
    print(f"\n{len(files) - bad}/{len(files)} answer files identical "
          f"(ignoring {', '.join(sorted(SKIP))})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
