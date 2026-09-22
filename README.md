# Agentic Fraud Investigation on TigerGraph

An agent that takes a fraud alert, investigates it against a transaction knowledge graph
and the bank's closed-case history, decides what kind of fraud it is (if any), recommends
the next best action under the bank's written policy, and records the whole thing as a
case the next investigation can retrieve.

Built for the TigerGraph × Hacker House Goa challenge on the IEEE-CIS / Vesta dataset:
590,742 transactions, 144,432 identity records, 5,565 closed investigations, 20 exam cases.

```bash
uv sync
uv run python prep/to_parquet.py        # raw CSVs  -> build/fraud.db
uv run python prep/derive.py            # card_id, device profiles, baselines
uv run python prep/validate_card_id.py  # the derivation is load-bearing; assert it
uv run python prep/rings.py             # connected components over the device graph
uv run python prep/build_corpus.py      # chunk + embed the policy, typologies, FinCEN PDFs
uv run python graph/load.py --all       # schema + data + docs + queries into TigerGraph
uv run python run.py --backend tigergraph
uv run python validate.py               # check all 20 answers against the spec
uv run python monitor.py --top 5        # cases nobody asked for: the self-directed sweep
uv run python ui/build.py && open ui/dashboard.html
```

Three backends, one tool surface:

