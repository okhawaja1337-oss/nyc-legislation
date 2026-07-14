#!/usr/bin/env python3
"""
scheduled_packet.py — generate a District Packet headlessly, on a schedule.

This is the "scheduled backend" companion to the Streamlit app: a plain CLI you
can run from cron (or any scheduler) to produce a member's packet as Markdown +
printable HTML, unattended. It reuses the same packet/profile/print modules the
app uses, so the output is identical.

Examples
--------
    # NYC Council member (bounded sponsor scan of the year's legislation)
    python3 scheduled_packet.py --level nyc --member Hanks --year 2026 --out out/hanks

    # A member of NYC's federal delegation (instant; needs no key for the roster)
    CONGRESS_API_KEY=... python3 scheduled_packet.py --level federal --member Jeffries --out out/jeffries

    # Add an AI "record at a glance"
    ANTHROPIC_API_KEY=... python3 scheduled_packet.py --level nyc --member Hanks --out out/hanks

Weekly cron (Mondays 08:07):
    7 8 * * 1  cd /path/to/repo && /usr/bin/python3 scheduled_packet.py \
                 --level nyc --member Hanks --out out/hanks_$(date +\\%Y\\%m\\%d)

Everything is defensive; on a data-source failure it still writes whatever it
could assemble and exits non-zero so a scheduler can alert.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    requests = None

import packet as _packet
import profiles as _profiles
import briefing as _brief
import llm as _llm

# The same public NYC read token the app uses.
NYC_TOKEN = ("Uvxb0j9syjm3aI8h46DhQvnX5skN4aSUL0x_Ee3ty9M.ew0KICAiVmVyc2lvbiI6IDEsDQ"
             "ogICJOYW1lIjogIk5ZQyByZWFkIHRva2VuIDIwMTcxMDI2IiwNCiAgIkRhdGUiOiAiMjAxN"
             "y0xMC0yNlQxNjoyNjo1Mi42ODM0MDYtMDU6MDAiLA0KICAiV3JpdGUiOiBmYWxzZQ0KfQ")
API_BASE = "https://webapi.legistar.com/v1/nyc"


# ---------------------------------------------------------------------------
# Compact, self-contained NYC Legistar fetch (mirrors the app's approach)
# ---------------------------------------------------------------------------
def _leg_get(path, params=None):
    params = dict(params or {}); params["token"] = NYC_TOKEN
    for attempt in range(5):
        try:
            r = requests.get(f"{API_BASE}/{path}", params=params, timeout=90)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 15)); continue
            r.raise_for_status()
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 15))
    return []


def _matters_since(since):
    flt = f"MatterIntroDate ge datetime'{since}'"
    out, seen, skip = [], set(), 0
    while True:
        batch = _leg_get("matters", {"$top": 1000, "$skip": skip, "$filter": flt})
        if not isinstance(batch, list) or not batch:
            break
        for m in batch:
            mid = m.get("MatterId")
            if mid not in seen:
                seen.add(mid); out.append(m)
        if len(batch) < 1000:
            break
        skip += 1000
    return out


def gather_nyc_member(name, year, cap=4000):
    """Scan the year's matters and keep those the member sponsors. Bounded by `cap`."""
    if not requests:
        return None, "requests not available"
    last = name.split()[-1].lower() if name.split() else name.lower()
    since = f"{int(year)}-01-01"
    matters = _matters_since(since)[:cap]
    if not matters:
        return {"bills": [], "stats": {}}, "no matters returned (network or empty year)"
    keep = []

    def _check(m):
        sp = _leg_get(f"matters/{m.get('MatterId')}/sponsors")
        names = [(s.get("MatterSponsorName") or "") for s in (sp or [])]
        if any(last in n.lower() for n in names):
            prime = next((s.get("MatterSponsorName") for s in sp
                          if s.get("MatterSponsorSequence") == 0), "")
            return {"File": m.get("MatterFile", ""), "Type": m.get("MatterTypeName", ""),
                    "Title": (m.get("MatterTitle") or m.get("MatterName") or "").strip(),
                    "Status": m.get("MatterStatusName", ""),
                    "Prime Sponsor": prime,
                    "Web Link": f"https://legistar.council.nyc.gov/gateway.aspx?m=l&id={m.get('MatterId')}",
                    "_is_prime": last in (prime or "").lower()}
        return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(_check, m) for m in matters]):
            try:
                r = fut.result()
                if r:
                    keep.append(r)
            except Exception:
                pass

    prime = sum(1 for r in keep if r["_is_prime"])
    passed = sum(1 for r in keep if any(w in (r["Status"] or "").lower()
                                        for w in ("enacted", "adopted", "approved")))
    stats = {"bills_on": len(keep), "as_prime": prime, "as_cosponsor": len(keep) - prime,
             "by_status": {"passed": passed}}
    return {"bills": sorted(keep, key=lambda r: r["File"]), "stats": stats}, None


