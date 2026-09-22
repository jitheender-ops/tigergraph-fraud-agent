"""LLM synthesis, deliberately confined to prose.

The model never chooses an action, a route, a verdict or a probability: those come from
policy.py and patterns.py so they are reproducible and auditable. It rewrites the
deterministic draft into something an analyst or a regulator would want to read, and
every rewrite is checked afterwards. If the rewrite drops or invents an ID or an amount,
it is discarded and the deterministic text stands.
"""
from __future__ import annotations
import os, re

SUMMARY_SYSTEM = """You are a fraud analyst at a card issuer writing the summary line of an
internal case file. You will be given a factual draft assembled from graph queries.

Rewrite it so a colleague can read it quickly. Rules:
- Two to six sentences. No headings, no bullet points, no preamble.
- Keep every transaction ID, card ID, customer ID, case ID, date, percentage and dollar
  amount exactly as given, and do not drop any. Do not add any that are not in the draft.
- Do not add conclusions the draft does not support, and do not soften or strengthen the
  stated probability.
- Plain past-tense English. No marketing tone."""

SAR_SYSTEM = """You are writing the narrative of a Suspicious Activity Report for a US
financial institution, following FinCEN narrative guidance. You will be given a factual
draft assembled from graph queries.

Rewrite it as a narrative that stands on its own for a regulator who has no other context.
Rules:
- Six to twelve sentences, one paragraph, chronological where possible.
- Cover who, what, when, where, how, and why the activity is suspicious.
- EVERY identifier in the draft (card IDs like C13487-K1, customer IDs, transaction IDs,
  closed-case IDs like CC-0141) must appear in your output, spelled exactly as in the draft.
  Do not summarise a list of IDs as "and others" or "several cards" - write them all out.
  Never invent one that is not in the draft.
- Keep every date and dollar amount exactly as given.
- State plainly what was assumed rather than observed, if the draft says so.
- No headings, no bullet points, no recommendations to the regulator."""

_ID = re.compile(r"\b(?:T?\d{6,}|C\d{4,}(?:-K\d+)?|CC-\d+|CASE-\d{4}-\d+)\b")
_MONEY = re.compile(r"\$[\d,]+(?:\.\d{2})?")


class LLM:
    """Provider-agnostic: anything with an OpenAI-compatible /chat/completions endpoint.

    Set LLM_PROVIDER=sarvam to use Sarvam's API. sarvam-105b is a reasoning model -- it
    spends most of its budget in `reasoning_content` and only then fills `content`, so it
    needs a far larger max_tokens than a non-reasoning model, and a response that stops on
    `length` has a truncated narrative and is rejected in favour of the deterministic draft.
    """

    def __init__(self, model=None):
        from openai import OpenAI
        provider = os.getenv("LLM_PROVIDER", "openai").lower()
        if provider == "sarvam":
            key = os.getenv("SARVAM_API_KEY")
            if not key:
                raise RuntimeError("LLM_PROVIDER=sarvam but SARVAM_API_KEY is not set")
            self.client = OpenAI(api_key=key,
                                 base_url=os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai/v1"))
            # sarvam-105b is a reasoning model: it spends ~13k tokens and 90s thinking
            # before it writes a word, for prose no better than the deterministic draft.
            # The -conversations variant does the same job in ~1.4k tokens and 1s.
            self.model = model or os.getenv("SARVAM_MODEL", "sarvam-105b-conversations")
            self.reasoning = "conversations" not in self.model
        else:
            self.client = OpenAI()
            self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            self.reasoning = False
        self.provider = provider
        self.total_tokens = 0
        self._case_tokens = 0

    def start_case(self):
        self._case_tokens = 0

    def tokens_for_case(self):
        return self._case_tokens

    def _chat(self, system, user, max_tokens=700):
        # a reasoning model needs room for the reasoning AND the answer
        budget = max_tokens * 8 if self.reasoning else max_tokens
        r = self.client.chat.completions.create(
            model=self.model, temperature=0.2, max_tokens=budget,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        used = getattr(r, "usage", None)
        if used:
            self.total_tokens += used.total_tokens
            self._case_tokens += used.total_tokens
        choice = r.choices[0]
        if choice.finish_reason == "length":
            # ran out mid-sentence: a truncated narrative is worse than the draft
            return None
        return (choice.message.content or "").strip() or None

    @staticmethod
    def _faithful(allowed: str, out: str, require: str = "") -> bool:
        """Reject a rewrite that invents an identifier or an amount, or that drops most
        of the ones the draft carried.

        `allowed` is everything the model was shown; `require` is the draft, whose IDs it
        is expected to keep."""
        if not out or len(out) < 40:
            return False
        a_ids, o_ids = set(_ID.findall(allowed)), set(_ID.findall(out))
        if o_ids - a_ids:
            return False           # invented an ID
        if set(_MONEY.findall(out)) - set(_MONEY.findall(allowed)):
            return False           # invented an amount
        r_ids = set(_ID.findall(require))
        if r_ids and len(r_ids - o_ids) > max(2, len(r_ids) // 2):
            return False           # dropped most of the draft's identifiers
        return True

    def polish_summary(self, draft, r):
        # The model is checked against everything it was shown, not against the draft
        # alone: the context is evidence the investigation actually gathered, so drawing
        # on it is correct. Only values in neither are inventions.
        ctx = _ctx(r)
        try:
            out = self._chat(SUMMARY_SYSTEM, ctx + "\n\nDRAFT:\n" + draft, 400)
            return out if self._faithful(ctx + draft, out, require=draft) else None
        except Exception as e:
            print(f"  [llm] summary skipped: {e}")
            return None

    def polish_narrative(self, draft, r, subjects):
        ctx = _ctx(r)
        try:
            user = (ctx + "\nSubjects that must appear: " + ", ".join(map(str, subjects))
                    + "\n\nDRAFT:\n" + draft)
            out = self._chat(SAR_SYSTEM, user, 900)
            return out if self._faithful(ctx + draft, out, require=draft) else None
        except Exception as e:
            print(f"  [llm] narrative skipped: {e}")
            return None


def _ctx(r):
    ev = "\n".join(f"- [{s.source}] {s.claim}" for s in r["signals"])
    return (f"Verdict: {r['verdict']} at probability {r['prob']:.2f}. "
            f"Pattern: {r['pattern']}. Exposure: ${r['exposure']:,.2f}.\n"
            f"Evidence gathered:\n{ev}")
