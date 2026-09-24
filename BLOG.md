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
engine, with an LLM that plans which evidence to gather inside the limits the policy
sets, and writes the prose.

```
trigger ─▶ investigate ─▶ assess ─▶ request evidence ─▶ re-assess ─▶ act ─▶ explain ─▶ remember
```

About thirteen graph and retrieval calls per case, 11 seconds of wall clock across all
twenty on the live database, ~1,600 LLM tokens each. The output is the spec's answer file: the case, the SAR
when policy requires one, and both the before-evidence and after-evidence action sets
with their approval routes.

Twenty cases: 8 fraud, 7 uncertain, 5 legitimate. Two SARs. 165 distinct closed cases
retrieved and cited as memory, by identity and by resemblance. Nine of the twenty asked
for evidence and eight changed their recommendation on the answer; the other eleven met
the policy 6 bar, or had nothing left to ask, before asking anything.

## The architecture

Four layers, and the boundary between them is the point.

**Graph tools.** Fourteen installed GSQL queries *are* the agent's tool surface —
`card_window`, `device_neighbors`, `prior_cases_for_device`, `ring_component`,
`doc_search`, `write_case` and the rest. Not "the agent can write Cypher": fourteen named,
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

### What the database told me

For most of the build the TigerGraph path was written and unrun — no workspace. Standing
up Community Edition in a container and pointing the pipeline at it was supposed to be a
formality. It found eleven bugs, none of which any amount of re-reading would have
surfaced:

- **`Case` is a reserved GSQL keyword.** The agent's case vertex is `FraudCase`.
- **`upsertVertices` wraps values itself**, so the loader's own `{"value": ...}` made
  every attribute a dict. It only showed on vertex types that *have* attributes — so
  `Customer`, which has none, loaded fine and `Card` was the first failure.
- **REST will not coerce a CSV string into an `INT`.** The loader now reads attribute
  types off the live graph and casts, rather than keeping a hand-written list of numeric
  columns in step with the schema.
- **`concat_ws` skips NULLs.** The packed `M1..M9` match-flag string shifted every later
  flag one position left and was unparseable by index. Explicit empty placeholders fixed
  it — and that one silently corrupted a *signal*, not just a load.
- **`PRINT T[T.amount]` names the attribute `T.amount`.** Every key came back carrying an
  alias prefix the SQL columns do not have.
- **RESTPP returns `DATETIME` as a string**, and callers written against typed frames were
  doing arithmetic on it.
- **`prior_cases_for_card` had no `ORDER BY`.** The agent cites the top eight, so the two
  backends cited *different closed cases* — not a different order, a different set. That
  one would have changed the answers.
- **A `SetAccum`'s order is arbitrary**, so the ring component named a different 25 cards
  out of the same component than the sorted SQL mirror did.
- **`date_diff('day', a, b)` counts day boundaries, not fractional days**, so the
  recurring-charge cadence test disagreed in the last decimal on every card.

The two that matter are the fourth, the seventh and the last: each silently changed what
the agent *concluded*, and each is invisible to anything but a differential test. So the
repo has two now. `prep/parity.py` asserts the feature rows are identical field for field
across backends; `prep/backend_diff.py` asserts the whole answer files are. Both pass
20/20 across all three backends, ignoring the four fields that must differ.

**Policy as code.** `agent/policy.py` is Fraud Policy v1.0 compiled: fourteen action
identifiers, the `auto`/`L1`/`L2` routing table, rules R1–R10. `route_for()` is the only
place an approval route is decided, so there is exactly one line to audit.

**The LLM plans; the policy decides.** It never picks an action, a route, a verdict or a
probability. It does choose which evidence to ask for next: the policy works out which
requests are allowed and whether to stop, and when there is a real choice the model reads
the evidence and picks the most informative one, with its reason written onto the request.
It can only name an option it was offered; anything else falls back to the policy's order,
so a bad answer costs a round, never a rule. It also rewrites the deterministic drafts of
the case summary and SAR narrative, and every rewrite is checked: one that drops or invents
an ID or a dollar amount is discarded and the draft stands.

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

`device_neighbors` is one hop: who else touched this handset. A ring is transitive —
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

The table is not only my implementation's word for it. TigerGraph's own algorithm library
computes the same partition: `tg_wcc`, installed unmodified from `gsql-graph-algorithms`,
run over `RING_DEVICE` edges that join each card to every ring-grade device profile it
used. `prep/ring_parity.py` asserts the two agree component for component — 2,616 cards
in 52 components, the cap-8 row above. The per-case query expands just the seed card's
component, which is what an investigation needs; the library runs the whole graph at once.

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
same policy engine over each. The top five move $6.9k–$17.5k apiece; four file a SAR. A
fraud agent that only answers the doorbell misses everything nobody thought to flag.