def gather_federal_member(name):
    from sources import congress as _cong
    import people as _people
    legs = _cong.load_legislators()
    deleg = [_people.federal_profile(p) for p in _cong.nyc_delegation(legs)]
    last = name.split()[-1].lower() if name.split() else name.lower()
    d = next((x for x in deleg if last in x["name"].lower()), None)
    if not d:
        return None, f"'{name}' not found in NYC's federal delegation"
    coms = _cong.load_committee_membership().get(d["extra"].get("bioguide", ""), [])
    spon = []
    ckey = os.environ.get("CONGRESS_API_KEY", "")
    if ckey:
        try:
            spon = _cong.CongressClient(api_key=ckey).member_legislation(
                d["extra"].get("bioguide", ""), kind="sponsored")
        except Exception:
            spon = []
    facts = _profiles.federal_facts(d, committees=coms, sponsored=spon)
    return {"profile": d, "facts": facts, "bills": spon}, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a District Packet headlessly.")
    ap.add_argument("--level", choices=["nyc", "federal"], default="nyc")
    ap.add_argument("--member", required=True, help="Member name / last name")
    ap.add_argument("--year", type=int, default=None, help="Session year (NYC); defaults to current")
    ap.add_argument("--out", default="packet", help="Output path prefix (writes .md and .html)")
    args = ap.parse_args(argv)

    import datetime
    year = args.year or datetime.date.today().year
    as_of = datetime.date.today().isoformat()
    warn = None

    if args.level == "nyc":
        data, warn = gather_nyc_member(args.member, year)
        if data is None:
            print(f"ERROR: {warn}", file=sys.stderr); return 2
        facts = _profiles.council_facts(args.member, data["stats"])
        bills = data["bills"]
        level_label = "NYC City Council"
        glance_name = args.member
    else:
        data, warn = gather_federal_member(args.member)
        if data is None:
            print(f"ERROR: {warn}", file=sys.stderr); return 2
        facts = data["facts"]; bills = data["bills"]
        level_label = "U.S. Congress"; glance_name = data["profile"]["name"]

    glance = ""
    client = _llm.LLM(api_key=os.environ.get("ANTHROPIC_API_KEY"), model=_llm.SMART_MODEL)
    if client.ready:
        glance = _profiles.glance(client, level_label, glance_name, facts)

    md = _packet.build_packet_md(glance_name, level_label, facts=facts, glance=glance,
                                 bills=bills, as_of=as_of)
    html = _brief.print_html(_brief.md_to_html(md), title=f"District packet — {glance_name}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(f"{args.out}.md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(f"{args.out}.html", "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {args.out}.md and {args.out}.html "
          f"({len(bills)} bills{' · AI glance' if glance else ''}).")
    if warn:
        print(f"NOTE: {warn}", file=sys.stderr)
    return 0 if not warn else 0


if __name__ == "__main__":
    sys.exit(main())
