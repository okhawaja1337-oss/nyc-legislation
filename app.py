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

        def heavy(m):
            mid = m["MatterId"]
            if mid not in sponsors_map:
                sponsors_map[mid] = client.sponsors(mid)
            histories_map[mid] = client.histories(mid)
            attach_map[mid] = client.attachments(mid)
            if want_text:
                tx = client.text_plain(mid, m.get("MatterVersion"))
                if tx:
                    text_map[mid] = tx

        if targets:
            print(f"Pulling full details for {len(targets)} bills...")
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
            "changes": changes, "run_info": run_info, "impact_mode": impact_mode}


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



# ===========================================================================
# NYC COUNCIL EXPLORER (Streamlit)
# ===========================================================================
import streamlit as st
import pandas as pd
import datetime as _dt

st.set_page_config(page_title="NYC Council Explorer", layout="wide")
NYC_TOKEN = "Uvxb0j9syjm3aI8h46DhQvnX5skN4aSUL0x_Ee3ty9M.ew0KICAiVmVyc2lvbiI6IDEsDQogICJOYW1lIjogIk5ZQyByZWFkIHRva2VuIDIwMTcxMDI2IiwNCiAgIkRhdGUiOiAiMjAxNy0xMC0yNlQxNjoyNjo1Mi42ODM0MDYtMDU6MDAiLA0KICAiV3JpdGUiOiBmYWxzZQ0KfQ"

st.sidebar.title("⚙️ Load legislation")
SCOPES = ["All bills (fast list)", "By a Council Member", "One specific bill"]
scope = st.sidebar.selectbox("Scope", SCOPES)
member_name = st.sidebar.text_input("Council Member (for 'By a Council Member')", "Hanks")
bill_number = st.sidebar.text_input("Bill number (for 'One specific bill')", "Int 0220-2026")
since_date = st.sidebar.text_input("Introduced on/after (YYYY-MM-DD)", "2024-01-01")
add_ai = st.sidebar.checkbox("Add AI impact bullets", value=False)
anthropic_key = st.sidebar.text_input("Anthropic key (optional)", "", type="password")
load = st.sidebar.button("Load legislation", type="primary")
st.sidebar.caption("**Fast list** = all bills/types in seconds. **By a Council Member** scans for that member's "
                   "bills. **Hearings** and **Dossier** tabs load on their own.")

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

@st.cache_data(ttl=1800, show_spinner=False)
def build_member_dossier(member, since):
    profile = {"name": "dossier", "filter": {"since": since, "sponsor": member},
               "enrich": True, "text": False, "workers": 3, "impact": "keyword"}
    snap = Snapshot("/tmp/legistar_state.db"); old = snap.load()
    bundle = assemble(_client(), None, snap, old, profile)
    for r in bundle["rows"]:
        t = keyword_tags(r, "")
        r["Topic tags"] = t["Pillar Tags"]; r["Pillar Tags"] = t["Pillar Tags"]; r["Boroughs named"] = t["Boroughs Named"]
    return {"member": member, "rows": bundle["rows"], "stats": dossier_stats(bundle["rows"], member)}

@st.cache_data(ttl=86400, show_spinner=False)
def make_dossier_ai(member, stats, key):
    return AIImpact("claude-haiku-4-5-20251001", api_key=key).dossier(member, stats)

@st.cache_data(ttl=1800, show_spinner=False)
def run_pull(scope, bill_number, member_name, since_date, add_ai, anthropic_key):
    profile = {"name": "web", "filter": {}, "enrich": True, "text": True, "workers": 3}
    if scope == "By a Council Member":
        profile["filter"] = {"since": since_date, "sponsor": member_name.strip()}; profile["text"] = False
    elif scope == "All bills (fast list)":
        profile["filter"] = {"since": since_date}; profile["enrich"] = False; profile["text"] = False
    else:
        profile["filter"] = {"file": bill_number.strip()}
    profile["impact"] = "ai" if (add_ai and anthropic_key.strip()) else "keyword"
    if add_ai and anthropic_key.strip():
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key.strip()
    client = LegistarClient(token=NYC_TOKEN, pause=0.2)
    snap = Snapshot("/tmp/legistar_state.db"); old = snap.load()
    ai = AIImpact("claude-haiku-4-5-20251001") if profile["impact"] == "ai" else None
    bundle = assemble(client, ai, snap, old, profile)
    for r in bundle["rows"]:
        t = keyword_tags(r, (bundle["text_map"].get(r["MatterId"], "") or "")[:4000])
        r["Topic tags"] = t["Pillar Tags"]; r["Boroughs named"] = t["Boroughs Named"]; r["Pillar Tags"] = t["Pillar Tags"]
    snap.save(bundle["rows"]); build_workbook(bundle, "/tmp/legislation.xlsx")
    return bundle