## Letting a human argue with it

An agent that only files a verdict is a report generator. The console is where an analyst
disagrees, and three of its endpoints **re-run the investigation** rather than editing its
output.

The blueprint I started from asked the LLM to "re-evaluate the evidence and generate a
new risk score". That would have destroyed the one property the whole thing rests on, so
it does something else. The analyst's objection *withdraws the premise it names*:

```
"ignore the out-of-region flag, the customer is on holiday"
  → withdrawn: region_new_travel
  → probability   0.05 → 0.17        (the same log-odds sum, one term short)
  → actions       ALLOW, CLOSE → ALLOW, CLOSE, MONITOR_CARD
```

The model's only job is mapping free text onto a signal *name* — a classification, not a
judgement — and its answer is intersected with the signals really present, so it can only
choose among them. An answer naming more than two is discarded as not having understood
the question. The withdrawn claims stay in the case file marked WITHDRAWN BY ANALYST,
because a case that quietly loses evidence is not auditable. And the steering is
reversible: the first cut let `suppressed` only ever grow, which makes a mis-aimed
challenge a trap rather than a control.

The console also had to show the arithmetic, because a probability you cannot decompose
is a probability nobody can argue with. The answer spec fixes `evidence` at
claim/source/ref/entity_ids — no room for a signal's name or weight — so the API returns
those separately rather than bending the spec to fit a screen, and a waterfall draws each
contribution and the running probability after it. Green bars are the exculpatory
signals. On a card-testing case it walks 0.27 → 0.18 → 0.29 → … → 0.90, and after a
challenge you watch the bar that was carrying the case disappear.

Two other things were being rendered as text that are really shapes: the ego-network the
investigation walked (card → device → sibling cards → the closed cases those reach), and
the episode itself — card testing is three authorisations under $5 and then a purchase,
which is something you see, not something you read off 25 identifiers.

Closing a case writes a `ClosedCase` vertex, and that is the memory loop rather than a
metaphor: `prior_cases_for_card` and `prior_cases_for_device` read `ClosedCase`, so the
next investigation touching that card or device retrieves what the analyst just decided.
Blacklisting a device profile attaches the case to the transactions that ran on it —
verified end to end, all four cards that had used one profile now reach a confirmed-fraud
case. Nothing is retrained. The evidence set grows.

## What I learned

**Measure the weights, then distrust the measurement.** Getting the log-likelihood ratios
off the closed cases was an afternoon. Working out *why* four of them had the wrong sign,
and which ones were artefacts of how the sample was selected rather than facts about
fraud, was most of the project — and it is the only reason the numbers should be believed.

**A selection-biased label set is worse than no labels,** because it is confidently
wrong in a direction you cannot see from the validation score. The 570× risk-score signal
would have looked *wonderful* in cross-validation.

**A second implementation is a test, if you diff it.** The DuckDB mirror existed to keep
the pipeline runnable without a workspace. Once both were real, diffing them found
defects in the graph path that no unit test would have been written for — including two
that changed which closed cases the agent cited and what it concluded. Two
implementations of one definition drift the moment nobody checks.

**Installed queries make an agent tractable.** Fourteen named parameterised queries are a
contract. The agent cannot drift, the routing is auditable, and swapping pyTigerGraph for
MCP became a twenty-line shim instead of a rewrite.

**A UI is where you find out whether your explanation is real.** The probability had been
"explained" by a list of claims for weeks. Drawing it as an arithmetic decomposition took
an afternoon and immediately exposed that the API was throwing away the two fields that
do the explaining. It also caught a console that reported writing a `ClosedCase` vertex
while running on the mirror, where there is no graph to write to — the same lie
`written_to_graph` used to tell before the live database made it checkable.

**Keep the negative results in the source.** Two of the most useful comments in this
codebase say "this was measured at −0.32, so it is named but not weighted, and here is
the sweep".

## Grading it like a judge, then fixing what the grade found

I scored the finished build as a judge would and gave it 7.5. The low marks were not
where I expected, and three of them were bugs dressed up as design.

**The stop rule was a caption.** Policy 6 says when to stop, and the agent quoted it in
`stop_reason` — after asking for exactly one round of evidence, whatever the numbers said.
Evidence gathering is now a loop with policy 6 as its condition: check the bar, ask for the
next thing policy 5 allows (the cardholder, then step-up, then an analyst), re-score,
recompute what the policy wants, repeat. `stop_reason` names the exit actually taken, so it
cannot claim "a verification response settled the question" when nobody answered — which it
had been doing on six of the twenty cases.

