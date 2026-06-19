#!/usr/bin/env python3
"""
legistar_sync.py  —  NYC Council legislation tracker (1:1 with Legistar)

Pulls legislation directly from the NYC Legistar Web API (the same data store the
legistar.council.nyc.gov pages render from). Detects what changed since the last run.
Optionally adds AI-drafted impact bullets (cached so re-runs only pay for new/changed bills).

  DATA layer  (exact, from API): Matters, Sponsors, Histories, Attachments, Text.
  ANALYSIS layer (interpretive, labeled): keyword tags and/or AI-drafted impact bullets.

CLI runs ONE profile. For multiple scoped workbooks on a schedule, use run_sync.py + config.json.

  python3 legistar_sync.py --file "Int 0225-2026" --enrich --out int225.xlsx
  python3 legistar_sync.py --since 2024-01-01 --sponsor Hanks --enrich --text --impact ai --out hanks.xlsx
"""

import argparse, hashlib, json, os, re, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None  # allowed; only needed for live runs, not offline tests

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

API_BASE = "https://webapi.legistar.com/v1/nyc"
# Verified-stable public link: the gateway 302-redirects to the correct LegislationDetail page.
WEB_BASE = "https://legistar.council.nyc.gov/gateway.aspx?m=l&id="
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
PAGE = 1000


def _ver_rank(v):
    """NYC matter versions are '*' (introduced) then 'A','B',...  Rank '*' lowest, letters ascending.
    (The stock scraper assumes integer versions and would crash on NYC's letters.)"""
    if v is None:
        return (-1, "")
    return (0, "") if str(v) == "*" else (1, str(v))


# ============================================================================
# DATA LAYER — Legistar Web API client
# ============================================================================
class LegistarClient:
    def __init__(self, token=None, pause=0.0):
        self.token = token or os.environ.get("LEGISTAR_TOKEN")
        self.pause = pause
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})

    def _get(self, path, params=None):
        params = dict(params or {})
        if self.token:
            params["token"] = self.token
        last_err = None
        for attempt in range(6):
            try:
                r = self.s.get(f"{API_BASE}/{path}", params=params, timeout=90)
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(min(2 ** attempt, 20))  # ride out dropped connections
                continue
            if r.status_code == 200:
                if self.pause:
                    time.sleep(self.pause)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 20)); continue
            r.raise_for_status()
        if last_err:
            raise last_err
        r.raise_for_status()

    def matters(self, odata_filter=None, top=PAGE):
        # Page WITHOUT a volatile $orderby and de-dupe by MatterId, exactly as the
        # production NYC scraper does: ordering by a non-unique field while paging by
        # $skip can drop/duplicate rows at page boundaries.
        out, seen, skip = [], set(), 0
        while True:
            params = {"$top": top, "$skip": skip}
            if odata_filter:
                params["$filter"] = odata_filter
            batch = self._get("matters", params)
            for m in batch:
                mid = m.get("MatterId")
                if mid not in seen:
                    seen.add(mid)
                    out.append(m)
            if len(batch) < top:
                return out
            skip += top

    def sponsors(self, mid):     return self._get(f"matters/{mid}/sponsors")
    def histories(self, mid):    return self._get(f"matters/{mid}/histories")
    def attachments(self, mid):  return self._get(f"matters/{mid}/attachments")

    def text_plain(self, mid, matter_version=None):
        # Verified shape: /matters/{id}/versions -> [{"Key":<text id>, "Value":<'*'|'A'|...>}],
        # pick the version whose Value == the matter's MatterVersion (else highest), then
        # /matters/{id}/texts/{Key} -> MatterTextPlain.
        try:
            versions = self._get(f"matters/{mid}/versions")
        except Exception:
            return ""
        if not versions:
            return ""
        chosen = None
        if matter_version is not None:
            chosen = next((v for v in versions if str(v.get("Value")) == str(matter_version)), None)
        if chosen is None:
            chosen = max(versions, key=lambda v: _ver_rank(v.get("Value")))
        try:
            t = self._get(f"matters/{mid}/texts/{chosen.get('Key')}")
            if isinstance(t, list):
                t = t[-1] if t else {}
            return (t or {}).get("MatterTextPlain") or ""
        except Exception:
            return ""

    def events(self, odata_filter=None, top=PAGE):
        out, seen, skip = [], set(), 0
        while True:
            params = {"$top": top, "$skip": skip}
            if odata_filter:
                params["$filter"] = odata_filter
            batch = self._get("events", params)
            for e in batch:
                eid = e.get("EventId")
                if eid not in seen:
                    seen.add(eid)
                    out.append(e)
            if len(batch) < top:
                return out
            skip += top

    def event_items(self, event_id):
        try:
            return self._get(f"events/{event_id}/eventitems")
        except Exception:
            return []

    def council_members(self):
        """Current Council Member names from the City Council body's active office records."""
        try:
            bodies = self._get("bodies")
            cc = next((b for b in bodies if (b.get("BodyName") or "").strip().lower() == "city council"), None)
            if cc:
                recs = self._get(f"bodies/{cc['BodyId']}/OfficeRecords")
                today = datetime.now().date().isoformat()
                names = set()
                for r in recs:
                    end = (r.get("OfficeRecordEndDate") or "")[:10]
                    if not end or end >= today:
                        nm = (r.get("OfficeRecordPersonName") or r.get("OfficeRecordFullName") or "").strip()
                        if nm:
                            names.add(nm)
                if names:
                    return sorted(names)
        except Exception:
            pass
        try:
            persons = self._get("persons", {"$filter": "PersonActiveFlag eq 1"})
            return sorted({(p.get("PersonFullName") or "").strip() for p in persons if p.get("PersonFullName")})
        except Exception:
            return []


# ============================================================================
# DATA LAYER — pure transforms (no network; unit-testable)
# ============================================================================
def web_url(m):
    return f"{WEB_BASE}{m.get('MatterId')}"


def current_sponsors(matter, sponsor_json):
    # Mirror the production scraper: keep sponsors at the latest version, sorted by sequence.
    # Prefer the matter's MatterVersion; else the highest sponsor version (NYC-safe rank).
    rows = [s for s in (sponsor_json or []) if (s.get("MatterSponsorName") or "").strip()]
    if not rows:
        return []
    target = matter.get("MatterVersion")
    present = {s.get("MatterSponsorMatterVersion") for s in rows}
    if target not in present:
        target = max(present, key=_ver_rank)
    scoped = [s for s in rows if s.get("MatterSponsorMatterVersion") == target] or rows
    by_name = {}
    for s in scoped:
        name = s["MatterSponsorName"].strip()
        seq = s.get("MatterSponsorSequence", 9999)
        if name not in by_name or seq < by_name[name].get("MatterSponsorSequence", 9999):
            by_name[name] = s
    return sorted(by_name.values(), key=lambda s: s.get("MatterSponsorSequence", 9999))


def _date(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).strftime("%-m/%-d/%Y")
    except Exception:
        return str(s)[:10]


def normalize_event(e):
    insite = e.get("EventInSiteURL") or f"https://legistar.council.nyc.gov/gateway.aspx?m=e&id={e.get('EventId')}"
    return {
        "Date": _date(e.get("EventDate")),
        "Time": (e.get("EventTime") or "").strip(),
        "Committee / Body": e.get("EventBodyName", ""),
        "Location": (e.get("EventLocation") or "").strip(),
        "Agenda status": e.get("EventAgendaStatusName", ""),
        "Agenda": e.get("EventAgendaFile", "") or "",
        "Minutes": e.get("EventMinutesFile", "") or "",
        "Legistar": insite,
        "EventId": e.get("EventId"),
        "_sortdate": e.get("EventDate") or "",
    }


# --- NYC Open Data (311) grounding for bill analysis ---
COMPLAINT_MAP = [
    (("noise", "amplified", "quiet"), "Noise"),
    (("rat", "rodent", "vermin", "pest"), "Rodent"),
    (("tree", "forestry"), "Damaged Tree"),
    (("pothole", "roadway", "pavement", "street condition"), "Street Condition"),
    (("heat", "hot water", "boiler"), "HEAT/HOT WATER"),
    (("homeless",), "Homeless Person Assistance"),
    (("tow", "abandoned vehicle", "illegal park", "parking"), "Illegal Parking"),
    (("garbage", "trash", "litter", "dumping", "sanitation"), "Dirty Condition"),
    (("flood", "sewer", "storm", "drain", "catch basin"), "Sewer"),
    (("graffiti",), "Graffiti"),
    (("idling", "emissions", "air quality"), "Air Quality"),
    (("sidewalk", "scaffold", "construction"), "Sidewalk Condition"),
    (("lead", "mold", "asbestos"), "Lead"),
]
_BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]


def bill_311_keyword(row):
    blob = ((row.get("Title") or "") + " " + (row.get("Name") or "")).lower()
    for terms, ctype in COMPLAINT_MAP:
        if any(t in blob for t in terms):
            return ctype
    return None


