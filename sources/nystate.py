#!/usr/bin/env python3
"""
sources/nystate.py — New York State legislation via the NY Senate Open
Legislation API (https://legislation.nysenate.gov).

The API is free but requires a key (register at
https://legislation.nysenate.gov/public/subscribe). Without a key every method
raises NeedsKey, which the UI turns into a friendly prompt.

Scope note: the "session year" is the odd year that opens a two-year session
(2025 covers 2025-2026). Bills carry a print number like "S1234" (Senate) or
"A5678" (Assembly).

Everything degrades gracefully — network errors are retried, and any failure
returns empty rather than crashing the app.
"""

import time

try:
    import requests
except ImportError:
    requests = None

API_BASE = "https://legislation.nysenate.gov/api/3"


class NeedsKey(RuntimeError):
    """Raised when a call needs an Open Legislation API key that isn't set."""


def session_year(cal_year):
    """The two-year session opens in the odd year; 2026 -> 2025."""
    y = int(cal_year)
    return y if y % 2 == 1 else y - 1


class NYStateClient:
    def __init__(self, api_key=None, pause=0.0):
        self.key = (api_key or "").strip() or None
        self.pause = pause
        self.s = requests.Session() if requests else None

    @property
    def ready(self):
        return bool(self.key) and self.s is not None

    def _get(self, path, params=None):
        if not self.key:
            raise NeedsKey("NY State Open Legislation API key not set")
        if not self.s:
            raise RuntimeError("requests is not available")
        params = dict(params or {})
        params["key"] = self.key
        last = None
        for attempt in range(4):
            try:
                r = self.s.get(f"{API_BASE}/{path}", params=params, timeout=60)
            except requests.exceptions.RequestException as e:  # type: ignore
                last = e
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code == 200:
                if self.pause:
                    time.sleep(self.pause)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 10))
                continue
            r.raise_for_status()
        if last:
            raise last
        raise RuntimeError("NY State API request failed")

    # ---- bills -----------------------------------------------------------
    def search_bills(self, term, year=None, limit=50):
        """Full-text search of bills for a session year. Returns normalized rows."""
        yr = session_year(year or 2025)
        try:
            data = self._get(f"bills/{yr}/search", {"term": term, "limit": limit})
        except NeedsKey:
            raise
        except Exception:
            return []
        items = (((data or {}).get("result") or {}).get("items")) or []
        out = []
        for it in items:
            b = it.get("result", it) if isinstance(it, dict) else {}
            out.append(normalize_bill(b))
        return [r for r in out if r]

    def bill(self, print_no, year=None):
        yr = session_year(year or 2025)
        try:
            data = self._get(f"bills/{yr}/{print_no}", {"view": "with_refs"})
        except NeedsKey:
            raise
        except Exception:
            return None
        return normalize_bill((data or {}).get("result") or {}, full=True)

    def members(self, year=None, chamber=None, limit=250):
        """Sitting members for a session year; optional chamber SENATE/ASSEMBLY."""
        yr = session_year(year or 2025)
        try:
            data = self._get(f"members/{yr}", {"limit": limit, "full": "true"})
        except NeedsKey:
            raise
        except Exception:
            return []
        items = (((data or {}).get("result") or {}).get("items")) or []
        out = []
        for m in items:
            ch = (m.get("chamber") or "").upper()
            if chamber and ch != chamber.upper():
                continue
            person = m.get("person") or {}
            out.append({
                "name": person.get("fullName") or m.get("fullName") or "",
                "chamber": "State Senate" if ch == "SENATE" else "State Assembly",
                "district": m.get("districtCode"),
                "party": m.get("party") or "",
                "short": m.get("shortName") or "",
                "incumbent": m.get("incumbent", True),
            })
        return [m for m in out if m["name"]]


def normalize_bill(b, full=False):
    if not isinstance(b, dict) or not b.get("printNo"):
        return None
    sponsor = ((b.get("sponsor") or {}).get("member") or {}).get("fullName", "")
    status = b.get("status") or {}
    amendments = b.get("amendments") or {}
    row = {
        "level": "NY State",
        "File": b.get("printNo", ""),
        "Type": "Senate bill" if (b.get("billType") or {}).get("chamber") == "SENATE"
                else "Assembly bill",
        "Title": (b.get("title") or "").strip(),
        "Summary": (b.get("summary") or "").strip(),
        "Sponsor": sponsor,
        "Status": (status.get("statusDesc") or status.get("actionDate") or "").strip(),
        "Committee": status.get("committeeName") or "",
        "Session": b.get("session"),
        "Web Link": f"https://www.nysenate.gov/legislation/bills/"
                    f"{b.get('session','')}/{b.get('basePrintNo', b.get('printNo',''))}",
    }
    if full:
        active = amendments.get("items", {}).get(b.get("activeVersion", ""), {}) \
            if isinstance(amendments.get("items"), dict) else {}
        row["FullText"] = (active.get("fullText") or "").strip()
        row["Cosponsors"] = [m.get("fullName", "") for m in
                             (active.get("coSponsors", {}) or {}).get("items", [])]
        row["Votes"] = bill_votes(b)
    return row


def bill_votes(b):
    """Extract floor/committee roll-calls from a full NY State bill object.

    Returns a list of {date, type, description, tally:{AYE,NAY,...}, members:[{name,vote}]}.
    """
    out = []
    votes = ((b or {}).get("votes") or {}).get("items") or []
    for v in votes:
        mv = (v.get("memberVotes") or {}).get("items") or {}
        tally, members = {}, []
        for code, bucket in mv.items():
            names = [m.get("fullName", "") for m in (bucket.get("items") or [])]
            tally[code.title()] = len(names)
            for nm in names:
                if nm:
                    members.append({"name": nm, "vote": code.title()})
        out.append({
            "date": (v.get("voteDate") or "")[:10],
            "type": v.get("voteType", ""),
            "description": (v.get("committee") or {}).get("name", "") or "Floor vote",
            "tally": tally, "members": members,
        })
    return out
