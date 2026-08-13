#!/usr/bin/env python3
"""
whip.py — the whip operation: contacts, counts, chances, and outreach.

Backs the Whip Room. Loads the Legislative-Director roster (data/ld_contacts.json,
imported from the office's spreadsheet and replaceable in-app via CSV), tracks a
per-bill whip board whose statuses persist in the durable store, does the vote
math (26 = majority, 34 = veto-proof of 51), estimates positive/negative response
chances by fusing the sign-on predictor with the recorded status, and builds the
outreach payloads (per-office and bulk mailto links, CSV) for a message blast.

The app never sends email itself — it prepares prefilled drafts (mailto/BCC) and
exports, so a human always pulls the trigger. Pure functions; no Streamlit.
"""

import csv
import io
import json
import os
from urllib.parse import quote

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ld_contacts.json")

MAJORITY = 26
VETO_PROOF = 34
SEATS = 51

STATUSES = ["signed_on", "committed", "leaning_yes", "undecided", "circle_back",
            "no_response", "leaning_no", "opposed", "not_contacted"]
STATUS_LABELS = {
    "signed_on": "✅ Signed on", "committed": "🤝 Committed", "leaning_yes": "🙂 Leaning yes",
    "undecided": "😐 Undecided", "circle_back": "🔁 Circle back", "no_response": "📵 No response",
    "leaning_no": "🙁 Leaning no", "opposed": "❌ Opposed", "not_contacted": "⬜ Not contacted",
}

# How a recorded status conditions the predicted chance of a POSITIVE response.
# None = fall through to the sign-on model's score for that member.
_STATUS_CHANCE = {"signed_on": 100, "committed": 95, "opposed": 5, "leaning_no": 25,
                  "leaning_yes": None, "undecided": None, "circle_back": None,
                  "no_response": None, "not_contacted": None}


def load_contacts(path=DATA_PATH):
    """The LD roster as a list of dicts (empty list if the file is missing/bad)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return list(data.get("contacts", []))
    except Exception:
        return []


def parse_roster_csv(text):
    """Parse an uploaded roster CSV into contact dicts (for replacing/merging the
    built-in roster — e.g. the second office spreadsheet). Column names are matched
    loosely: district, cm, leg_staffer/leg director, leg_email/email, etc."""
    out = []
    try:
        rdr = csv.DictReader(io.StringIO(text))
    except Exception:
        return out
    def pick(row, *keys):
        low = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        for k in keys:
            for cand, val in low.items():
                if k in cand and val:
                    return val
        return ""
    for row in rdr:
        d = pick(row, "district")
        cm = pick(row, "cm", "member", "council")
        if not cm and not d:
            continue
        try:
            dnum = int("".join(ch for ch in d if ch.isdigit())) if d else None
        except ValueError:
            dnum = None
        out.append({
            "district": dnum, "cm": cm, "cm_email": pick(row, "cm_email", "cm e"),
            "chief_of_staff": pick(row, "chief"),
            "leg_staffer": pick(row, "leg staffer", "leg_staffer", "legislative director", "leg director", "ld"),
            "leg_email": pick(row, "leg_email", "leg email", "ld email") or pick(row, "email"),
            "budget_staffer": pick(row, "budget staffer", "budget_staffer"),
            "budget_email": pick(row, "budget_email", "budget email"),
            "comms_staffer": pick(row, "comms"),
            "comms_email": pick(row, "comms_email", "comms email"),
            "status": "not_contacted", "note": pick(row, "note", "status", "comment"),
        })
    return out


def merge_statuses(contacts, saved):
    """Overlay per-bill saved {district: {status, note}} onto the roster."""
    saved = saved or {}
    out = []
    for c in contacts:
        d = dict(c)
        ov = saved.get(str(c.get("district"))) or saved.get(c.get("district")) or {}
        if ov.get("status"):
            d["status"] = ov["status"]
        if ov.get("note"):
            d["note"] = ov["note"]
        out.append(d)
    return out


def tally(contacts):
    """Whip math over the board."""
    counts = {s: 0 for s in STATUSES}
    for c in contacts:
        counts[c.get("status") if c.get("status") in counts else "not_contacted"] += 1
    have = counts["signed_on"] + counts["committed"]
    soft = have + counts["leaning_yes"]
    return {"counts": counts, "have": have, "soft": soft,
            "need_majority": max(0, MAJORITY - have), "need_veto": max(0, VETO_PROOF - have),
            "majority": MAJORITY, "veto_proof": VETO_PROOF, "seats": SEATS}


def response_chances(contacts, pred_scores):
    """Positive/negative response chance per office.

    Fuses the recorded whip status with the multi-factor sign-on score
    (pred_scores: {cm_lastname_lower: 0-100}). Hard statuses pin the chance;
    soft/unknown statuses fall back to the model, nudged by status. Transparent:
    each row reports which source drove it.
    """
    out = []
    for c in contacts:
        last = (c.get("cm") or "").split()[-1].lower()
        model = pred_scores.get(last)
        pinned = _STATUS_CHANCE.get(c.get("status"), None)
        if pinned is not None:
            pos, src = pinned, "status"
        elif model is not None:
            pos = model
            if c.get("status") == "leaning_yes":
                pos = max(pos, 70)
            elif c.get("status") == "no_response":
                pos = round(pos * 0.8)
            pos, src = min(100, max(0, pos)), "model+status"
        else:
            pos, src = 50, "unknown"
        out.append({**c, "positive_chance": pos, "negative_chance": 100 - pos, "chance_source": src})
    return out


def expected_yes(chanced):
    """Expected final yes count = sum of positive chances (a labeled estimate)."""
    return round(sum(c["positive_chance"] for c in chanced) / 100.0, 1)


# ---------------------------------------------------------------------------
# Outreach — prefilled drafts; a human sends them.
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATE = """Hi {staffer},

