#!/usr/bin/env python3
"""
sources/congress.py — the U.S. Congress layer, focused on NYC's delegation.

Two data sources, deliberately split by whether they need a key:

  * congress-legislators  (NO KEY) — the well-maintained public dataset of every
    current member of Congress: bioguide id, party, state, district, terms,
    birthday, and social handles. We use it to build "NYC's delegation" — the
    House members whose districts sit in the five boroughs, plus both NY
    senators. Source: https://github.com/unitedstates/congress-legislators

  * Congress.gov API v3  (NEEDS KEY, free at https://api.congress.gov) — used to
    pull what those members are actually sponsoring/cosponsoring right now, and
    bill details. Because the delegation *is* NYC's representation, their
    legislation is inherently the "federal action that affects NYC" the user
    wants tracked.

Everything is defensive: no network -> empty results, no key -> NeedsKey.
"""

import time

try:
    import requests
except ImportError:
    requests = None

LEGISLATORS_URL = ("https://unitedstates.github.io/congress-legislators/"
                   "legislators-current.json")
API_BASE = "https://api.congress.gov/v3"

# NY U.S. House districts that lie wholly or substantially within the five
# boroughs (post-2022 lines). Kept as an editable constant — district lines move
# with redistricting, and a couple (NY-03 in NE Queens/Nassau, NY-16 in the
# north Bronx/Westchester) straddle the city edge, so they're included with that
# caveat. Adjust here if the map changes.
NYC_HOUSE_DISTRICTS = {3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}


class NeedsKey(RuntimeError):
    """Raised when a Congress.gov call needs an API key that isn't set."""


# ---------------------------------------------------------------------------
# congress-legislators (no key)
# ---------------------------------------------------------------------------
def load_legislators(timeout=30):
    """Fetch the current-legislators dataset. Returns [] on any failure."""
    if not requests:
        return []
    last = None
    for attempt in range(3):
        try:
            r = requests.get(LEGISLATORS_URL, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(min(2 ** attempt, 8))
        except requests.exceptions.RequestException as e:  # type: ignore
            last = e
            time.sleep(min(2 ** attempt, 8))
    return []


def _current_term(leg):
    terms = leg.get("terms") or []
    return terms[-1] if terms else {}


def nyc_delegation(legislators, districts=None):
    """Filter the dataset down to NYC's House members + both NY senators.

    Returns normalized profile dicts sorted senators-first, then by district.
    """
    districts = districts if districts is not None else NYC_HOUSE_DISTRICTS
    out = []
    for leg in legislators or []:
        term = _current_term(leg)
        if term.get("state") != "NY":
            continue
        chamber = term.get("type")  # 'sen' or 'rep'
        if chamber == "rep":
            dist = term.get("district")
            if dist not in districts:
                continue
        elif chamber != "sen":
            continue
        out.append(_profile(leg, term))
    out.sort(key=lambda p: (p["chamber"] != "U.S. Senate", p.get("district") or 0))
    return out


def _profile(leg, term):
    name = leg.get("name") or {}
    bio = leg.get("bio") or {}
    ids = leg.get("id") or {}
    social = leg.get("social") or {}
    full = name.get("official_full") or \
        f"{name.get('first','')} {name.get('last','')}".strip()
    return {
        "level": "U.S. Congress",
        "name": full,
        "chamber": "U.S. Senate" if term.get("type") == "sen" else "U.S. House",
        "district": term.get("district"),
        "state": term.get("state"),
        "party": term.get("party") or "",
        "bioguide": ids.get("bioguide") or "",
        "phone": term.get("phone") or "",
        "office": term.get("office") or "",
        "url": term.get("url") or "",
        "since": (term.get("start") or "")[:4],
        "term_end": (term.get("end") or "")[:10],
        "birthday": bio.get("birthday") or "",
        "gender": bio.get("gender") or "",
        "twitter": social.get("twitter") or "",
        "seat": ("Senator" if term.get("type") == "sen"
                 else f"NY-{term.get('district'):02d}" if term.get("district")
                 else "Representative"),
    }


# ---------------------------------------------------------------------------
# Congress.gov API v3 (needs key)
# ---------------------------------------------------------------------------
class CongressClient:
    def __init__(self, api_key=None, pause=0.0):
        self.key = (api_key or "").strip() or None
        self.pause = pause
        self.s = requests.Session() if requests else None

    @property
    def ready(self):
        return bool(self.key) and self.s is not None

    def _get(self, path, params=None):
        if not self.key:
            raise NeedsKey("Congress.gov API key not set")
        if not self.s:
            raise RuntimeError("requests is not available")
        params = dict(params or {})
        params["api_key"] = self.key
        params.setdefault("format", "json")
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
        raise RuntimeError("Congress.gov request failed")

    def member_legislation(self, bioguide, kind="sponsored", limit=25):
        """Bills a member sponsored or cosponsored. kind in {sponsored,cosponsored}."""
        path = f"member/{bioguide}/{kind}-legislation"
        try:
            data = self._get(path, {"limit": limit})
        except NeedsKey:
            raise
        except Exception:
            return []
        key = f"{kind}Legislation"
        items = (data or {}).get(key) or []
        return [_norm_fed_bill(b) for b in items if isinstance(b, dict)]

    def recent_bills(self, congress=119, limit=25):
        try:
            data = self._get(f"bill/{congress}", {"limit": limit, "sort": "updateDate+desc"})
        except NeedsKey:
            raise
        except Exception:
            return []
        return [_norm_fed_bill(b) for b in (data or {}).get("bills", [])]

    def bill_detail(self, congress, bill_type, number):
        try:
            data = self._get(f"bill/{congress}/{str(bill_type).lower()}/{number}")
        except NeedsKey:
            raise
        except Exception:
            return None
        return (data or {}).get("bill")


def _norm_fed_bill(b):
    bt = (b.get("type") or "").upper()
    num = b.get("number") or ""
    congress = b.get("congress") or ""
    latest = b.get("latestAction") or {}
    return {
        "level": "U.S. Congress",
        "File": f"{bt} {num}".strip(),
        "Type": {"HR": "House bill", "S": "Senate bill", "HRES": "House resolution",
                 "SRES": "Senate resolution", "HJRES": "House joint res.",
                 "SJRES": "Senate joint res."}.get(bt, bt or "Bill"),
        "Title": (b.get("title") or "").strip(),
        "Status": (latest.get("text") or "").strip(),
        "Action date": (latest.get("actionDate") or "")[:10],
        "Congress": congress,
        "Web Link": b.get("url", "") or (
            f"https://www.congress.gov/bill/{congress}th-congress/"
            f"{'house-bill' if bt == 'HR' else 'senate-bill'}/{num}" if num else ""),
    }
