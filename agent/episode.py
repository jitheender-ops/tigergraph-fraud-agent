"""Reconstruct the fraud episode around a flagged transaction.

Exposure is defined by policy section 4 as the sum of the absolute amounts of every
transaction in the episode, so which transactions belong is a scored decision, not a
formality. Episodes are grown from the flagged transaction along whichever attribute
the pattern says links them, inside the window the closed history shows that pattern
occupying (spans measured in prep/calibrate.py: CNP ~5h, ATO / new-device ~26h,
out-of-region ~16h, card testing ~246h).
"""
import datetime as dt

WINDOW_HOURS = {
    "card_testing": 168, "account_takeover": 48, "card_not_present_new_device": 48,
    "out_of_region_use": 48, "card_not_present_fraud": 24, "undocumented": 72, "none": 24,
}


def build(backend, card_id, flagged, pattern, f):
    """Return the episode: the flagged transaction plus the ones that share its anomaly."""
    hours = WINDOW_HOURS.get(pattern, 24)
    ts = flagged["ts"]
    lo, hi = ts - dt.timedelta(hours=hours), ts + dt.timedelta(hours=hours)
    win = backend.card_window(card_id, lo, hi)
    if not len(win):
        return _single(flagged)

    keep = []
    for _, r in win.iterrows():
        if int(r.txn_id) == int(flagged["txn_id"]):
            keep.append(r)
            continue
        if pattern == "card_testing":
            # the testing sequence: sub-$5 online auths and the larger purchase after them
            if r.channel == "online" and (r.amount < 5 or r.amount >= 100):
                keep.append(r)
        elif pattern in ("card_not_present_new_device", "undocumented"):
            if r.device_profile and r.device_profile == flagged.get("device_profile"):
                keep.append(r)
        elif pattern == "out_of_region_use":
            if str(r.addr1) == str(flagged.get("addr1")) and f["prior_in_region"] == 0:
                keep.append(r)
        elif pattern == "account_takeover":
            # mixed-channel activity in the window, excluding the card's routine channel
            if r.channel != _dominant_channel(f):
                keep.append(r)
        elif pattern == "card_not_present_fraud":
            if r.channel == "online" and abs((r.ts - ts).total_seconds()) <= 24 * 3600:
                keep.append(r)

    ids = [str(int(r.txn_id)) for r in keep]
    amts = [abs(float(r.amount)) for r in keep]
    times = [r.ts for r in keep]
    span = (max(times) - min(times)).total_seconds() / 3600 if len(times) > 1 else 0.0
    return {"txn_ids": ids, "exposure": round(sum(amts), 2),
            "first_txn_id": str(int(min(keep, key=lambda r: r.ts).txn_id)),
            "span_h": span, "t0": min(times), "t1": max(times)}


def _dominant_channel(f):
    return "online" if (f.get("online_share") or 0) >= 0.5 else "in_person"


def _single(flagged):
    return {"txn_ids": [str(int(flagged["txn_id"]))], "exposure": round(abs(float(flagged["amount"])), 2),
            "first_txn_id": str(int(flagged["txn_id"])), "span_h": 0.0,
            "t0": flagged["ts"], "t1": flagged["ts"]}
