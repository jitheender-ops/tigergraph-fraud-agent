"""The one evidence source that is not the bank's own.

Everything else the agent reads -- transactions, devices, closed cases, the policy --
belongs to the institution. This is the outside: what a fraud desk buys from a vendor.

The transaction file gives an email domain and nothing more. Who operates that domain,
what it costs to open an account there, and whether a billing relationship stands behind
it are facts about the world, not facts about the transaction, and they separate the
closed cases cleanly:

    class              fraud  cleared   log-LR
    isp_tied             135       94    -1.24   a paid subscription and a credit check
    masked               171      139    -1.39   the processor withheld the address
    free_webmail       3,667      573    +0.26   the default for real people and fraudsters
    privacy_or_niche      38        3    +0.80   weak or no identity checks

Served from data/external/email_domain_intel.json rather than fetched, so the agent runs
offline and a demo is reproducible -- the lookup is shaped like the API call it stands in
for, and is counted as a tool call like every other evidence request.
"""
from __future__ import annotations
import json, pathlib

_INTEL: dict[str, str] | None = None
PATH = pathlib.Path("data/external/email_domain_intel.json")

# Measured on the 5,565 closed cases, damped where the base is thin -- the same treatment
# every other weight in patterns.py gets, for the same reason.
CLASS_WEIGHT = {
    "isp_tied":         -1.24,   # measured
    "masked":           -1.39,   # measured
    "privacy_or_niche":  0.40,   # measured +0.80 on 38 fraud and 3 cleared; halved
    "free_webmail":      0.00,   # measured +0.26, and it describes most of the book
    "unknown":           0.00,
}

CLASS_PROSE = {
    "isp_tied": ("an address tied to a paid internet, cable or phone subscription, which "
                 "means a billing relationship and a credit check stand behind it"),
    "masked": ("a masked address -- the processor withheld it rather than the cardholder "
               "choosing an anonymous one"),
    "free_webmail": ("a free webmail address, which is what most real cardholders and "
                     "most fraudsters both use"),
    "privacy_or_niche": ("a privacy-focused or small provider with weak or no identity "
                         "checks at signup"),
}


def _intel() -> dict[str, str]:
    global _INTEL
    if _INTEL is None:
        if not PATH.exists():
            _INTEL = {}
        else:
            _INTEL = json.loads(PATH.read_text())["domains"]
    return _INTEL


def lookup(domain: str | None) -> dict:
    """Shaped like the vendor call it stands in for: a domain in, a classification out."""
    if not domain:
        return {"domain": None, "class": "unknown", "weight": 0.0}
    cls = _intel().get(str(domain).strip().lower(), "unknown")
    return {"domain": domain, "class": cls, "weight": CLASS_WEIGHT.get(cls, 0.0)}


def claim(hit: dict) -> str:
    cls = hit["class"]
    if cls == "unknown":
        return (f"External email-domain intelligence has no classification for "
                f"'{hit['domain']}'. Recorded, and scored at nothing.")
    body = CLASS_PROSE[cls]
    if hit["weight"] == 0:
        return (f"External email-domain intelligence classifies '{hit['domain']}' as {body}. "
                f"Measured across the bank's closed cases this is the baseline -- it "
                f"appears on 3,667 confirmed frauds and 573 cleared alarms -- so it is "
                f"named as context and carries no weight.")
    direction = "away from" if hit["weight"] < 0 else "toward"
    return (f"External email-domain intelligence classifies '{hit['domain']}' as {body}. "
            f"Measured against the bank's closed cases that points {direction} fraud "
            f"(log-likelihood ratio {hit['weight']:+.2f}). The domain is in the "
            f"transaction; what kind of domain it is, is not -- that is the one piece of "
            f"evidence here the institution does not own.")


def demo():
    """The classes have to map the way the measurement says they do, or the weight is
    being applied to a domain it was never measured on."""
    for domain, want in [("comcast.net", "isp_tied"), ("icloud.com", "isp_tied"),
                         ("gmail.com", "free_webmail"), ("outlook.com", "free_webmail"),
                         ("anonymous.com", "masked"), ("mail.com", "privacy_or_niche"),
                         ("protonmail.com", "privacy_or_niche"),
                         ("not-a-real-domain.xyz", "unknown"), (None, "unknown")]:
        hit = lookup(domain)
        assert hit["class"] == want, (domain, hit["class"], want)
        assert hit["weight"] == CLASS_WEIGHT[want]
        assert claim(hit)
    assert lookup("GMAIL.COM ")["class"] == "free_webmail", "lookup must be case-insensitive"
    assert lookup("anonymous.com")["weight"] < 0, "a masked address measured exculpatory"
    print(f"external.py: {len(_intel())} domains classified, all checks passed")


if __name__ == "__main__":
    demo()