if load:
    try:
        with st.spinner("Working... please wait."):
            st.session_state["bundle"] = run_pull(scope, bill_number, member_name, since_date, add_ai, anthropic_key)
    except requests.exceptions.HTTPError as e:
        st.error(f"NYC API returned HTTP {getattr(e.response,'status_code','?')}: {(getattr(e.response,'text','') or '')[:400]}")
    except Exception as e:
        st.error(f"{type(e).__name__}: {e}")

st.title("🗽 NYC Council Explorer")
st.caption("Live legislation, sponsors, committees, hearings, and member dossiers - straight from NYC's Legistar.")

t_hear, t_search, t_detail, t_members, t_dossier, t_compare, t_over, t_changes, t_about = st.tabs(
    ["📅 Hearings", "🔎 Search bills", "📄 Bill detail", "👤 Members", "📕 Dossier", "⚖️ Compare",
     "📊 Overview", "🔔 What changed", "ℹ️ About"])

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

bundle = st.session_state.get("bundle")
rows = bundle["rows"] if bundle else []

def need_data():
    st.info("Load legislation from the sidebar first (this tab uses that data).")

# ---------------- SEARCH ----------------
with t_search:
    if not bundle:
        need_data()
    else:
        st.caption("Type a number like **220** to find that bill across all types, or words like **ferry**, a committee, or a member's name.")
        q = st.text_input("Search")
        cc = st.columns(4)
        types = sorted({r["Type"] for r in rows if r["Type"]})
        pick_type = cc[0].multiselect("Type (Introduction / Resolution / Land Use…)", types)
        statuses = sorted({r["Status"] for r in rows if r["Status"]})
        pick_status = cc[1].multiselect("Status", statuses)
        topics = sorted({p for r in rows for p in (r.get("Topic tags") or "").split("; ") if p})
        pick_topic = cc[2].multiselect("Policy topic", topics)
        pick_bor = cc[3].multiselect("Borough named", ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"])
        sponsor_q = st.text_input("Signed on by (member name contains)")
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
            st.dataframe(pd.DataFrame(disp), use_container_width=True, height=460,
                column_config={"Web Link": st.column_config.LinkColumn("Legistar", display_text="Open")})

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
        st.info("Sponsor data isn't loaded. Use the **By a Council Member** scope in the sidebar.")
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
    st.caption("A profile of any member's legislative record, with optional AI analysis. Loads on its own (doesn't need the sidebar pull).")
    members = get_directory()
    if not members:
        manual = st.text_input("Directory unavailable — type a member's last name", "Hanks")
        members = [manual.strip()] if manual.strip() else []
    who = st.selectbox("Council Member", members) if members else None
    run_ai = st.checkbox("Include AI analysis (uses the Anthropic key in the sidebar)", value=bool(anthropic_key.strip()))
    if who and st.button("Build dossier", type="primary"):
        with st.spinner(f"Scanning {who}'s record — this scans the session (a few minutes; cached after)..."):
            try:
                dd = build_member_dossier(who, since_date)
                st.session_state["dossier"] = dd
                st.session_state["dossier_ai"] = ""
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
            st.info("Add your Anthropic key in the sidebar to include the AI write-up.")
        st.markdown("### Prime-sponsored bills")
        pm = [r for r in mb if member.lower() in (r.get("Prime Sponsor", "") or "").lower()]
        df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_") and k != "Pillar Tags"} for r in pm])
        if not df.empty:
            st.dataframe(df, use_container_width=True, height=320,
                column_config={"Web Link": st.column_config.LinkColumn("Legistar", display_text="Open")})

# ---------------- COMPARE ----------------
with t_compare:
    if not bundle or not any(r.get("_sponsor_names") for r in rows):
        st.info("Load a scope with sponsor data (By a Council Member, or open bills) to compare members.")
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
            st.caption("Both members must appear in the loaded data. For a full compare, load a broad set.")

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
- **Hearings** — committee meeting schedule, locations, agendas, and outcomes.
- **Bills** — every type, with sponsors, committee, status, history, attachments, and full text.
- **Members & Dossiers** — any member's record, what they lead vs. co-sponsor, coalition, and an AI profile.

**Policy topics** are auto-tagged from each bill's text (Arts & Culture, Neighborhood Development, Economic
Development, Public Safety/Crisis, Health & Hospitals); bills are also flagged by the **boroughs** they name.

**About the AI dossier:** it analyzes only a member's public sponsorship record (not floor votes), is generated by
AI, and is labeled as inference — not an official statement. Keyword lists live at the top of the code and can be expanded.
""")