def nyc_311_context(borough=None, complaint_type=None, days=365, timeout=20):
    """Live count of 311 complaints from NYC Open Data (dataset erm2-nwe9). Best-effort."""
    from datetime import timedelta
    import urllib.request
    import urllib.parse
    base = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    where = [f"created_date >= '{since}'"]
    if borough:
        where.append(f"upper(borough)='{borough.upper()}'")
    if complaint_type:
        where.append(f"complaint_type='{complaint_type}'")
    url = base + "?" + urllib.parse.urlencode({"$select": "count(*) AS n", "$where": " AND ".join(where)})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return {"ok": True, "count": (data[0].get("n") if data else None), "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}


def build_data_context(row):
    """Assemble a small REAL-DATA block for a bill (currently 311). Returns '' if nothing relevant."""
    ctype = bill_311_keyword(row)
    if not ctype:
        return ""
    bors = [b for b in _BOROUGHS if b in (row.get("Boroughs named") or "")]
    bor = bors[0] if len(bors) == 1 else None
    scope = bor or "citywide"
    res = nyc_311_context(bor, ctype)
    if res.get("ok") and res.get("count") is not None:
        return (f"NYC 311 service requests, last 12 months, {scope}: {res['count']} '{ctype}' complaints. "
                f"Source: NYC Open Data 311 (dataset erm2-nwe9).")
    return (f"A NYC 311 lookup for '{ctype}' complaints ({scope}) is relevant here but was not retrieved live; "
            f"verify at NYC Open Data (dataset erm2-nwe9).")


def normalize_matter(m, sponsors=None, histories=None, attachments=None):
    sp = current_sponsors(m, sponsors) if sponsors is not None else None
    prime = next((s["MatterSponsorName"] for s in (sp or []) if s.get("MatterSponsorSequence") == 0), "")
    if not prime and sp:
        prime = sp[0]["MatterSponsorName"]
    names = [s["MatterSponsorName"] for s in (sp or [])]
    latest = None
    if histories:
        dated = [h for h in histories if h.get("MatterHistoryActionDate")]
        latest = max(dated, key=lambda h: h["MatterHistoryActionDate"], default=None)
    return {
        "File": m.get("MatterFile", ""), "Type": m.get("MatterTypeName", ""),
        "Name": m.get("MatterName", ""), "Title": m.get("MatterTitle", ""),
        "Status": m.get("MatterStatusName", ""), "Committee/Body": m.get("MatterBodyName", ""),
        "Intro Date": _date(m.get("MatterIntroDate")), "Agenda Date": _date(m.get("MatterAgendaDate")),
        "Passed Date": _date(m.get("MatterPassedDate")), "Enacted Date": _date(m.get("MatterEnactmentDate")),
        "Law #": m.get("MatterEnactmentNumber", ""),
        "Sponsors (#)": len(names) if sp is not None else "",
        "Prime Sponsor": prime,
        "Hanks?": "Y" if any("Hanks" in n for n in names) else ("" if sp is not None else "?"),
        "Latest Action": (latest or {}).get("MatterHistoryActionName", ""),
        "Latest Action Date": _date((latest or {}).get("MatterHistoryActionDate")),
        "Attachments (#)": len(attachments) if attachments is not None else "",
        "Last Modified (UTC)": m.get("MatterLastModifiedUtc", ""),
        "MatterId": m.get("MatterId", ""), "Web Link": web_url(m),
        "_sponsor_names": names, "_prime": prime,
        "_sponsor_objs": sp or [], "_status_raw": m.get("MatterStatusName", ""),
    }


# ============================================================================
# ANALYSIS LAYER (1) — heuristic keyword tagging (free, transparent)
# ============================================================================
PILLARS = {
    "Arts & Culture": ["arts", "cultural", "museum", "artist", "theater", "library", "heritage", "public art", "DCLA", "humanities"],
    "Neighborhood Dev": ["zoning", "land use", "rezon", "ULURP", "housing", "landmark", "preservation", "neighborhood", "construction", "building", "tenant"],
    "Economic Dev": ["economic", "small business", "commercial", "workforce", "employment", "job", "tourism", "EDC", "tax credit", "women-owned"],
    "Public Safety/Crisis": ["NYPD", "police", "FDNY", "fire", "emergency", "crime", "traffic", "tow", "911", "B-HEARD", "shelter", "flood", "storm"],
    "Health & Hospitals": ["health", "hospital", "H+H", "clinic", "medical", "DOHMH", "mental health", "substance", "care", "wellness"],
}
SI_TERMS = ["staten island", "north shore", "verrazzano", "district 49", "st. george", "stapleton",
            "port richmond", "tompkinsville", "ferry", "tow pound", "borough of richmond"]
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]


def keyword_tags(row, text=""):
    blob = " ".join([row.get("Title", ""), row.get("Name", ""), text]).lower()
    pillars = [p for p, kws in PILLARS.items() if any(k.lower() in blob for k in kws)]
    si = [t for t in SI_TERMS if t in blob]
    bor = [b for b in BOROUGHS if b.lower() in blob]
    return {"Pillar Tags": "; ".join(pillars),
            "SI/D-49 Signal": f"{len(si)} ({', '.join(si)})" if si else "0",
            "Boroughs Named": "; ".join(bor) or "(citywide / none named)"}


# ============================================================================
# ANALYSIS LAYER (2) — AI-drafted impact bullets (Anthropic API, cached)
# ============================================================================
PROMPT = """You are a legislative analyst for NYC Council Member Kamillah Hanks (District 49, \
North Shore Staten Island; majority whip). Given a NYC Council bill's file number, title, and text, \
write a tight impact read. Ground every statement ONLY in the provided text; do not invent specifics.

Return ONLY a JSON object with keys "purpose", "affects", "d49":
- purpose: <=25 words, plain language, what the bill actually does.
- affects: <=30 words, who/what across the city it touches and how.
- d49: <=35 words, the Staten Island / District 49 angle tied to CM Hanks's pillars (Arts & Culture, \
Neighborhood Development, Economic Development, Public Safety/Crisis, Health & Hospitals, SI parity/independence). \
If the bill has little direct D-49 nexus, say so plainly rather than stretching.

File: {file}
Type: {type}
Title: {title}
Text: {text}

JSON:"""


DOSSIER_PROMPT = """You are a nonpartisan legislative analyst preparing a neutral staff profile of a sitting \
NYC Council Member, for internal professional use. Use ONLY the structured data provided below (their sponsorship \
record, topic mix, bill outcomes, and frequent co-sponsors).

STRICT RULES:
- Do NOT invent or assume facts: no quotes, biographical details, party label, district, scandals, endorsements, \
or policy positions that are not derivable from the data.
- If the data is thin or a section can't be supported, say so plainly rather than speculating.
- Neutral, factual, analytical tone. No praise, no criticism, no campaign language.
- Clearly mark interpretive statements as inferences ("the record suggests...").

Write ~180 words with these short labeled sections:
1. Policy focus — the leading topic areas, from the topic mix.
2. Legislative activity — volume, prime vs co-sponsor balance, and how much has passed vs stalled.
3. Coalition — the colleagues they most often co-sponsor alongside.
4. Patterns & caveats — any notable pattern, plus a one-line caveat that this reflects sponsorship activity only \
(not floor votes) and is AI-generated.

Member: {member}
Legislative data (JSON): {stats}

Profile:"""


ANALYSIS_PROMPT = """You are a nonpartisan legislative policy analyst writing an internal briefing on a NYC Council bill.
Use the bill information and any REAL DATA CONTEXT provided below.

RULES:
- Ground every claim in the bill text or the provided data. Do NOT invent statistics, dollar amounts, poll numbers,
  agency budgets, or quotes. If you lack a figure, say what data would answer it and name the source to check.
- Be balanced: present support and opposition fairly. Mark predictions clearly as predictions.
- Neutral, professional tone. Label the brief as AI analysis/inference, not an official statement.

Write these sections, each a bold header followed by tight bullets:
**What the bill does** — plain-language summary of the mechanism and who it covers.
**Who would support it** — constituencies/stakeholders likely in favor, and the reasons they'd give.
**Who would oppose it / concerns** — likely opponents and their strongest objections or trade-offs.
**Political analysis** — sponsor coalition, partisan/borough dynamics, and what moving it would realistically take.
**District / borough / citywide outlook** — how effects differ at the district level, the borough level, and citywide.
**Fiscal analysis** — qualitative cost and revenue drivers; what an OMB/IBO fiscal note would examine; name the
  specific figures to verify. Do NOT fabricate numbers.
**Why it exists / who needed it** — the underlying problem; cite the REAL DATA CONTEXT if present, and name the NYC
  Open Data / agency datasets (311, agency reports, IBO, Open Data portal) that would document the need.
**If implemented** — likely near-term and longer-term effects, plus risks and what to monitor afterward.

BILL
File: {file} | Type: {type} | Status: {status} | Committee: {committee}
Title: {title}
Sponsors: {sponsors}
Text (excerpt): {text}

REAL DATA CONTEXT (may be empty):
{data}

Briefing:"""