| `--backend` | how the graph is reached |
|---|---|
| `duckdb` | the same traversals in SQL, so the pipeline runs with no workspace |
| `tigergraph` | the installed GSQL queries over pyTigerGraph |
| `mcp` | the same installed queries over [TigerGraph MCP](https://github.com/tigergraph/tigergraph-mcp) |

## What the agent does

```
trigger ──▶ investigate ──▶ assess ──▶ request evidence ──▶ re-assess ──▶ act ──▶ explain ──▶ remember
```

Seven graph calls per case, on average: pull the flagged transaction, read the card's own
baseline, expand through the device profile to other cards, check the card's history in
the billing region, probe for a card-testing sequence, retrieve prior closed cases for the
card and the device, then reconstruct the episode and size the exposure.

The agent then scores the evidence, classifies the pattern, recommends an initial action
set, asks for whatever evidence the policy says it may ask for, folds the response back in,
and re-recommends. Both recommendations are recorded, with what changed between them.

## GraphRAG: both halves

The graph answers *what happened*. It cannot answer *what the bank is allowed to do about
it* or *what a regulator has to be told* — that lives in prose. `prep/build_corpus.py`
chunks three corpora and embeds them with `BAAI/bge-small-en-v1.5`: the bank's fraud policy
split so that a rule is never cut in half (25 chunks), the five documented typologies, and
six FinCEN SAR/identity-theft PDFs (284 chunks). They load into TigerGraph as `DocChunk`
vertices with a 384-dimension `COSINE` vector attribute, and `doc_search` retrieves them
with `vectorSearch`.

Retrieval happens **before** the action decision, not after it, so the rule is grounding
rather than post-hoc justification. Each case retrieves the typology, the policy rule that
governs its situation — the situation is described in graph language and has to *find* the
rule — and, only when a filing is warranted, the FinCEN narrative guidance, before a word of
the narrative is written. The passages land in `case.evidence` with `source: "document"`
beside the graph claims, and they are what the LLM is grounded on.

They carry **zero weight**. A policy rule is not evidence that this cardholder committed
fraud; it is the rule the recommendation has to satisfy. Letting it move the probability
would be double-counting the bank's own instructions as proof.

The five typologies are looked up by name rather than searched: cosine similarity over five
near-identical paragraphs misfiled pattern 3 as pattern 2 one time in five, and an enum with
five values does not need a vector index.

## A graph algorithm, and a negative result worth keeping

`connected_cards` is one hop — who else touched this handset. A ring is transitive: A shares
a phone with B, B shares a different phone with C, and one hop never reaches C.
`ring_component` closes the hull (GSQL breadth-first expansion to a fixed point; the mirror
in `prep/rings.py` is what the calibration ran on).

Sweeping the cap on how many cards a device may touch before it stops counting as a link:

| cap | cards | components | largest | log-LR of the largest |
|---:|---:|---:|---:|---:|
| 3 | 1,483 | 113 | 1,229 | +0.35 |
| 5 | 2,051 | 73 | 1,888 | +0.22 |
| 8 | 2,616 | 52 | 2,500 | +0.14 |
| 20 | 3,584 | 37 | 3,504 | +0.07 |

Transitive device sharing **percolates**. At every threshold one giant component swallows
the population and its separation decays as it grows: membership is not "this card is in a
ring", it is "this card has ever shared a browser fingerprint". So component size is named
as evidence, as R6 requires, and given no weight — the same call the one-hop ring got.

The bounded tail is where the algorithm earns its place. Components of 4–10 cards run 15
confirmed fraud against 2 cleared, and their members feed `MONITOR_CONNECTED_CARDS` even
where no single device links the cards directly. The giant component never does: monitoring
2,500 cards is not an action, it is a denial of service on the fraud desk.

## Cases nobody asked for

All twenty benchmark cases arrive from a trigger somebody else pulled. `monitor.py` is the
other half: it ranks the bounded ring components by money moved in the exam window, discards
any that touch a benchmark card, and runs the same investigation, policy engine and answer
format over each. Output is `monitoring/`, same shape as `cases/`, appended to the same
graph case log. The top five move $6.9k–$17.5k apiece and one of them files a SAR.

## How TigerGraph is used

`graph/schema.gsql` — Customer, Card, Transaction, DeviceProfile, EmailDomain,
BillingRegion, ClosedCase and Case vertices; `OWNS`, `MADE`, `FROM_DEVICE`,
`PURCHASER_EMAIL`, `BILLED_IN`, `NEXT`, `INVOLVES`, `ON_CARD`, `CONNECTED_TO` edges, plus
the `CASE_*` edges the agent writes back — and `DocChunk`, whose 384-dimension vector
attribute is the GraphRAG document store.

`graph/queries.gsql` — twelve installed GSQL queries that *are* the agent's tools:
`card_window`, `card_baseline`, `device_neighbors`, `region_history`,
`card_testing_probe`, `prior_cases_for_card`, `prior_cases_for_device`, `connected_cards`,
`region_cluster`, `ring_component`, `doc_search`, `write_case`. The multi-hop ones do the
work that a row store cannot:
`device_neighbors` walks DeviceProfile ← Transaction ← Card to find every other cardholder
who used one machine; `connected_cards` expands two hops from a card through its device
profiles and back out; `prior_cases_for_device` reaches a closed investigation through the
transactions that shared a device.

`write_case` closes the memory loop. Every investigation is written back as a `Case`
vertex with edges to its transactions, its connected cards, its device profiles and the
prior cases it cited, so a case that names a device becomes evidence for the next analyst.

**Over MCP.** `agent/mcp_backend.py` reaches the same installed queries through the
TigerGraph MCP server instead of pyTigerGraph. Because the ten tools are already installed
GSQL, the whole surface maps onto one MCP tool, `tigergraph__run_installed_query`, whose
arguments are exactly `runInstalledQuery(name, params)` — so `MCPBackend` is
`TigerGraphBackend` with its connection swapped for a shim, and the response shaping, which
is the part with the bugs in it, is written once. `.mcp.json` registers the server for any
MCP client. The client is async and the investigation loop is not, so the session lives in
its own event loop on a daemon thread.

## Recovering `card_id`

The answer files are keyed on card IDs like `C08623-K2`, but there is no card column in
`transactions.csv` — it has to be derived, and every ID in every answer depends on getting
it right. Ranking each customer's distinct `(card1, card6)` tuples ascending, NULLs first,
reproduces **4,665 of 4,665** card IDs in `closed_cases_history.csv` and **20 of 20** in
`case_pack.csv`. Since `customer_id` is one-to-one with `card1`, what actually distinguishes
a customer's cards is `card6` — credit before debit. `prep/validate_card_id.py` asserts it.

## Calibration, and the trap in the labelled data

Signal weights are log-likelihood ratios measured on the 4,665 confirmed and 900 cleared
closed cases (`prep/calibrate.py`), not chosen by taste. Measuring them changed the sign of
four signals that intuition had backwards: a corroborated new device (−1.85, not +1.0),
proxy use (−1.39, not +1.2), a 4× amount outlier (−0.97, not +0.6) and an odd channel
(−0.99, not +0.8). A rare shared device profile measured +0.06, and a ring of three or more
cards on one device measured −0.32 — so the ring is still *named* as evidence, because
policy R6 requires the shared element to be identified, but it does not move the probability.

The bigger finding is what could not be measured. **Every one of the 900 cleared cases
scored 0.70 or higher on the bank's model.** The bank never opened a "cleared" investigation
on a quiet transaction, so below 0.70 there is no negative class at all. Taken at face value
the data says a risk score under 0.30 is 570× more likely on fraud — which is not a fact
about fraud, it is a fact about which alerts got investigated. Using it would mark every
quiet transaction in the book as fraud. It is dropped, and the two bands where both classes
are present (0.70–0.85 at −0.40, ≥0.85 at −2.12, damped to −0.80) are the only ones used.
The same selection pressure inflates the new-device signal, so that one is damped too.

## Not inventing the customer's answer

The dataset ships no customer or analyst replies, and the agent is allowed to ask. The easy
version assumes the answer that matches its own suspicion — which adds no information and
only manufactures confidence. Here the assumed reply is derived from a feature the
cardholder's answer would actually turn on: a subscription cadence (same amount, same
product code, three or more months, ~30 days apart) implies a confirmation; two or more
independent incriminating findings imply a denial; and when nothing independent points
either way, the agent assumes **no reply**, which policy R4 already covers. Simulated
replies carry ±1.20 rather than the ±1.9/−2.4 a real answer would justify, and every
assumption is written into `evidence_requests` in full.