I'm reaching out from Council Member Hanks's office about {bill} — {title}.

{pitch}

Could you let us know where CM {cm} stands, and whether the Member would consider signing on as a co-sponsor? Happy to send the bill text, our one-pager, or set up a quick call.

Thank you!
"""


def render_message(template, contact, bill="", title="", pitch=""):
    staffer = (contact.get("leg_staffer") or contact.get("chief_of_staff") or "there").split(";")[0].split(" and ")[0].strip()
    first = staffer.split()[0] if staffer and staffer != "there" else "there"
    try:
        return template.format(staffer=first, cm=contact.get("cm", ""), bill=bill,
                               title=title, pitch=pitch, district=contact.get("district", ""))
    except (KeyError, IndexError):
        return template


def contact_email(contact, role="leg"):
    """Best email for an office: requested role, falling back sensibly."""
    order = {"leg": ["leg_email", "budget_email", "comms_email", "cm_email"],
             "budget": ["budget_email", "leg_email", "cm_email"],
             "comms": ["comms_email", "leg_email", "cm_email"],
             "cm": ["cm_email", "leg_email"]}[role if role in ("leg", "budget", "comms", "cm") else "leg"]
    for k in order:
        v = (contact.get(k) or "").strip()
        if v:
            return v.split(";")[0].strip().lower()
    return ""


def mailto(email, subject, body):
    return f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"


def bulk_mailto(emails, subject, body):
    """One draft BCC'd to every target office."""
    bcc = ",".join(sorted({e for e in emails if e}))
    return f"mailto:?bcc={bcc}&subject={quote(subject)}&body={quote(body)}"


def outreach_rows(contacts, template, bill="", title="", pitch="", role="leg"):
    """Per-office outreach payload: recipient, personalized message, mailto link."""
    rows = []
    subject = f"Co-sponsorship request: {bill}" if bill else "Co-sponsorship request"
    for c in contacts:
        email = contact_email(c, role)
        if not email:
            continue
        msg = render_message(template, c, bill=bill, title=title, pitch=pitch)
        rows.append({"district": c.get("district"), "cm": c.get("cm"),
                     "recipient": (c.get("leg_staffer") or c.get("chief_of_staff") or ""),
                     "email": email, "status": c.get("status"), "message": msg,
                     "mailto": mailto(email, subject, msg)})
    return rows
