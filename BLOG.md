# Measuring a fraud agent instead of trusting it

*Building an agentic fraud investigator on TigerGraph, and the four times the data told me I was wrong.*

---

The brief was to build an agent that investigates fraud and recommends a next best
action when the signals are uncertain. The dataset — 590,742 IEEE-CIS / Vesta card
transactions over six months, 5,565 closed investigations, a written fraud policy and
twenty benchmark cases — has one property that changes how you have to build:
**there is no `isFraud` flag.** Every transaction carries the bank's own model score
and nothing else.

That absence is the whole problem. With a label you train a classifier. Without one you
have to decide what counts as evidence, and then you have to justify that decision to
someone who can be sued over it. Which, it turns out, is what a fraud analyst does all
day.

## What I built

A trigger-driven investigation loop that runs entirely on graph traversals and a policy
engine, with an LLM confined to writing prose.

```
trigger ─▶ investigate ─▶ assess ─▶ request evidence ─▶ re-assess ─▶ act ─▶ explain ─▶ remember
```

Nine or ten graph and retrieval calls per case, 1.5 seconds of wall clock across all
twenty, ~1,170 LLM tokens each. The output is the spec's answer file: the case, the SAR
when policy requires one, and both the before-evidence and after-evidence action sets
with their approval routes.

Twenty cases: 8 fraud, 7 uncertain, 5 legitimate. Two SARs. 67 prior closed cases
retrieved and cited as memory. Ten of the twenty changed their recommendation after
requesting evidence.

## The architecture

Four layers, and the boundary between them is the point.

**Graph tools.** Twelve installed GSQL queries *are* the agent's tool surface —
`card_window`, `device_neighbors`, `prior_cases_for_device`, `ring_component`,
`doc_search`, `write_case` and the rest. Not "the agent can write Cypher": twelve named,
parameterised, installed queries with a fixed shape.

**Three backends behind one interface.** `tigergraph` runs the installed queries over
pyTigerGraph. `mcp` runs the *same* installed queries through the TigerGraph MCP server.
`duckdb` runs the same traversals in SQL so the whole pipeline is runnable and testable
without a workspace. The MCP backend turned out to be twenty lines of real code, because
once your tools are installed GSQL the entire surface maps onto one MCP tool,
`tigergraph__run_installed_query`, whose arguments are literally
`runInstalledQuery(name, params)`. So `MCPBackend` subclasses `TigerGraphBackend` and
swaps the connection for a shim. The response-shaping code — the part that actually has
bugs in it — is written once.

**Policy as code.** `agent/policy.py` is Fraud Policy v1.0 compiled: fourteen action
identifiers, the `auto`/`L1`/`L2` routing table, rules R1–R10. `route_for()` is the only
place an approval route is decided, so there is exactly one line to audit.

**The LLM writes prose and nothing else.** It never picks an action, a route, a verdict
or a probability. It rewrites a deterministic draft of the case summary and the SAR
narrative, and every rewrite is checked afterwards: if it drops or invents an ID or a
dollar amount, it is discarded and the draft stands.

## How TigerGraph is used

The schema is the obvious one — Customer, Card, Transaction, DeviceProfile, EmailDomain,
BillingRegion — plus two that matter more: `ClosedCase` for the bank's finished
investigations, and `Case` for the ones this agent opens.

The multi-hop queries do work a row store cannot. `device_neighbors` walks
DeviceProfile ← Transaction ← Card to find every other cardholder who used one machine.
`prior_cases_for_device` reaches a closed investigation *through* the transactions that
shared a device — which is how the agent discovers that this exact handset already
produced a confirmed fraud case in August.

`write_case` closes the memory loop. Each investigation becomes a `Case` vertex with
edges to its transactions, its connected cards, its device profiles and the prior cases
it cited. A case that names a device becomes evidence for the next analyst who touches
that device.

### GraphRAG, both halves

The graph answers *what happened*. It cannot answer *what the bank is allowed to do
about it*, and that is half of a next-best-action.

So three corpora are chunked and embedded with `BAAI/bge-small-en-v1.5` and loaded into
TigerGraph as `DocChunk` vertices with a 384-dimension `COSINE` vector attribute: the
bank's fraud policy (split so a rule is never cut in half), the five documented
typologies, and six FinCEN SAR and identity-theft PDFs. `doc_search` retrieves them with
`vectorSearch`.

Two design decisions here are load-bearing:

**Retrieval happens before the action decision, not after it.** The pattern is
classified, then the situation — described in graph language — has to *find* the rule
that governs it, and only then are actions chosen. FinCEN's narrative guidance is
retrieved when the filing decision is settled, before a word of the narrative is
written. Retrieving the rule afterwards would be post-hoc justification dressed as
grounding.

**Document evidence carries zero weight.** A policy rule is not evidence that this
cardholder committed fraud; it is the rule the recommendation has to satisfy. Letting it
move the probability would be counting the bank's own instructions as proof. Forty-five
document-sourced claims across twenty cases, every one of them at weight zero.

## The four times the data told me I was wrong

This is the part I did not expect.

Signal weights are log-likelihood ratios measured on the 4,665 confirmed and 900 cleared
closed cases, not chosen by taste. Measuring them **flipped the sign on four signals**
that intuition had backwards:

| signal | I assumed | measured |
|---|---:|---:|
| corroborated new device | +1.0 | −1.85 |
| proxy / anonymised IP | +1.2 | −1.39 |
| 4× amount outlier | +0.6 | −0.97 |
| out-of-character channel | +0.8 | −0.99 |

All four are things a fraud blog post will tell you are fraud indicators. On this book
they are *exculpatory*, because the bank's model already fires on them, so the alerts
that survive review and get cleared are disproportionately made of them.

