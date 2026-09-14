#!/usr/bin/env python3
"""
Where a matter stands, derived from one place.

The corpus carried two booleans, ``enacted`` and ``pending``, set by whichever
ingest touched a row last. After the Legistar mirror began refreshing status
they drifted, and drifted in both directions at once:

    Enacted (Mayor's Desk for Signature)   enacted=0, pending=1  (24 rows)
    Committee                              enacted=0, pending=0  (80 rows)
    Adopted                                enacted=0, pending=1  (10 rows)
    Filed                                  enacted=1             (2 rows)

The sign-on recommender reads those flags. So it offered the Councilmember a
bill already sitting on the Mayor's desk -- there is nothing to sign on to --
while silently withholding eighty bills still in committee, which is where a
sign-on actually matters. Both failures are invisible: one produces advice
that is merely useless, the other produces no advice at all.

Status is what upstream maintains. The flags are our derivation, and a
derivation that disagrees with its source is just a second, worse source. So
stage is computed here, from status, and the booleans are computed from stage.

The vocabulary is Legistar's, written down rather than guessed:

    live      still open to a sponsor   Committee · Laid Over in Committee ·
                                        Introduced · Reported from Committee ·
                                        Companion Pending Approval by Council
    passed    Council has acted         Enacted (Mayor's Desk for Signature)
    law       on the books              Enacted, with a local law number
    adopted   resolution carried        Adopted
    dead      no longer moving          Filed · Filed (End of Session) ·
                                        Withdrawn · Disapproved · Vetoed
"""
from __future__ import annotations

import re
from typing import Any

LIVE = ("committee", "laid over", "introduced", "reported from",
        "companion pending", "hearing", "recessed")
PASSED = ("mayor's desk", "mayors desk", "sent to mayor")
DEAD = ("filed", "withdrawn", "disapproved", "vetoed", "returned unsigned",
        "rejected", "expired")

# The order matters. "Enacted (Mayor's Desk for Signature)" contains both
# "enacted" and "mayor's desk"; the parenthetical is the operative part,
# because the Council is done and the Mayor is not.
ORDER = ("passed", "dead", "law", "adopted", "live")


def of(status: Any, local_law: Any = None, enacted_date: Any = None) -> str:
    """
    One word for where this matter stands.

    Falls back to "unknown" rather than guessing. A matter whose status the
    system does not recognise must not be offered as a sign-on candidate on
    the strength of a default.
    """
    text = str(status or "").strip().lower()
    if not text:
        return "unknown"
    if any(k in text for k in PASSED):
        return "passed"
    if any(text.startswith(k) or f" {k}" in text for k in DEAD):
        return "dead"
    if "enacted" in text or local_law or enacted_date:
        return "law"
    if text.startswith("adopted") or text == "approved":
        return "adopted"
    if any(k in text for k in LIVE):
        return "live"
    return "unknown"


def flags(status: Any, local_law: Any = None,
          enacted_date: Any = None) -> tuple[str, int, int]:
    """(stage, enacted, pending) — the booleans follow the stage, never lead it."""
    st = of(status, local_law, enacted_date)
    return st, (1 if st == "law" else 0), (1 if st == "live" else 0)


def signable(status: Any, local_law: Any = None,
             enacted_date: Any = None) -> bool:
    """
    Can the Councilmember still add her name to this?

    Only a live matter. A bill on the Mayor's desk has passed the Council; a
    filed one is gone; an adopted resolution is finished. Recommending any of
    them wastes the reader's attention on a decision that no longer exists.
    """
    return of(status, local_law, enacted_date) == "live"


def label(stage: str) -> str:
    """How to say it to the Councilmember."""
    return {
        "live": "still in committee — a sign-on is possible",
        "passed": "passed the Council, awaiting the Mayor",
        "law": "on the books",
        "adopted": "adopted",
        "dead": "no longer moving",
        "unknown": "status not recognised — check Legistar",
    }.get(stage, stage)


def backfill(store) -> dict:
    """
    Recompute stage, enacted and pending for every matter.

    Safe to re-run: it derives from status, which upstream owns, so running it
    twice produces the same answer.
    """
    rows = store.q("SELECT matter_id, status, local_law FROM matters")
    updates = []
    counts: dict[str, int] = {}
    for r in rows:
        st, enacted, pending = flags(r["status"], r["local_law"])
        counts[st] = counts.get(st, 0) + 1
        updates.append((st, enacted, pending, r["matter_id"]))
    with store.tx() as c:
        c.executemany(
            "UPDATE matters SET stage = ?, enacted = ?, pending = ? "
            "WHERE matter_id = ?", updates)
    return {"rows": len(updates), "by_stage": dict(sorted(counts.items()))}