**My own step-up simulation pointed the wrong way.** I had made it fail on a New device.
In this data a New device sits on 83% of cleared alerts and 18% of fraud — people buy
phones — so a failed step-up was being scored as fraud evidence off a fact that measures as
innocence. Worse, a *passed* step-up counted as the cardholder confirming the charge, which
on HHG-011 produced `BLOCK_CARD` and `CLOSE_NO_FRAUD` in the same action set. A passcode
proves who holds the phone, not who made a past purchase; it now moves the score and nothing
else, runs off the match flags, and carries half weight because those flags are already
scored.

**The backtest counted the wrong thing.** Adopting measured memory weights took held-out
"false blocks" from 2 to 17, and I nearly reported that as the cost. But the column counted
fraud *verdicts*, and a fraud verdict below 0.85 does not block a card — R1 declines the
pending authorisation and asks first. Running the policy engine on every held-out case
instead: of 144 legitimate cardholders, **one** had a card blocked; sixteen were declined
and asked to verify. The metric had been overstating customer harm seventeen-fold.

**Memory could read the future.** Every lookup cut on the date a case *opened*, but an
outcome is unknown until the case *closes*. The leak was small — cases close in one to five
days — but it had inflated the device-memory weight from +1.32 to +1.67. Every lookup now
cuts on the close date, in both backends, the similarity index, calibration and the
backtest.

**Smaller, and just as real.** A case resting on an unanswered verification was marked
`closed_fraud`; the spec defines that state as `open`. HHG-001 and MON-001 both claimed graph
ID `CASE-2016-001`. The smoke test left a device blacklist in the live graph, which the next
regeneration dutifully cited as confirmed fraud. An approval recorded whatever name the
request body carried. And five of the twenty answer files broke a rule the spec states in
one line — *if you requested nothing, `final` equals `initial`* — because the rules engine's
fallback branch treated "fraud and the cardholder denies it" as uncertain and recommended
asking a cardholder who had just reported the fraud, and because a denial that contradicted
a legitimate verdict was only noticed after the evidence step. The validator never checked
that rule; it does now. Each of these is fixed, and each now has a check.

Then a second agent red-teamed it, and found three ways past the controls; I found six
more by attacking the running API. They shared one shape: **the approval routes guarded
only the harsh actions.** Blocking, declining and filing needed L1 or L2 — but allowing a
transaction, closing a case, recording a "customer confirmed" reply, withdrawing evidence,
closing fraud as cleared and blacklisting a device all ran on a single analyst's token. A
rogue insider only needs the downgrade path. Worse, two exoneration paths were my own
simulation: an assumed passcode success (stolen card details pass every match flag) and an
assumed "the cardholder recognises this subscription" that overruled a cardholder who had
just disputed the charge. An analyst's reply was even being scored as the cardholder's.

The fix is one rule: a human may make the agent harsher alone; making it more lenient
needs a second person, and an assumption may never clear a case. Each exploit is now a
test. Two benchmark cases that had read `legitimate` on an assumed passcode now read
`uncertain` and wait for a real answer — a worse-looking score and a more honest one.

What did not change is worth saying too: the verdict band. The backtest fits the band a cost
function would choose and prints it beside the shipped one. It looks better on October —
under a review cost and a false-block cost I made up. Nobody in this dataset prices either,
so the band stays at the policy's own numbers, and the fit is there for whoever can.

## What I would improve with more time

- **Retrieve the rule as a constraint, not just as context.** Right now the policy passage
  grounds the explanation while a hand-written implementation of the same rule picks the
  action. Verifying the chosen action set *against* the retrieved text — and flagging a
  divergence — would make the policy engine self-checking.
- **Calibration curves, not point weights.** The backtest now prints a reliability table
  and Brier score, and it shows the low end is poorly calibrated against this 89%-fraud
  history (by design: the prior is set for a 50/50 exam set). An isotonic fit with
  confidence intervals is the next step.
- **Real merchant data.** Policy R7 is "same merchant, same amount, monthly" and there is
  no merchant column, so product code stands in and cadence does the work. It holds up,
  but it is the weakest joint in the system.
- **A learning memory, not just a retrieved one.** Cases are written back and retrieved,
  but outcomes never update the weights. Closing the loop — re-measuring the LRs as the
  agent's own cases resolve — is the difference between memory and learning.

---

*Code: the fourteen GSQL queries, the policy engine, the calibration harness and all twenty
answer files are in the repository. Built for the TigerGraph × Hacker House Goa agentic
fraud investigation challenge.*
