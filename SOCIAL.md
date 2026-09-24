# Social post drafts

Pick one, add the blog link, post from your own account. Tag **@TigerGraphDB**.

---

## LinkedIn

Built an agentic fraud investigator on TigerGraph for the Hacker House Goa challenge.
590,742 card transactions, 5,565 closed investigations, a written fraud policy — and no
`isFraud` flag anywhere in the data.

That last part changed everything. With a label you train a classifier. Without one you
have to decide what counts as evidence and then justify it to someone who could be sued
over it.

So I measured the signal weights as log-likelihood ratios against the 5,565 closed cases
instead of picking them. Four of them came back with the *opposite sign* to what every
fraud article will tell you:

→ a corroborated new device: I assumed +1.0, measured −1.85
→ proxy / anonymised IP: assumed +1.2, measured −1.39
→ a 4× amount outlier: assumed +0.6, measured −0.97

Then the finding that actually changed the design. Every one of the 900 cleared cases
scored 0.70 or higher on the bank's model. The bank never opened a "cleared"
investigation on a quiet transaction — so below 0.70 there is no negative class at all.
Taken at face value the data says a low risk score is 570× more likely on fraud. That is
not a fact about fraud. It is a fact about which alerts got investigated, and it would
have looked wonderful in cross-validation.

What shipped:
• 12 installed GSQL queries as the agent's entire tool surface
• three interchangeable backends — pyTigerGraph, TigerGraph MCP, and a local mirror
• GraphRAG on both halves: connected evidence from the graph plus the policy, the
  typologies and FinCEN guidance retrieved *before* the action decision, never after
• the fraud policy compiled to code — the LLM plans which evidence to gather and writes the prose, but never picks an action,
  a route, a verdict or a probability
• connected components over the device-sharing graph, reported and deliberately
  unweighted, because the transitive hull percolates into one giant component at every
  threshold I tested. I kept the negative result in the source with the sweep table.

Seven of twenty cases end `uncertain`. That is not a failure mode — the policy has a
rule for exactly that state, and the agent says plainly that the remaining uncertainty
is the cardholder's own intent, which only the cardholder can resolve.

Write-up: [link]

@TigerGraphDB #TigerGraph #GraphRAG #FraudDetection #AIAgents

---

## X

Built an agentic fraud investigator on @TigerGraphDB.

590k transactions, 5,565 closed cases, no isFraud label.

Measured the signal weights instead of guessing them. Four came back with the opposite
sign to what every fraud article says. 🧵

---

Then the real one: all 900 cleared cases scored ≥0.70 on the bank's model.

Below 0.70 there is NO negative class.

Taken at face value: a low risk score is 570× more likely on fraud. That's not a fact
about fraud — it's a fact about which alerts got investigated.

It would have looked great in CV.

---

What shipped:

• 12 installed GSQL queries as the whole tool surface
• 3 backends: pyTigerGraph, TigerGraph MCP, local mirror
• GraphRAG both halves — graph evidence + policy/FinCEN retrieved BEFORE the decision
• policy as code; the LLM plans evidence requests inside policy limits, never actions
• connected components, reported and deliberately unweighted

---

Why unweighted? Transitive device sharing percolates. One giant component at every
threshold I tried:

cap 3 → largest 1,229 (LR +0.35)
cap 20 → largest 3,504 (LR +0.07)

Membership means "has shared a browser fingerprint", not "is in a ring".

Kept the negative result in the source.

---

7 of 20 cases end `uncertain`. Not a failure — the policy has a rule for that, and the
agent says why: the remaining uncertainty is the cardholder's intent, and only the
cardholder can resolve it. So it routes there instead of guessing.

Full write-up: [link]
