#!/usr/bin/env python3
"""
Build the search index.

Everything the lake holds is flattened into one wide row per record so a
single query can reach a bill, a funding line, an organization, a hearing, a
contact, a media clip, a staff note and a task at once -- and so the facet
counts beside the filters are computed from the same table.

Indexing is a bulk operation: fifty thousand funding rows are written in one
transaction with the FTS index populated alongside. A full rebuild of the
whole corpus takes seconds, which is what makes the search box feel instant
afterwards.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..core.config import PILLARS
from . import events, schema

BATCH = 2000


def _pillar_label(key: str | None) -> str:
    if not key:
        return ""
    return PILLARS.get(key, {}).get("label", str(key).replace("_", " ").title())


def _clean(v: Any) -> str:
    return "" if v is None else str(v)


# ------------------------------------------------------------- extractors --
def _matters(store) -> Iterable[dict]:
    for r in store.q("""
        SELECT m.*, mb.name AS prime_name
        FROM matters m LEFT JOIN members mb ON mb.person_id = m.prime_id"""):
        pillars = json.loads(r["pillars"] or "[]")
        topics = json.loads(r["topics"] or "[]")
        yield {
            "key": f"matter:{r['matter_id']}", "kind": "matter",
            "entity_id": str(r["matter_id"]),
            "title": f"{r['file'] or ''} — {r['name'] or ''}".strip(" —"),
            "body": " ".join(filter(None, [
                r["name"], r["file"], r["committee"], r["status"],
                r["prime_name"], " ".join(str(t) for t in topics),
                " ".join(_pillar_label(p) for p in pillars)])),
            "year": r["year"], "fy": None, "status": r["status"],
            "committee": r["committee"], "sponsor": r["prime_name"],
            "agency": "", "channel": r["type"], "district": None,
            "source_id": r["source_id"] or "LEGISTAR",
            "url": (f"https://legistar.council.nyc.gov/LegislationDetail.aspx"
                    f"?ID={r['matter_id']}"),
            "updated": r["updated"] or "", "amount": None, "org": "",
            "pillar": _pillar_label(pillars[0] if pillars else None),
            "ein": "",
            "detail": json.dumps({"n_sponsors": r["n_sponsors"],
                                  "pass_prob": r["pass_prob"],
                                  "enacted": r["enacted"],
                                  "local_law": r["local_law"],
                                  "pending": r["pending"]}),
        }


def _funding(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM funding"):
        org = (r["org"] or "").split(" - ")[0]
        yield {
            "key": f"funding:{r['line_id']}", "kind": "funding",
            "entity_id": r["line_id"],
            "title": f"{org} — {_clean(r['pot'])} (FY{r['fy']})".strip(" —"),
            "body": " ".join(filter(None, [
                r["org"], r["pot"], r["purpose"], r["agency"], r["member"],
                r["program"], r["section"], r["ein"], r["status"]])),
            "year": r["fy"], "fy": r["fy"], "status": r["status"],
            "committee": "", "sponsor": r["member"], "agency": r["agency"],
            "channel": r["channel"], "district": r["district"],
            "source_id": r["source_id"], "url": "",
            "updated": r["updated"] or "", "amount": r["amount"],
            "org": org, "pillar": _pillar_label(r["pillar"]),
            "ein": r["ein"] or "",
            "detail": json.dumps({"pot": r["pot"], "tier": r["tier"],
                                  "reso": r["reso"], "locator": r["locator"],
                                  "in_d49": r["in_d49"], "mocs_id": r["mocs_id"],
                                  "analyst": r["analyst"]}),
        }


def _orgs(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM orgs"):
        yield {
            "key": f"org:{r['org_key']}", "kind": "org",
            "entity_id": r["org_key"], "title": r["name"] or "",
            "body": " ".join(filter(None, [r["name"], r["ein"], r["borough"],
                                           r["zip"], r["pillars"]])),
            "year": r["last_fy"], "fy": r["last_fy"], "status": "",
            "committee": "", "sponsor": "", "agency": "", "channel": "",
            "district": 49 if r["in_d49"] else None, "source_id": "SCHEDULE_C",
            "url": (f"https://projects.propublica.org/nonprofits/search?q="
                    f"{(r['ein'] or '').replace('-', '')}" if r["ein"] else ""),
            "updated": "", "amount": r["total_awarded"], "org": r["name"] or "",
            "pillar": "", "ein": r["ein"] or "",
            "detail": json.dumps({"n_awards": r["n_awards"],
                                  "first_fy": r["first_fy"],
                                  "last_fy": r["last_fy"],
                                  "in_d49": r["in_d49"]}),
        }


def _members(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM members WHERE current = 1"):
        yield {
            "key": f"member:{r['person_id']}", "kind": "member",
            "entity_id": str(r["person_id"]), "title": r["name"] or "",
            "body": " ".join(filter(None, [r["name"], r["party"], r["borough"],
                                           r["leadership"], str(r["district"])])),
            "year": None, "fy": None, "status": r["party"], "committee": "",
            "sponsor": r["name"], "agency": "", "channel": "",
            "district": r["district"], "source_id": "COUNCIL_RECORD",
            "url": r["wiki"] or "", "updated": "", "amount": None,
            "org": "", "pillar": "", "ein": "",
            "detail": json.dumps({"party": r["party"], "borough": r["borough"],
                                  "leadership": r["leadership"]}),
        }


def _calendar(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM calendar"):
        yield {
            "key": f"calendar:{r['event_id']}", "kind": "calendar",
            "entity_id": r["event_id"], "title": r["summary"] or "",
            "body": " ".join(filter(None, [r["summary"], r["location"],
                                           r["kind"], r["owners"]])),
            "year": int((r["start"] or "0000")[:4] or 0) or None, "fy": None,
            "status": r["kind"], "committee": "", "sponsor": "", "agency": "",
            "channel": r["kind"], "district": 49, "source_id": "D49_CALENDAR",
            "url": r["link"] or "", "updated": r["updated"] or "",
            "amount": None, "org": "", "pillar": _pillar_label(r["pillar"]),
            "ein": "",
            "detail": json.dumps({"start": r["start"], "end": r["end"],
                                  "location": r["location"],
                                  "owners": r["owners"]}),
        }


def _contacts(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM contacts"):
        yield {
            "key": f"contact:{r['contact_id']}", "kind": "contact",
            "entity_id": r["contact_id"],
            "title": " — ".join(filter(None, [r["agency"], r["office"],
                                              r["person"]])),
            "body": " ".join(filter(None, [r["agency"], r["office"], r["person"],
                                           r["title"], r["service_area"],
                                           r["phone"], r["email"],
                                           r["address"]])),
            "year": None, "fy": None, "status": r["level"], "committee": "",
            "sponsor": "", "agency": r["agency"], "channel": r["level"],
            "district": r["district"], "source_id": r["source_id"] or "GREENBOOK",
            "url": r["url"] or "", "updated": r["updated"] or "",
            "amount": None, "org": r["agency"] or "", "pillar": "", "ein": "",
            "detail": json.dumps({"phone": r["phone"], "email": r["email"],
                                  "service_area": r["service_area"]}),
        }


def _deliverables(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM deliverables"):
        yield {
            "key": f"deliverable:{r['deliverable_id']}", "kind": "deliverable",
            "entity_id": r["deliverable_id"], "title": r["subject"] or "",
            "body": (r["body"] or "")[:20000],
            "year": int((r["created"] or "0000")[:4] or 0) or None, "fy": None,
            "status": r["status"], "committee": "", "sponsor": "", "agency": "",
            "channel": r["kind"], "district": 49, "source_id": "D49_WORKSPACE",
            "url": "", "updated": r["updated"] or "", "amount": None,
            "org": "", "pillar": "", "ein": "", "detail": r["meta"] or "{}",
        }


def _media(store) -> Iterable[dict]:
    try:
        rows = store.q("SELECT * FROM ws_media")
    except Exception:
        return
    for r in rows:
        yield {
            "key": f"media:{r['id']}", "kind": "media", "entity_id": r["id"],
            "title": r["title"] or "",
            "body": " ".join(filter(None, [r["title"], r["summary"],
                                           r["transcript"], r["speakers"],
                                           r["outlet"]])),
            "year": int((r["published"] or "0000")[:4] or 0) or None, "fy": None,
            "status": r["kind"], "committee": r["committee"] or "",
            "sponsor": r["speakers"] or "", "agency": "", "channel": r["source"],
            "district": 49, "source_id": r["source"] or "MEDIA",
            "url": r["url"] or "", "updated": r["fetched"] or "",
            "amount": None, "org": r["outlet"] or "", "pillar": "", "ein": "",
            "detail": json.dumps({"published": r["published"],
                                  "duration": r["duration"],
                                  "has_transcript": bool(r["transcript"])}),
        }


def _tasks(store) -> Iterable[dict]:
    for r in store.q("SELECT * FROM workspace_tasks"):
        yield {
            "key": f"task:{r['id']}", "kind": "task", "entity_id": r["id"],
            "title": r["title"] or "",
            "body": " ".join(filter(None, [r["title"], r["description"],
                                           r["owner"], r["blocker"],
                                           r["outcome"], r["tags"]])),
            "year": int((r["created"] or "0000")[:4] or 0) or None, "fy": None,
            "status": r["status"], "committee": "", "sponsor": r["owner"],
            "agency": "", "channel": "task", "district": 49,
            "source_id": "D49_WORKSPACE", "url": r["link"] or "",
            "updated": r["updated"] or "", "amount": None, "org": "",
            "pillar": "", "ein": "",
            "detail": json.dumps({"priority": r["priority"], "due": r["due"],
                                  "project_id": r["project_id"]}),
        }


SOURCES = {
    "matters": _matters, "funding": _funding, "orgs": _orgs,
    "members": _members, "calendar": _calendar, "contacts": _contacts,
    "deliverables": _deliverables, "media": _media, "tasks": _tasks,
}

COLUMNS = ("key", "kind", "entity_id", "title", "body", "year", "fy", "status",
           "committee", "sponsor", "agency", "channel", "district",
           "source_id", "url", "updated", "amount", "org", "pillar", "ein",
           "detail")


# -------------------------------------------------------------- rebuilding --
def rebuild(store, only: Iterable[str] | None = None,
            progress=None) -> dict:
    """Rebuild the index. Bulk-loaded in batches inside one transaction each."""
    schema.apply(store.conn)
    wanted = list(only) if only else list(SOURCES)
    counts: dict[str, int] = {}

    for name in wanted:
        extract = SOURCES.get(name)
        if not extract:
            continue
        try:
            rows = list(extract(store))
        except Exception as exc:            # a table that is not loaded yet
            counts[name] = 0
            if progress:
                progress(f"{name}: skipped ({type(exc).__name__})")
            continue

        with store.tx() as c:
            c.execute("DELETE FROM workspace_fts WHERE key IN "
                      "(SELECT key FROM workspace_records WHERE kind = ?)",
                      (rows[0]["kind"],) if rows else ("__none__",))
            c.execute("DELETE FROM workspace_records WHERE kind = ?",
                      (rows[0]["kind"],) if rows else ("__none__",))
            for i in range(0, len(rows), BATCH):
                chunk = rows[i:i + BATCH]
                c.executemany(
                    f"INSERT OR REPLACE INTO workspace_records ({','.join(COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(COLUMNS))})",
                    [[r.get(k) for k in COLUMNS] for r in chunk])
                c.executemany(
                    "INSERT INTO workspace_fts (key, title, body) VALUES (?,?,?)",
                    [[r["key"], r.get("title") or "", r.get("body") or ""]
                     for r in chunk])
        counts[name] = len(rows)
        if progress:
            progress(f"{name}: {len(rows):,}")

    store.conn.execute("INSERT INTO workspace_fts(workspace_fts) VALUES('optimize')")
    store.conn.commit()
    total = sum(counts.values())
    events.emit(store, "index.rebuilt", "index", "workspace",
                f"Search index rebuilt: {total:,} records", "", counts)
    return {"indexed": total, "by_source": counts}


def index_one(store, kind: str, row: dict) -> None:
    """Index a single record immediately, so a new task is searchable at once."""
    payload = {k: row.get(k) for k in COLUMNS}
    payload["kind"] = kind
    with store.tx() as c:
        c.execute("DELETE FROM workspace_fts WHERE key = ?", (payload["key"],))
        c.execute(f"INSERT OR REPLACE INTO workspace_records ({','.join(COLUMNS)}) "
                  f"VALUES ({','.join('?' * len(COLUMNS))})",
                  [payload.get(k) for k in COLUMNS])
        c.execute("INSERT INTO workspace_fts (key, title, body) VALUES (?,?,?)",
                  (payload["key"], payload.get("title") or "",
                   payload.get("body") or ""))


def coverage(store) -> list[dict]:
    """What is indexed, by kind -- the honest answer to 'is it all in there?'"""
    return [dict(r) for r in store.q(
        "SELECT kind, COUNT(*) AS n, MIN(fy) AS first_fy, MAX(fy) AS last_fy, "
        "SUM(CASE WHEN amount IS NOT NULL THEN amount ELSE 0 END) AS total "
        "FROM workspace_records GROUP BY kind ORDER BY n DESC")]