## Policy as code

`agent/policy.py` implements Fraud Policy v1.0 directly: the fourteen action identifiers,
the `auto` / `L1` / `L2` routing table, and rules R1–R10. The LLM never picks an action, a
route, a verdict or a probability — those are deterministic and reproducible. The SAR test
(policy 3a) was checked against the history: of 4,665 confirmed cases, every single one with
exposure over $1,000 was reported, and the only four reported below it were the undocumented
ones with connected cards.

`agent/llm.py` is confined to prose — the case summary and the SAR narrative — and every
rewrite is checked afterwards. If it drops or invents an ID or a dollar amount, it is
discarded and the deterministic draft stands.

## Layout

```
prep/        to_parquet.py  derive.py  calibrate.py  validate_card_id.py
             rings.py  build_corpus.py
graph/       schema.gsql  queries.gsql  load.py
agent/       features.py  patterns.py  policy.py  episode.py  investigate.py
             answer.py  backend.py  mcp_backend.py  retrieve.py  llm.py  tg.py
cases/       HHG-001.json … HHG-020.json
monitoring/  MON-001.json … MON-005.json  index.md
ui/          build.py -> dashboard.html
run.py       monitor.py  validate.py  .mcp.json
```

Three modules carry a runnable self-check rather than a test suite:
`uv run python agent/retrieve.py` asserts that every policy situation retrieves its own
rule, `uv run python agent/mcp_backend.py` checks the MCP envelope parser, and
`uv run python prep/validate_card_id.py` asserts the card-ID derivation.

`validate.py` checks all twenty answers against the spec: every required field present,
every enum legal, every ID present in the dataset, every approval route matching the policy
table at that exposure, `sar.file` agreeing with whether `FILE_REPORT` is in the final
actions, and no action set that both closes an alert and blocks the card.