### The trap underneath that

Here is the finding that changed the design. **Every one of the 900 cleared cases scored
0.70 or higher on the bank's model.** The bank never opened a "cleared" investigation on
a quiet transaction.

Below 0.70 there is no negative class. None. Taken at face value the data says a risk
score under 0.30 is 570× more likely on fraud than on a cleared case — which is not a
fact about fraud, it is a fact about *which alerts got investigated*. Use it and you mark
every quiet transaction in the book as fraud.

So that band is dropped entirely, the two bands where both classes are present are used,
and the signals that correlate with alert *selection* rather than with fraud are damped
rather than fitted. The measured −2.12 for a high score is used at −0.80.

That is the difference between a model that scores well on the closed cases and an agent
that works on the next one.

### And a negative result I kept

`connected_cards` is one hop: who else touched this handset. A ring is transitive —
A shares a phone with B, B shares a different phone with C, and one hop never reaches C.
So I added connected components over the device-sharing graph.

It percolates:

| device cap | cards | components | largest | log-LR of the largest |
|---:|---:|---:|---:|---:|
| 3 | 1,483 | 113 | 1,229 | +0.35 |
| 5 | 2,051 | 73 | 1,888 | +0.22 |
| 8 | 2,616 | 52 | 2,500 | +0.14 |
| 20 | 3,584 | 37 | 3,504 | +0.07 |

At every threshold one giant component swallows the population, and its separation
decays as it grows. Membership is not "this card is in a ring", it is "this card has
ever shared a browser fingerprint", which describes most of the book.

The easy move is to delete the feature and never mention it. Instead the component is
reported as evidence — policy R6 requires the shared element to be named — and given
zero weight, with the sweep table in the source so the next person does not redo it.

The bounded tail is where the algorithm earns its keep: components of 4–10 cards run 15
confirmed fraud against 2 cleared, and their members feed `MONITOR_CONNECTED_CARDS` even
where no single device links the cards directly. The giant component never does.
Monitoring 2,500 cards is not an action, it is a denial of service on the fraud desk.

## Agentic capabilities

**Uncertainty is a first-class verdict.** Seven of twenty cases land `uncertain`, and
they are not failures. The policy has a rule for exactly that state (R8: escalate when
uncertain and exposed), and the stop reason says so explicitly: the remaining uncertainty
is the cardholder's own intent, which only the cardholder or an analyst can resolve, so
the recommended actions route it to them rather than resolving it in the graph.

**Not inventing the customer's answer.** The agent is allowed to ask the cardholder, and
the dataset ships no replies. The easy version assumes the answer that matches its own
suspicion — which adds no information and manufactures confidence out of a prior it
already had.

Here the assumed reply is derived from a feature the cardholder's answer would actually
turn on. A subscription cadence — same amount, same product code, three or more distinct
months, roughly thirty days apart — implies a confirmation. Two or more independent
incriminating findings imply a denial. And when nothing independent points either way,
the agent assumes **no reply at all**, which is the case policy R4 was written for.
Simulated replies carry ±1.20 rather than the ±1.9/−2.4 a real answer would justify, and
every assumption is written out in full in `evidence_requests`.

**Permissions are structural.** The agent executes `auto` actions and recommends `L1`
and `L2` ones with the route stated. A `BLOCK_CARD` at $2,400 exposure routes to L1; the
same action at $2,600 routes to L2. One function decides this for every action in the
system.

**Cases nobody asked for.** All twenty benchmark cases arrive from a trigger somebody
else pulled. `monitor.py` ranks the bounded ring components by money moved in the exam
window, discards any touching a benchmark card, and runs the same investigation and the
same policy engine over each. The top five move $6.9k–$17.5k apiece; one files a SAR. A
fraud agent that only answers the doorbell misses everything nobody thought to flag.

## What I learned

**Measure the weights, then distrust the measurement.** Getting the log-likelihood ratios
off the closed cases was an afternoon. Working out *why* four of them had the wrong sign,
and which ones were artefacts of how the sample was selected rather than facts about
fraud, was most of the project — and it is the only reason the numbers should be believed.

**A selection-biased label set is worse than no labels,** because it is confidently
wrong in a direction you cannot see from the validation score. The 570× risk-score signal
would have looked *wonderful* in cross-validation.

**Installed queries make an agent tractable.** Twelve named parameterised queries are a
contract. The agent cannot drift, the routing is auditable, and swapping pyTigerGraph for
MCP became a twenty-line shim instead of a rewrite.

**Keep the negative results in the source.** Two of the most useful comments in this
codebase say "this was measured at −0.32, so it is named but not weighted, and here is
the sweep".

## What I would improve with more time

- **Retrieve the rule as a constraint, not just as context.** Right now the policy passage
  grounds the explanation while a hand-written implementation of the same rule picks the
  action. Verifying the chosen action set *against* the retrieved text — and flagging a
  divergence — would make the policy engine self-checking.
- **Calibration curves, not point weights.** The weights are per-band log-LRs. A proper
  isotonic fit with confidence intervals would let the agent say how sure it is about how
  sure it is, which is exactly what the `uncertain` band needs.
- **Real merchant data.** Policy R7 is "same merchant, same amount, monthly" and there is
  no merchant column, so product code stands in and cadence does the work. It holds up,
  but it is the weakest joint in the system.
- **A learning memory, not just a retrieved one.** Cases are written back and retrieved,
  but outcomes never update the weights. Closing the loop — re-measuring the LRs as the
  agent's own cases resolve — is the difference between memory and learning.

---

*Code: the twelve GSQL queries, the policy engine, the calibration harness and all twenty
answer files are in the repository. Built for the TigerGraph × Hacker House Goa agentic
fraud investigation challenge.*