def text_hash(*parts):
    return hashlib.sha1("||".join(p or "" for p in parts).encode("utf-8", "ignore")).hexdigest()[:16]


class AIImpact:
    def __init__(self, model, api_key=None, pause=0.0):
        self.model = model
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.pause = pause
        self.s = requests.Session() if requests else None

    def draft(self, row, text):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        prompt = PROMPT.format(file=row.get("File", ""), type=row.get("Type", ""),
                               title=row.get("Title", ""), text=(text or row.get("Name", ""))[:6000])
        body = {"model": self.model, "max_tokens": 320,
                "messages": [{"role": "user", "content": prompt}]}
        r = self.s.post(ANTHROPIC_URL, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json=body, timeout=90)
        r.raise_for_status()
        data = r.json()
        txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if self.pause:
            time.sleep(self.pause)
        return parse_bullets(txt)

    def dossier(self, member, stats):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        prompt = DOSSIER_PROMPT.format(member=member, stats=json.dumps(stats, ensure_ascii=False)[:6000])
        body = {"model": self.model, "max_tokens": 700,
                "messages": [{"role": "user", "content": prompt}]}
        r = self.s.post(ANTHROPIC_URL, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

    def analyze(self, row, text, data_ctx=""):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        sponsors = ", ".join(s.get("MatterSponsorName", "") for s in (row.get("_sponsor_objs") or [])) or "(not loaded)"
        prompt = ANALYSIS_PROMPT.format(
            file=row.get("File", ""), type=row.get("Type", ""), status=row.get("Status", ""),
            committee=row.get("Committee/Body", ""), title=row.get("Title", ""),
            sponsors=sponsors, text=(text or row.get("Name", ""))[:7000], data=data_ctx or "(none retrieved)")
        body = {"model": self.model, "max_tokens": 1400,
                "messages": [{"role": "user", "content": prompt}]}
        r = self.s.post(ANTHROPIC_URL, headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json=body, timeout=150)
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def parse_bullets(txt):
    s = re.sub(r"^```(json)?|```$", "", (txt or "").strip(), flags=re.MULTILINE).strip()
    try:
        d = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
    return {"purpose": d.get("purpose", ""), "affects": d.get("affects", ""), "d49": d.get("d49", "")}


# ============================================================================
# CHANGE DETECTION + caches — local snapshot
# ============================================================================
def fingerprint(row):
    return {"File": row["File"], "Status": row["Status"], "SponsorCount": row["Sponsors (#)"],
            "Sponsors": sorted(row.get("_sponsor_names", [])), "Prime": row.get("_prime", ""),
            "Attachments": row["Attachments (#)"], "LatestAction": row["Latest Action"],
            "LastModified": row["Last Modified (UTC)"]}


class Snapshot:
    def __init__(self, path="legistar_state.db"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (matter_id INTEGER PRIMARY KEY, fp TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ai (matter_id INTEGER, thash TEXT, model TEXT, "
                        "bullets TEXT, created TEXT, PRIMARY KEY (matter_id, thash, model))")
        self.db.commit()

    def load(self):
        return {mid: json.loads(fp) for mid, fp in self.db.execute("SELECT matter_id, fp FROM state")}

    def save(self, rows):
        self.db.executemany("INSERT OR REPLACE INTO state VALUES (?,?)",
                            [(r["MatterId"], json.dumps(fingerprint(r))) for r in rows])
        self.db.commit()

    def ai_get(self, mid, thash, model):
        row = self.db.execute("SELECT bullets FROM ai WHERE matter_id=? AND thash=? AND model=?",
                              (mid, thash, model)).fetchone()
        return json.loads(row[0]) if row else None

    def ai_put(self, mid, thash, model, bullets):
        self.db.execute("INSERT OR REPLACE INTO ai VALUES (?,?,?,?,?)",
                        (mid, thash, model, json.dumps(bullets),
                         datetime.now(timezone.utc).isoformat()))
        self.db.commit()


def diff(old, rows):
    changes = []
    for r in rows:
        mid, new = r["MatterId"], fingerprint(r)
        prev = old.get(mid)
        if prev is None:
            changes.append((r["File"], "NEW", "—", "—", f"{r['Type']}: {r['Title'][:80]}")); continue
        if prev["Status"] != new["Status"]:
            changes.append((r["File"], "STATUS", "Status", prev["Status"], new["Status"]))
        added = set(new["Sponsors"]) - set(prev["Sponsors"])
        dropped = set(prev["Sponsors"]) - set(new["Sponsors"])
        if added:
            changes.append((r["File"], "SPONSOR +", "Sponsors", f'{prev["SponsorCount"]} sponsors', "; ".join(sorted(added))))
        if dropped:
            changes.append((r["File"], "SPONSOR -", "Sponsors", "; ".join(sorted(dropped)), f'now {new["SponsorCount"]}'))
        if prev["Prime"] != new["Prime"]:
            changes.append((r["File"], "PRIME", "Prime Sponsor", prev["Prime"], new["Prime"]))
        if prev["LatestAction"] != new["LatestAction"]:
            changes.append((r["File"], "ACTION", "Latest Action", prev["LatestAction"], new["LatestAction"]))
        if prev["Attachments"] != new["Attachments"]:
            changes.append((r["File"], "ATTACH", "Attachments (#)", prev["Attachments"], new["Attachments"]))
    return changes


# ============================================================================
# OUTPUT — house-style workbook
# ============================================================================
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY = Font(name="Arial", size=10)
AI_FONT = Font(name="Arial", size=10, italic=True)
CHANGE_FILL = PatternFill("solid", fgColor="FFF2CC")
AI_FILL = PatternFill("solid", fgColor="EAF1FB")
THIN = Border(*(Side(style="thin", color="D9D9D9"),) * 4)


def _sheet(wb, title, headers, rows, widths=None, fill=None, font=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(1, c); cell.font, cell.fill = HDR_FONT, HDR_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(headers)):
        for cell in row:
            cell.font = font or BODY; cell.border = THIN
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if fill:
                cell.fill = fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def build_workbook(bundle, path):
    rows = bundle["rows"]
    sponsors_map, histories_map = bundle["sponsors_map"], bundle["histories_map"]
    attach_map, text_map = bundle["attach_map"], bundle["text_map"]
    ai_map, changes, run_info = bundle["ai_map"], bundle["changes"], bundle["run_info"]
    impact_mode = bundle["impact_mode"]

    wb = Workbook(); wb.remove(wb.active)

    cols = ["File", "Type", "Title", "Status", "Committee/Body", "Intro Date", "Latest Action",
            "Latest Action Date", "Sponsors (#)", "Prime Sponsor", "Hanks?", "Law #",
            "Last Modified (UTC)", "MatterId", "Web Link"]
    add_kw = impact_mode in ("keyword", "ai")
    if add_kw:
        cols += ["Pillar Tags", "SI/D-49 Signal", "Boroughs Named"]
    body = []
    for r in rows:
        line = [r.get(c, "") for c in cols if c not in ("Pillar Tags", "SI/D-49 Signal", "Boroughs Named")]
        if add_kw:
            t = keyword_tags(r, (text_map.get(r["MatterId"], "") or "")[:4000])
            line += [t["Pillar Tags"], t["SI/D-49 Signal"], t["Boroughs Named"]]
        body.append(line)
    _sheet(wb, "Matters", cols, body,
           widths=[16, 15, 58, 15, 24, 11, 26, 12, 11, 22, 7, 9, 22, 10, 50] + ([22, 26, 22] if add_kw else []))

    if impact_mode == "ai":
        ai_rows = []
        for r in rows:
            b = ai_map.get(r["MatterId"])
            if b:
                ai_rows.append([r["File"], r["Title"], b.get("purpose", ""), b.get("affects", ""),
                                b.get("d49", ""), run_info.get("AI model", "")])
        _sheet(wb, "Impact (AI-drafted)",
               ["File", "Title", "Purpose", "Who it affects", "D-49 angle", "Model"],
               ai_rows, widths=[16, 46, 40, 40, 44, 22], fill=AI_FILL, font=AI_FONT)

    sp_rows = []
    for r in rows:
        for s in r.get("_sponsor_objs", []):
            seq = s.get("MatterSponsorSequence", "")
            sp_rows.append([r["File"], r["Type"], s.get("MatterSponsorName", ""),
                            seq, "Prime" if seq == 0 else "Co-sponsor", r["MatterId"]])
    _sheet(wb, "Sponsors", ["File", "Type", "Sponsor", "Seq", "Role", "MatterId"], sp_rows,
           widths=[16, 15, 28, 6, 12, 10])

    h_rows = []
    for r in rows:
        seen_h = set()
        for h in sorted(histories_map.get(r["MatterId"], []), key=lambda x: x.get("MatterHistoryActionDate") or ""):
            d = h.get("MatterHistoryActionDate")
            a = (h.get("MatterHistoryActionName") or "").strip()
            b = h.get("MatterHistoryActionBodyName") or ""
            if not d or not a:
                continue
            k = (d, a, b)
            if k in seen_h:  # duplicate data-entry rows happen; drop them
                continue
            seen_h.add(k)
            h_rows.append([r["File"], _date(d), a, b, h.get("MatterHistoryPassedFlagName", "")])
    _sheet(wb, "History", ["File", "Action Date", "Action", "Action By", "Result"], h_rows,
           widths=[16, 12, 40, 30, 12])

    a_rows = []
    for r in rows:
        for a in attach_map.get(r["MatterId"], []):
            a_rows.append([r["File"], a.get("MatterAttachmentName", ""), a.get("MatterAttachmentHyperlink", "")])
    _sheet(wb, "Attachments", ["File", "Attachment", "Link"], a_rows, widths=[16, 40, 70])

    if text_map:
        t_rows = [[r["File"], r["Title"], (text_map.get(r["MatterId"], "") or "")[:30000]]
                  for r in rows if text_map.get(r["MatterId"])]
        _sheet(wb, "Bill Text", ["File", "Title", "Plain Text (truncated 30k)"], t_rows,
               widths=[16, 50, 120])

    hdr = ["File", "Change", "Field", "Was", "Now"]
    if changes:
        _sheet(wb, "Changes Since Last Sync", hdr, changes, widths=[16, 12, 16, 40, 40], fill=CHANGE_FILL)
    else:
        msg = run_info.get("Changes placeholder", "(no changes since last sync)")
        _sheet(wb, "Changes Since Last Sync", hdr, [[msg, "", "", "", ""]], widths=[16, 12, 16, 40, 40])

    _sheet(wb, "Run Info", ["Field", "Value"], [[k, v] for k, v in run_info.items()], widths=[26, 80])
    wb.move_sheet("Matters", -(len(wb.sheetnames) - 1))
    wb.save(path)
    return path


# ============================================================================
# ORCHESTRATION — shared by CLI and the scheduled runner
# ============================================================================
def assemble(client, ai, snap, old, profile):
    """Run one profile. Returns a bundle dict ready for build_workbook. Does NOT save snapshot."""
    f = profile.get("filter", {})
    clauses = []
    if f.get("file"):  clauses.append(f"MatterFile eq '{f['file']}'")
    if f.get("since"): clauses.append(f"MatterIntroDate ge datetime'{f['since']}'")
    if f.get("until"): clauses.append(f"MatterIntroDate lt datetime'{f['until']}'")
    if f.get("type"):  clauses.append(f"MatterTypeName eq '{f['type']}'")
    odata = " and ".join(clauses) or None

    raw = client.matters(odata)
    raw.sort(key=lambda m: m.get("MatterIntroDate") or "", reverse=True)  # safe in-memory display order
    if profile.get("limit"):
        raw = raw[: profile["limit"]]

    enrich = profile.get("enrich", False)
    enrich_status = set(profile.get("enrich_status") or [])  # empty => enrich all
    want_text = profile.get("text", False)
    workers = profile.get("workers", 4)
    sponsor_filter = (f.get("sponsor") or "").lower()

    sponsors_map, histories_map, attach_map, text_map = {}, {}, {}, {}

    def _pool(items, fn, label):
        total = len(items)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(fn, m) for m in items]):
                fut.result()
                done += 1
                if total > 40 and done % 25 == 0:
                    print(f"   {label}: {done}/{total}")

    # Phase A — sponsor scan (cheap: 1 call/matter) only when filtering by sponsor
    if sponsor_filter:
        print(f"Scanning {len(raw)} bills for sponsor '{f['sponsor']}' — this is the slow part, please wait...")
        _pool(raw, lambda m: sponsors_map.__setitem__(m["MatterId"], client.sponsors(m["MatterId"])), "scanned")
        raw = [m for m in raw
               if any(sponsor_filter in (s.get("MatterSponsorName") or "").lower()
                      for s in current_sponsors(m, sponsors_map.get(m["MatterId"], [])))]
        print(f"   matched {len(raw)} bills sponsored by '{f['sponsor']}'")

    # Phase B — heavy detail (histories/attachments/text) only on the matters we keep
    if enrich:
        targets = [m for m in raw if not enrich_status or m.get("MatterStatusName") in enrich_status]
        sponsors_only = profile.get("sponsors_only", False)

        def heavy(m):
            mid = m["MatterId"]
            if mid not in sponsors_map:
                sponsors_map[mid] = client.sponsors(mid)
            if sponsors_only:
                return
            histories_map[mid] = client.histories(mid)
            attach_map[mid] = client.attachments(mid)
            if want_text:
                tx = client.text_plain(mid, m.get("MatterVersion"))
                if tx:
                    text_map[mid] = tx

        if targets:
            print(f"Pulling {'sponsors' if sponsors_only else 'full details'} for {len(targets)} bills...")
            _pool(targets, heavy, "details")

    rows = []
    for m in raw:
        mid = m["MatterId"]
        rows.append(normalize_matter(
            m,
            sponsors_map.get(mid) if mid in sponsors_map else None,
            histories_map.get(mid),
            attach_map.get(mid) if mid in attach_map else None))

    impact_mode = profile.get("impact", "none")
    ai_map = {}
    ai_drafted = ai_cached = 0
    if impact_mode == "ai" and ai is not None:
        cap = profile.get("ai_max_bills", 0)
        consider = rows[:cap] if cap else rows
        for r in consider:
            mid = r["MatterId"]
            th = text_hash(r.get("Title", ""), text_map.get(mid, ""))
            cached = snap.ai_get(mid, th, ai.model)
            if cached:
                ai_map[mid] = cached; ai_cached += 1; continue
            try:
                b = ai.draft(r, text_map.get(mid, ""))
                snap.ai_put(mid, th, ai.model, b); ai_map[mid] = b; ai_drafted += 1
            except Exception as e:
                ai_map[mid] = {"purpose": f"[AI error: {e}]", "affects": "", "d49": ""}

    changes = diff(old, rows) if old else []
    run_info = {
        "Profile": profile.get("name", "(cli)"),
        "Run timestamp (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "API base": API_BASE, "Filter": odata or "(none)",
        "Sponsor filter": f.get("sponsor", "(none)"),
        "Enrich": enrich, "Enrich status gate": ", ".join(enrich_status) or "(all)",
        "Full text pulled": bool(text_map),
        "Impact mode": impact_mode, "AI model": (ai.model if (impact_mode == "ai" and ai) else "(n/a)"),
        "AI drafted this run": ai_drafted, "AI reused from cache": ai_cached,
        "Matters in workbook": len(rows), "Prior snapshot matters": len(old),
        "Changes detected": len(changes), "Token used": bool(getattr(client, "token", None)),
    }
    return {"rows": rows, "sponsors_map": sponsors_map, "histories_map": histories_map,
            "attach_map": attach_map, "text_map": text_map, "ai_map": ai_map,
            "changes": changes, "run_info": run_info, "impact_mode": impact_mode,
            "_filter": odata, "_raw_count": len(raw)}


def main():
    ap = argparse.ArgumentParser(description="Sync NYC Council legislation 1:1 from Legistar (single profile).")
    ap.add_argument("--file"); ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--type"); ap.add_argument("--sponsor")
    ap.add_argument("--enrich", action="store_true")
    ap.add_argument("--enrich-status", help="Comma list, e.g. Committee — enrich only these statuses")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--impact", choices=["none", "keyword", "ai"], default="none")
    ap.add_argument("--ai-model", default=os.environ.get("LEGISTAR_AI_MODEL", "claude-haiku-4-5-20251001"))
    ap.add_argument("--limit", type=int); ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--state", default="legistar_state.db"); ap.add_argument("--out", default="legislation.xlsx")
    a = ap.parse_args()

    client = LegistarClient()
    snap = Snapshot(a.state)
    old = snap.load()
    ai = AIImpact(a.ai_model) if a.impact == "ai" else None
    profile = {"name": "cli", "filter": {k: getattr(a, k) for k in ("file", "since", "until", "type", "sponsor") if getattr(a, k)},
               "enrich": a.enrich, "enrich_status": (a.enrich_status.split(",") if a.enrich_status else None),
               "text": a.text, "impact": a.impact, "limit": a.limit, "workers": a.workers}
    print(f"[*] {profile['filter'] or '(no filter)'}  impact={a.impact}")
    bundle = assemble(client, ai, snap, old, profile)
    snap.save(bundle["rows"])
    build_workbook(bundle, a.out)
    ri = bundle["run_info"]
    print(f"[*] {ri['Matters in workbook']} matters, {ri['Changes detected']} changes, "
          f"AI drafted {ri['AI drafted this run']} / reused {ri['AI reused from cache']} -> {a.out}")


import collections
def _status_group(s):
    ALIVE = {"Committee", "Laid Over in Committee"}
    PASSED = {"Adopted", "Enacted", "Enacted (Mayor's Desk for Signature)"}
    return "Alive" if s in ALIVE else ("Passed" if s in PASSED else "Dead/Filed")

def overview_stats(rows):
    g = collections.Counter(_status_group(r.get("Status", "")) for r in rows)
    prime = sum(1 for r in rows if r.get("Prime Sponsor") == "Kamillah Hanks")
    hanks = sum(1 for r in rows if r.get("Hanks?") == "Y")
    d49 = sum(1 for r in rows if not str(r.get("SI/D-49 Signal", "0")).startswith("0"))
    return {"total": len(rows), "prime": prime, "hanks_on": hanks, "d49": d49,
            "alive": g.get("Alive", 0), "passed": g.get("Passed", 0), "dead": g.get("Dead/Filed", 0)}

def pillar_counts(rows):
    c = collections.Counter()
    for r in rows:
        for t in (r.get("Pillar Tags") or "").split("; "):
            if t: c[t] += 1
    return dict(c)

def status_counts(rows):
    return dict(collections.Counter(_status_group(r.get("Status", "")) for r in rows))

def coalition_counts(rows, member="Hanks", top=15):
    c = collections.Counter()
    for r in rows:
        names = r.get("_sponsor_names", [])
        if any(member in n for n in names):
            for n in names:
                if member not in n: c[n] += 1
    return dict(c.most_common(top))

import re as _re
def parse_file(fileno):
    """'Int 0220-2026' -> ('Int', 220, '2026'). Returns (prefix, number_or_None, year_or_None)."""
    m = _re.match(r"\s*([A-Za-z\.]+)\s*0*(\d+)\s*-\s*(\d{4})", fileno or "")
    if m:
        return m.group(1), int(m.group(2)), m.group(3)
    return (fileno or ""), None, None

def _blob(r):
    parts = [str(r.get(k, "") or "") for k in
             ("File", "Title", "Name", "Type", "Status", "Committee/Body", "Prime Sponsor",
              "Pillar Tags", "SI/D-49 Signal")]
    parts += list(r.get("_sponsor_names", []))
    return " ".join(parts).lower()

def matches_search(r, query):
    """Smart, simple search. Pure number -> match bill number across ALL types.
    Otherwise every word must appear somewhere (title, sponsors, committee, type, etc.)."""
    if not query or not query.strip():
        return True
    q = query.strip().lower()
    prefix, num, year = parse_file(r.get("File", ""))
    if q.isdigit():
        qn = int(q)
        if num is not None and num == qn:
            return True
        if q in (r.get("File", "") or "").lower():
            return True
        if year and q in year:
            return True
        return False
    return all(tok in _blob(r) for tok in q.split())

def type_counts(rows):
    return dict(collections.Counter(r.get("Type", "") for r in rows if r.get("Type")))

def overview_general(rows):
    g = collections.Counter(_status_group(r.get("Status", "")) for r in rows)
    return {"total": len(rows), "alive": g.get("Alive", 0),
            "passed": g.get("Passed", 0), "dead": g.get("Dead/Filed", 0)}

def member_bills(rows, member):
    m = (member or "").lower().strip()
    if not m:
        return []
    return [r for r in rows if any(m in (n or "").lower() for n in r.get("_sponsor_names", []))
            or m in (r.get("Prime Sponsor", "") or "").lower()]

def member_prime_count(rows, member):
    m = (member or "").lower().strip()
    return sum(1 for r in rows if m in (r.get("Prime Sponsor", "") or "").lower())

def dossier_stats(mb, member):
    m = (member or "").lower()
    prime = member_prime_count(mb, member)
    st_ = overview_general(mb)
    last = member.split()[-1] if member.split() else member
    coal = coalition_counts(mb, member=last)
    prime_titles = [r.get("Title", "") for r in mb if m in (r.get("Prime Sponsor", "") or "").lower()][:8]
    return {"member": member, "bills_on": len(mb), "as_prime": prime, "as_cosponsor": len(mb) - prime,
            "by_topic": pillar_counts(mb),
            "by_status": {"alive": st_["alive"], "passed": st_["passed"], "dead": st_["dead"]},
            "top_coalition": dict(list(coal.items())[:8]),
            "example_prime_bills": prime_titles}

import streamlit as st
import pandas as pd
import datetime as _dt

st.set_page_config(page_title="NYC Council Explorer", layout="wide", initial_sidebar_state="collapsed")
NYC_TOKEN = "Uvxb0j9syjm3aI8h46DhQvnX5skN4aSUL0x_Ee3ty9M.ew0KICAiVmVyc2lvbiI6IDEsDQogICJOYW1lIjogIk5ZQyByZWFkIHRva2VuIDIwMTcxMDI2IiwNCiAgIkRhdGUiOiAiMjAxNy0xMC0yNlQxNjoyNjo1Mi42ODM0MDYtMDU6MDAiLA0KICAiV3JpdGUiOiBmYWxzZQ0KfQ"

def year_window(year):
    if year == "2024–present":
        return "2024-01-01", None
    y = int(year)
    return f"{y}-01-01", f"{y + 1}-01-01"

st.markdown("""
<style>
:root { --bg:#0a0f1c; --bg2:#0d1426; --surf:#121c33; --surf2:#16213b; --line:#243352;
        --ink:#e7eefb; --mut:#9fb2d0; --blue:#3b82f6; --cyan:#38bdf8; --teal:#2dd4bf; --green:#34d399; }
[data-testid="stSidebar"] { display:none !important; }
[data-testid="stSidebarCollapsedControl"] { display:none !important; }
.stApp { background: radial-gradient(1200px 600px at 80% -10%, #14233f 0%, var(--bg) 55%) fixed; color: var(--ink); }
.block-container { padding-top: 1.0rem; max-width: 1440px; }
body, .stMarkdown, p, span, label, div { color: var(--ink); }
.appbar { background: linear-gradient(110deg,#0b1226 0%,#16264a 50%,#1e3a8a 140%);
  color:#fff; border-radius:16px; padding:18px 22px; margin-bottom:14px;
  border:1px solid #24365e; box-shadow:0 10px 30px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);
  position:relative; overflow:hidden; }
.appbar:after { content:""; position:absolute; right:-30px; top:-70px; width:240px; height:240px;
  background:radial-gradient(circle, rgba(56,189,248,.18) 0%, rgba(56,189,248,0) 70%); }
.appbar-title { font-size:1.55rem; font-weight:800; letter-spacing:.3px; color:#fff; }
.appbar-sub { opacity:.85; font-size:.9rem; margin-top:3px; color:#cdd9f0; }
.livepill { position:absolute; top:18px; right:22px; background:rgba(52,211,153,.15);
  color:#6ee7b7; font-weight:700; font-size:.7rem; padding:4px 11px; border-radius:999px; letter-spacing:.6px;
  border:1px solid rgba(52,211,153,.35); }
div[data-testid="stMetric"] { background:linear-gradient(180deg,var(--surf) 0%,var(--bg2) 100%);
  border:1px solid var(--line); border-radius:14px; padding:14px 16px;
  box-shadow:0 4px 14px rgba(0,0,0,.35); }
div[data-testid="stMetricValue"] { color:var(--cyan); font-weight:800; }
div[data-testid="stMetricLabel"] { color:var(--mut); font-weight:600; }
.stTabs [data-baseweb="tab-list"] { gap:6px; flex-wrap:wrap; border-bottom:1px solid var(--line); }
.stTabs [data-baseweb="tab"] { background:var(--surf); border:1px solid var(--line); border-bottom:none;
  border-radius:11px 11px 0 0; padding:7px 14px; font-weight:600; color:var(--mut); }
.stTabs [aria-selected="true"] { background:linear-gradient(180deg,#1e40af,#1d4ed8) !important; color:#fff !important;
  border-color:#2a4fa0; box-shadow:0 0 0 1px rgba(56,189,248,.25) inset; }
.stButton>button { background:linear-gradient(180deg,#2563eb,#1d4ed8); color:#fff; border:1px solid #2a4fa0;
  border-radius:10px; font-weight:700; padding:.5rem 1.1rem; box-shadow:0 4px 14px rgba(37,99,235,.35); }
.stButton>button:hover { background:linear-gradient(180deg,#1d4ed8,#1e40af); color:#fff; }
.stDownloadButton>button { background:linear-gradient(180deg,#0d9488,#0f766e); color:#fff; border:1px solid #115e59;
  border-radius:10px; font-weight:700; }
[data-testid="stExpander"] { border:1px solid var(--line); border-radius:14px;
  background:linear-gradient(180deg,var(--surf) 0%,var(--bg2) 100%); box-shadow:0 4px 16px rgba(0,0,0,.35); }
[data-testid="stExpander"] summary { font-weight:700; color:var(--ink); }
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px; }
[data-baseweb="select"]>div, .stTextInput input, .stNumberInput input {
  background:var(--surf2) !important; color:var(--ink) !important; border-color:var(--line) !important; }
a { color:var(--cyan) !important; }
h1,h2,h3 { color:var(--ink); }
div[data-testid="stAlert"] { border-radius:12px; background:var(--surf); border:1px solid var(--line); color:var(--ink); }
hr { border-color:var(--line); }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="appbar">
  <div class="appbar-title">🗽 NYC Council Explorer</div>
  <div class="appbar-sub">Legislation · Sponsors · Committees · Hearings · Member dossiers — live from NYC Legistar</div>
  <span class="livepill">● LIVE</span>
</div>
""", unsafe_allow_html=True)

YEAR_OPTS = ["2026", "2025", "2024", "2023", "2022", "2024–present"]
SCOPES = ["All legislation (browse list)", "By a Council Member", "One specific bill"]

@st.cache_resource
def _client():
    return LegistarClient(token=NYC_TOKEN, pause=0.2)

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_detail(mid):
    c = _client(); out = {"sponsors": [], "histories": [], "attachments": [], "text": ""}
    for k, fn in [("sponsors", c.sponsors), ("histories", c.histories), ("attachments", c.attachments)]:
        try: out[k] = fn(mid)
        except Exception: pass
    try: out["text"] = c.text_plain(mid, None)
    except Exception: pass
    return out

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_hearings(frm, to):
    c = _client()
    flt = f"EventDate ge datetime'{frm}' and EventDate lt datetime'{to}'"
    r = [normalize_event(e) for e in c.events(flt)]
    r.sort(key=lambda x: x["_sortdate"])
    return r

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_agenda(event_id):
    items = _client().event_items(event_id); out = []
    for it in items:
        if not (it.get("EventItemTitle") or it.get("EventItemMatterFile")):
            continue
        out.append({"Seq": it.get("EventItemAgendaSequence"), "File": it.get("EventItemMatterFile", "") or "",
                    "Item": (it.get("EventItemTitle") or "").strip(),
                    "Action": it.get("EventItemActionName", "") or "", "Result": it.get("EventItemPassedFlagName", "") or ""})
    out.sort(key=lambda x: (x["Seq"] is None, x["Seq"] or 0))
    return out

@st.cache_data(ttl=86400, show_spinner=False)
def get_directory():
    try: return _client().council_members()
    except Exception: return []

# --- top control panel (replaces the old sidebar) ---
_dir = get_directory()
_pre_bundle = st.session_state.get("bundle")
with st.expander("⚙️  Data controls — choose what to load, then press Load", expanded=(_pre_bundle is None)):
    a1, a2, a3 = st.columns([1, 1.5, 1.1])
    year = a1.selectbox("Year", YEAR_OPTS, index=0)
    scope = a2.selectbox("Scope", SCOPES)
    include_sponsors = a3.checkbox("Include sponsors (slower)", value=False,
        help="On = pull who signed each bill so the list is searchable by member. Off loads much faster.")
    if scope == "By a Council Member":
        member_name = st.selectbox("Council Member", _dir) if _dir else st.text_input("Council Member", "Hanks")
    else:
        member_name = ""
    if scope == "One specific bill":
        _loaded_files = sorted({r["File"] for r in (_pre_bundle or {}).get("rows", []) if r.get("File")})
        if _loaded_files:
            bill_number = st.selectbox("Pick a bill", _loaded_files)
        else:
            bill_number = st.text_input("Bill number", "Int 0220-2026")
            st.caption("Tip: load **All legislation** once — then this becomes a dropdown of every bill.")
    else:
        bill_number = ""
    b1, b2, b3 = st.columns([1.3, 1.3, 1])
    add_ai = b1.checkbox("Add AI impact bullets", value=False)
    anthropic_key = b2.text_input("Anthropic key (optional)", "", type="password")
    b3.write(""); b3.write("")
    load = b3.button("⟳  Load data", type="primary")
    st.caption("First load of a full year reads live from NYC's servers (seconds–minutes); cached and instant after. "
               "Single-bill and member scopes are fastest.")

def _tag_rows(rows, text_map=None):
    for r in rows:
        t = keyword_tags(r, ((text_map or {}).get(r["MatterId"], "") or "")[:4000])
        r["Topic tags"] = t["Pillar Tags"]; r["Boroughs named"] = t["Boroughs Named"]; r["Pillar Tags"] = t["Pillar Tags"]
    return rows

@st.cache_data(ttl=1800, show_spinner=False)
def build_member_dossier(member, year):
    since, until = year_window(year)
    flt = {"since": since, "sponsor": member}
    if until: flt["until"] = until
    profile = {"name": "dossier", "filter": flt, "enrich": True, "text": False, "workers": 4, "impact": "keyword"}
    snap = Snapshot("/tmp/legistar_state.db"); old = snap.load()
    bundle = assemble(_client(), None, snap, old, profile)
    _tag_rows(bundle["rows"])
    return {"member": member, "rows": bundle["rows"], "stats": dossier_stats(bundle["rows"], member)}

@st.cache_data(ttl=86400, show_spinner=False)
def make_dossier_ai(member, stats, key):
    return AIImpact("claude-haiku-4-5-20251001", api_key=key).dossier(member, stats)

@st.cache_data(ttl=1800, show_spinner=False)
def run_pull(scope, year, include_sponsors, bill_number, member_name, add_ai, anthropic_key):
    since, until = year_window(year)
    profile = {"name": "web", "filter": {}, "enrich": True, "text": True, "workers": 4}
    if scope == "By a Council Member":
        flt = {"since": since, "sponsor": member_name.strip()}
        if until: flt["until"] = until
        profile["filter"] = flt; profile["text"] = False
    elif scope == "All legislation (browse list)":
        flt = {"since": since}
        if until: flt["until"] = until
        profile["filter"] = flt; profile["text"] = False
        if include_sponsors:
            profile["enrich"] = True; profile["sponsors_only"] = True; profile["workers"] = 6
        else:
            profile["enrich"] = False
    else:
        profile["filter"] = {"file": bill_number.strip()}
    profile["impact"] = "ai" if (add_ai and anthropic_key.strip()) else "keyword"
    if add_ai and anthropic_key.strip():
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key.strip()
    client = LegistarClient(token=NYC_TOKEN, pause=0.2)
    snap = Snapshot("/tmp/legistar_state.db"); old = snap.load()
    ai = AIImpact("claude-haiku-4-5-20251001") if profile["impact"] == "ai" else None
    bundle = assemble(client, ai, snap, old, profile)
    if scope == "All legislation (browse list)" and not bundle["rows"] and until:
        profile["filter"] = {"since": since}
        bundle = assemble(client, ai, snap, old, profile)
    _tag_rows(bundle["rows"], bundle["text_map"])
    snap.save(bundle["rows"]); build_workbook(bundle, "/tmp/legislation.xlsx")
    return bundle

with st.expander("🔧 Connection test — open this if a year won't load"):
    st.caption("Runs the exact queries the loader uses and shows what NYC Legistar returns, so we can see the problem.")
    if st.button("Run connection test"):
        c = _client()
        for label, flt in [("2026", "MatterIntroDate ge datetime'2026-01-01' and MatterIntroDate lt datetime'2027-01-01'"),
                           ("2025", "MatterIntroDate ge datetime'2025-01-01' and MatterIntroDate lt datetime'2026-01-01'"),
                           ("no filter (first page)", None)]:
            try:
                res = c.matters(flt)
                st.write(f"**{label}** → {len(res)} matters. Sample: " + ", ".join(m.get('MatterFile','?') for m in res[:6]))
            except Exception as e:
                st.error(f"**{label}** query failed: {type(e).__name__}: {e}")

if load:
    try:
        with st.spinner("Working... pulling from Legistar. Big years can take a moment."):
            _b = run_pull(scope, year, include_sponsors, bill_number, member_name, add_ai, anthropic_key)
        if not _b["rows"]:
            run_pull.clear()  # never trap a transient/empty result in the cache
        st.session_state["bundle"] = _b
        st.session_state["loaded_year"] = year
    except requests.exceptions.HTTPError as e:
        st.error(f"NYC API returned HTTP {getattr(e.response,'status_code','?')}: {(getattr(e.response,'text','') or '')[:400]}")
    except Exception as e:
        st.error(f"{type(e).__name__}: {e}")

bundle = st.session_state.get("bundle")
rows = bundle["rows"] if bundle else []
loaded_year = st.session_state.get("loaded_year", "")
if not bundle:
    st.info("**Open the ⚙️ Data controls panel above, pick a Year and Scope, then press _Load data_.**  \n"
            "Start with **All legislation** + **2026** to load every Introduction, Resolution, and Land Use item "
            "for the year — then use the **Legislation list** tab to search by number or word.")
elif not rows:
    st.warning(f"This load returned **0 bills**.  Filter used: `{bundle.get('_filter')}` · raw matches from "
               f"Legistar: **{bundle.get('_raw_count')}**.  Try another year, or clear filters in the controls panel.")
elif load:
    st.success(f"Loaded **{len(rows)}** items for **{loaded_year}**. Use the tabs below.")

t_list, t_hear, t_detail, t_members, t_dossier, t_compare, t_over, t_changes, t_about = st.tabs(
    ["📋 Legislation list", "📅 Hearings", "📄 Bill detail", "👤 Members", "📕 Dossier", "⚖️ Compare",
     "📊 Overview", "🔔 What changed", "ℹ️ About"])

def need_data():
    st.info("Load data from the ⚙️ controls panel above first (this tab uses that data).")

# ---------------- LEGISLATION LIST (Legistar-style master list + search) ----------------
with t_list:
    if not bundle:
        need_data()
    else:
        st.subheader(f"All legislation — {loaded_year}")
        st.caption("This is the full list (every type). Type a number like **220** to find that bill across all "
                   "types, or words like **ferry**, a committee, or a member's name. Leave blank to see everything.")
        q = st.text_input("Search")
        cc = st.columns(4)
        types = sorted({r["Type"] for r in rows if r["Type"]})
        pick_type = cc[0].multiselect("Type (Introduction / Resolution / Land Use…)", types)
        statuses = sorted({r["Status"] for r in rows if r["Status"]})
        pick_status = cc[1].multiselect("Status", statuses)
        topics = sorted({p for r in rows for p in (r.get("Topic tags") or "").split("; ") if p})
        pick_topic = cc[2].multiselect("Policy topic", topics)
        pick_bor = cc[3].multiselect("Borough named", ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"])
        sponsor_q = st.text_input("Signed on by (member name contains) — needs 'Include sponsors' or a member scope")
        f = [r for r in rows if matches_search(r, q)]
        if pick_type:   f = [r for r in f if r["Type"] in pick_type]
        if pick_status: f = [r for r in f if r["Status"] in pick_status]
        if pick_topic:  f = [r for r in f if any(p in (r.get("Topic tags") or "") for p in pick_topic)]
        if pick_bor:    f = [r for r in f if any(b in (r.get("Boroughs named") or "") for b in pick_bor)]
        if sponsor_q.strip():
            sq = sponsor_q.strip().lower()
            f = [r for r in f if any(sq in (n or "").lower() for n in r.get("_sponsor_names", []))
                 or sq in (r.get("Prime Sponsor", "") or "").lower()]
        st.caption(f"Showing {len(f)} of {len(rows)} bills")
        has_sp = any(r.get("_sponsor_names") for r in f)
        disp = []
        for r in f:
            d = {k: v for k, v in r.items() if not k.startswith("_") and k != "Pillar Tags"}
            if has_sp: d["All sponsors"] = "; ".join(r.get("_sponsor_names", []))
            disp.append(d)
        if disp:
            st.dataframe(pd.DataFrame(disp), use_container_width=True, height=520,
                column_config={"Web Link": st.column_config.LinkColumn("Legistar", display_text="Open")})

        with st.expander("🔬 Generate full analysis for these bills → Excel (uses your key, capped)"):
            st.caption("Runs the deep analysis on the filtered bills above and returns an Excel. Capped to keep "
                       "time and cost sane — filter the list first, then analyze.")
            ncap = st.number_input("How many of the filtered bills (max 15)", 1, 15, 5)
            if st.button("Generate analyses", key="batch_an"):
                if not anthropic_key.strip():
                    st.warning("Add your Anthropic key in the ⚙️ controls panel above.")
                else:
                    sub = f[:int(ncap)]
                    ai_an = AIImpact("claude-haiku-4-5-20251001", api_key=anthropic_key.strip())
                    out = []; prog = st.progress(0.0)
                    for i, rr in enumerate(sub):
                        try:
                            det = fetch_detail(rr["MatterId"])
                            rr2 = dict(rr); rr2["_sponsor_objs"] = current_sponsors({"MatterVersion": None}, det.get("sponsors", []))
                            anx = ai_an.analyze(rr2, det.get("text", ""), build_data_context(rr))
                        except Exception as e:
                            anx = f"(failed: {e})"
                        out.append({"File": rr["File"], "Title": rr["Title"], "Analysis": anx})
                        prog.progress((i + 1) / len(sub))
                    from openpyxl import Workbook
                    wb = Workbook(); ws = wb.active; ws.title = "Analysis"
                    ws.append(["File", "Title", "Analysis"])
                    for o in out:
                        ws.append([o["File"], o["Title"], o["Analysis"]])
                    wb.save("/tmp/analysis.xlsx"); st.session_state["analysis_xlsx"] = True
                    st.success(f"Analyzed {len(out)} bills.")
            if st.session_state.get("analysis_xlsx"):
                with open("/tmp/analysis.xlsx", "rb") as fh:
                    st.download_button("⬇️ Download analyses (Excel)", fh.read(), "bill_analyses.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------------- HEARINGS ----------------
with t_hear:
    st.subheader("Committee hearings & meetings — schedule")
    cda, cdb, cdc = st.columns([1, 1, 1])
    today = _dt.date.today()
    frm = cda.date_input("From", today); to = cdb.date_input("To", today + _dt.timedelta(days=60))
    cdc.write(""); cdc.write("")
    if cdc.button("Load hearings", type="primary"):
        try:
            with st.spinner("Loading hearings..."):
                st.session_state["hearings"] = fetch_hearings(str(frm), str(to))
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
    hev = st.session_state.get("hearings")
    if hev is None:
        st.info("Pick a date range and click **Load hearings** (defaults to the next 60 days).")
    elif not hev:
        st.warning("No hearings found in that range.")
    else:
        bodies = sorted({r["Committee / Body"] for r in hev if r["Committee / Body"]})
        pickb = st.multiselect("Filter by committee", bodies)
        show = [r for r in hev if not pickb or r["Committee / Body"] in pickb]
        st.caption(f"{len(show)} hearings")
        df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_") and k != "EventId"} for r in show])
        st.dataframe(df, use_container_width=True, height=420, column_config={
            "Legistar": st.column_config.LinkColumn("Meeting", display_text="Open"),
            "Agenda": st.column_config.LinkColumn("Agenda", display_text="PDF"),
            "Minutes": st.column_config.LinkColumn("Minutes", display_text="PDF")})
        st.divider()
        st.markdown("**See what's on a hearing's agenda (bills + outcomes):**")
        labels = {f"{r['Date']} — {r['Committee / Body']}": r["EventId"] for r in show}
        if labels:
            picklbl = st.selectbox("Pick a hearing", list(labels.keys()))
            with st.spinner("Loading agenda..."):
                ag = fetch_agenda(labels[picklbl])
            if ag:
                st.dataframe(pd.DataFrame(ag), hide_index=True, use_container_width=True)
            else:
                st.caption("No agenda items posted yet for this hearing.")

# ---------------- BILL DETAIL ----------------
with t_detail:
    if not bundle:
        need_data()
    else:
        pick = st.selectbox("Pick a bill", [r["File"] for r in rows], key="detail")
        r = next(x for x in rows if x["File"] == pick); mid = r["MatterId"]
        sp = r.get("_sponsor_objs", []); hi = bundle["histories_map"].get(mid, [])
        at = bundle["attach_map"].get(mid, []); tx = bundle["text_map"].get(mid, "")
        if not sp and not hi and not at:
            with st.spinner("Loading full details from Legistar..."):
                det = fetch_detail(mid)
            sp = current_sponsors({"MatterVersion": None}, det.get("sponsors", []))
            hi, at, tx = det.get("histories", []), det.get("attachments", []), det.get("text", "")
        st.markdown(f"### {r['File']} — {r['Type']}")
        st.markdown(f"**{r['Title']}**")
        d = st.columns(3)
        d[0].metric("Status", r["Status"] or "-")
        d[1].metric("Sponsors", len(sp) if sp else (r["Sponsors (#)"] if r["Sponsors (#)"] != "" else "-"))
        d[2].write("**Committee**"); d[2].write(r["Committee/Body"] or "-")
        st.markdown(f"[➡ Open on the official Legistar site]({r['Web Link']})")
        st.markdown(f"**Topic tags:** {r.get('Topic tags','') or '—'}  \n**Boroughs named:** {r.get('Boroughs named','')}")

        st.markdown("### 🔬 Full policy analysis")
        _an = st.session_state.get("analyses", {}).get(mid)
        if not _an and anthropic_key.strip():
            with st.spinner("Analyzing this bill (NYC Open Data + AI)..."):
                try:
                    r_an = dict(r); r_an["_sponsor_objs"] = sp
                    _an = AIImpact("claude-haiku-4-5-20251001", api_key=anthropic_key.strip()).analyze(r_an, tx, build_data_context(r))
                    st.session_state.setdefault("analyses", {})[mid] = _an
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")
        if _an:
            st.caption("Auto-generated for this bill, grounded in the bill text and any retrieved NYC Open Data. "
                       "Inference, not official — verify figures with OMB / IBO / agency sources.")
            st.markdown(_an)
            if st.button("↻ Regenerate analysis", key="an_btn"):
                st.session_state.get("analyses", {}).pop(mid, None); st.rerun()
        elif not anthropic_key.strip():
            st.info("➕ Add your **Anthropic API key** in the ⚙️ controls panel to auto-generate a full analysis "
                    "(what it does, who supports/opposes, political, district/borough/city, fiscal, why it exists, "
                    "what happens if passed) for every bill you open.")
        if sp:
            st.markdown("**Sponsors (signature order):**")
            st.dataframe(pd.DataFrame([{"#": s.get("MatterSponsorSequence"), "Sponsor": s.get("MatterSponsorName"),
                "Role": "Prime" if s.get("MatterSponsorSequence") == 0 else "Co-sponsor"} for s in sp]),
                hide_index=True, use_container_width=True)
        if hi:
            st.markdown("**Action history (incl. vote results):**")
            st.dataframe(pd.DataFrame([{"Date": _date(h.get("MatterHistoryActionDate")),
                "Action": (h.get("MatterHistoryActionName") or "").strip(),
                "By": h.get("MatterHistoryActionBodyName"), "Result": h.get("MatterHistoryPassedFlagName")}
                for h in sorted(hi, key=lambda x: x.get("MatterHistoryActionDate") or "")]),
                hide_index=True, use_container_width=True)
        if at:
            st.markdown("**Attachments:**")
            for a in at:
                st.markdown(f"- [{a.get('MatterAttachmentName')}]({a.get('MatterAttachmentHyperlink')})")
        ab = bundle.get("ai_map", {}).get(mid)
        if ab:
            st.markdown("**AI impact read (analysis, not official):**")
            st.write(f"- **Purpose:** {ab.get('purpose','')}")
            st.write(f"- **Who it affects:** {ab.get('affects','')}")
            st.write(f"- **District angle:** {ab.get('d49','')}")
        if tx:
            with st.expander("Full bill text"):
                st.text(tx)



# ---------------- MEMBERS ----------------
with t_members:
    if not bundle:
        need_data()
    elif not any(r.get("_sponsor_names") for r in rows):
        st.info("Sponsor data isn't loaded. Turn on **Include sponsors** in the ⚙️ controls panel, or use the "
                "**By a Council Member** scope, then reload.")
    else:
        mem = st.text_input("Council Member name (e.g., Hanks, Carr, Morano, Salaam)")
        if mem.strip():
            mb = member_bills(rows, mem); prime = member_prime_count(mb, mem)
            c = st.columns(3)
            c[0].metric("Bills (on)", len(mb)); c[1].metric("As prime", prime); c[2].metric("As co-sponsor", len(mb) - prime)
            pc = pillar_counts(mb)
            if pc:
                st.subheader("Their bills by policy topic"); st.bar_chart(pd.Series(pc).sort_values(ascending=False))
            df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_") and k != "Pillar Tags"} for r in mb])
            if not df.empty:
                st.dataframe(df, use_container_width=True, height=340,
                    column_config={"Web Link": st.column_config.LinkColumn("Legistar", display_text="Open")})
            last = mem.split()[-1] if mem.split() else mem
            coal = coalition_counts(mb, member=last)
            if coal:
                st.subheader(f"Who {mem} co-sponsors with most"); st.bar_chart(pd.Series(coal))

# ---------------- DOSSIER ----------------
with t_dossier:
    st.subheader("📕 Council Member dossier")
    st.caption("A profile of any member's record for the selected **Year** (controls panel), with optional AI analysis. "
               "Loads on its own.")
    members = get_directory()
    if not members:
        manual = st.text_input("Directory unavailable — type a member's last name", "Hanks")
        members = [manual.strip()] if manual.strip() else []
    who = st.selectbox("Council Member", members) if members else None
    run_ai = st.checkbox("Include AI analysis (uses the Anthropic key in the controls panel)", value=bool(anthropic_key.strip()))
    if who and st.button("Build dossier", type="primary"):
        with st.spinner(f"Scanning {who}'s {year} record (a few minutes; cached after)..."):
            try:
                dd = build_member_dossier(who, year)
                st.session_state["dossier"] = dd; st.session_state["dossier_ai"] = ""
                if run_ai and anthropic_key.strip():
                    with st.spinner("Writing AI analysis..."):
                        st.session_state["dossier_ai"] = make_dossier_ai(who, dd["stats"], anthropic_key.strip())
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
    dd = st.session_state.get("dossier")
    if dd:
        stats = dd["stats"]; mb = dd["rows"]; member = dd["member"]
        st.markdown(f"## {member}")
        c = st.columns(4)
        c[0].metric("Bills (on)", stats["bills_on"]); c[1].metric("As prime", stats["as_prime"])
        c[2].metric("Passed", stats["by_status"]["passed"]); c[3].metric("Alive", stats["by_status"]["alive"])
        cc = st.columns(2)
        if stats["by_topic"]:
            cc[0].subheader("Policy topics"); cc[0].bar_chart(pd.Series(stats["by_topic"]).sort_values(ascending=False))
        if stats["top_coalition"]:
            cc[1].subheader("Top coalition partners"); cc[1].bar_chart(pd.Series(stats["top_coalition"]))
        ai_txt = st.session_state.get("dossier_ai", "")
        if ai_txt:
            st.markdown("### 🧠 AI analysis")
            st.caption("Generated by AI from this member's public legislative record only. Inference, not official — "
                       "reflects sponsorship activity (not floor votes) and may be incomplete.")
            st.write(ai_txt)
        elif run_ai and not anthropic_key.strip():
            st.info("Add your Anthropic key in the ⚙️ controls panel to include the AI write-up.")
        st.markdown("### Prime-sponsored bills")
        pm = [r for r in mb if member.lower() in (r.get("Prime Sponsor", "") or "").lower()]
        df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_") and k != "Pillar Tags"} for r in pm])
        if not df.empty:
            st.dataframe(df, use_container_width=True, height=320,
                column_config={"Web Link": st.column_config.LinkColumn("Legistar", display_text="Open")})

# ---------------- COMPARE ----------------
with t_compare:
    if not bundle or not any(r.get("_sponsor_names") for r in rows):
        st.info("Turn on **Include sponsors** (or use a member scope) so two members can be compared.")
    else:
        c = st.columns(2)
        a = c[0].text_input("Member A", "Hanks"); b = c[1].text_input("Member B", "Carr")
        if a.strip() and b.strip():
            ma, mb = member_bills(rows, a), member_bills(rows, b)
            comp = pd.DataFrame({"Member": [a, b], "Bills (on)": [len(ma), len(mb)],
                "As prime": [member_prime_count(ma, a), member_prime_count(mb, b)],
                "Alive": [overview_general(ma)["alive"], overview_general(mb)["alive"]],
                "Passed": [overview_general(ma)["passed"], overview_general(mb)["passed"]],
                "Dead/filed": [overview_general(ma)["dead"], overview_general(mb)["dead"]]})
            st.dataframe(comp, hide_index=True, use_container_width=True)

# ---------------- OVERVIEW ----------------
with t_over:
    if not bundle:
        need_data()
    else:
        s = overview_general(rows)
        a = st.columns(4)
        a[0].metric("Bills loaded", s["total"]); a[1].metric("Alive (in committee)", s["alive"])
        a[2].metric("Passed", s["passed"]); a[3].metric("Dead / filed", s["dead"])
        tc = type_counts(rows)
        if tc:
            st.caption("By type: " + " · ".join(f"{k}: {v}" for k, v in sorted(tc.items(), key=lambda x: -x[1])))
        with open("/tmp/legislation.xlsx", "rb") as fh:
            st.download_button("⬇️ Download everything as Excel", fh.read(), "NYC_Council_legislation.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c1, c2 = st.columns(2)
        pc = pillar_counts(rows)
        if pc:
            c1.subheader("By policy topic"); c1.bar_chart(pd.Series(pc).sort_values(ascending=False))
        sc = status_counts(rows)
        if sc:
            c2.subheader("By status"); c2.bar_chart(pd.Series(sc))

# ---------------- WHAT CHANGED ----------------
with t_changes:
    if not bundle:
        need_data()
    else:
        st.subheader("Changes since the last load (while this app stays awake)")
        ch = bundle.get("changes", [])
        if ch:
            st.dataframe(pd.DataFrame(ch, columns=["File", "Change", "Field", "Was", "Now"]),
                         hide_index=True, use_container_width=True)
        else:
            st.info("No changes detected since the previous load in this session.")
        st.caption("For permanent daily tracking, the scheduled version (handed to Council IT) keeps a lasting history.")

# ---------------- ABOUT ----------------
with t_about:
    st.subheader("About this tool")
    st.markdown("""
**NYC Council Explorer** pulls live data from the NYC Council's official Legistar system:
- **Legislation list** — every bill for the chosen **year**, all types, searchable by number or word (Legistar-style).
- **Hearings** — committee meeting schedule, locations, agendas, and outcomes.
- **Bill detail** — sponsors, committee, status, history, attachments, full text.
- **Members & Dossiers** — any member's record by year, lead vs. co-sponsor, coalition, and an AI profile.

**Policy topics** are auto-tagged from each bill's text; bills are also flagged by the **boroughs** they name.

**About the AI dossier:** it analyzes only a member's public sponsorship record (not floor votes), is AI-generated,
and is labeled as inference — not an official statement.
""")
