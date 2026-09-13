#!/usr/bin/env python3
"""
The Councilmember's public record: video, hearings and press.

Everything the office might be asked about in public is collected here, so a
press question never has to be answered from memory:

* **Council hearing video** — Legistar carries the InSite page and the video
  path for every meeting, which is the authoritative record of what was said
  in a committee.
* **The Council's YouTube channel** — published as an RSS feed, which needs no
  API key and no quota. Items mentioning the Member are flagged.
* **News coverage** — Google News publishes an RSS feed per query, so
  "Kamillah Hanks" is a standing search with no key.
* **Council press releases** — the Council's own newsroom feed.

Nothing here invents a quote. A transcript is stored only when a real one is
supplied -- pasted by staff, or returned by a caption endpoint. Anything the
office attributes to the Member in public has to come from a clip with a URL
and a timestamp, and that is what this table stores.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Iterable

from ..core.config import MEMBER_NAME
from ..live.feeds import UA, get_json
from . import events, schema

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_media (
  id TEXT PRIMARY KEY,
  kind TEXT,            -- hearing | video | news | press | statement
  source TEXT,          -- LEGISTAR_EVENTS | YOUTUBE | NEWS | COUNCIL_PRESS
  title TEXT,
  url TEXT,
  outlet TEXT,
  committee TEXT,
  speakers TEXT,        -- who appears; the Member when detected
  published TEXT,
  duration TEXT,
  summary TEXT,
  transcript TEXT,      -- only ever a real transcript, never generated
  mentions_member INTEGER DEFAULT 0,
  matter_id INTEGER,
  fetched TEXT
);
CREATE INDEX IF NOT EXISTS ix_ws_media_kind ON ws_media(kind, published);
CREATE INDEX IF NOT EXISTS ix_ws_media_member ON ws_media(mentions_member);
"""

# Public feeds. None of these need an API key.
COUNCIL_YOUTUBE_CHANNELS = {
    # The Council's own channel. Overridable in settings if the id changes.
    "NYC Council": "UC-pTyDGjfM3wLOmH-Yk_-4Q",
}
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
NEWS_FEED = ("https://news.google.com/rss/search?q={query}"
             "&hl=en-US&gl=US&ceid=US:en")
COUNCIL_PRESS = "https://council.nyc.gov/press/feed/"

ATOM = {"a": "http://www.w3.org/2005/Atom",
        "media": "http://search.yahoo.com/mrss/",
        "yt": "http://www.youtube.com/xml/schemas/2015"}


def init(store) -> None:
    schema.apply(store.conn)
    store.conn.executescript(MEDIA_SCHEMA)
    store.conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_xml(url: str, timeout: int = 30) -> ET.Element | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ET.fromstring(resp.read())
    except Exception:
        return None


def _mentions(text: str, member: str = MEMBER_NAME) -> bool:
    """Does this item name the Member? Surname alone is enough in context."""
    low = (text or "").lower()
    last = member.split()[-1].lower()
    return member.lower() in low or f" {last}" in f" {low}"


def _save(store, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    store.upsert("ws_media", rows)
    return len(rows)


# ------------------------------------------------------------- collectors --
def collect_youtube(store, channels: dict | None = None,
                    member: str = MEMBER_NAME) -> dict:
    """Pull the Council's YouTube channel feed. No API key, no quota."""
    init(store)
    found, flagged, errors = 0, 0, {}
    for name, channel in (channels or COUNCIL_YOUTUBE_CHANNELS).items():
        root = _fetch_xml(YOUTUBE_FEED.format(channel=channel))
        if root is None:
            errors[name] = "feed unreachable"
            continue
        rows = []
        for entry in root.findall("a:entry", ATOM):
            vid = entry.findtext("yt:videoId", default="", namespaces=ATOM)
            title = entry.findtext("a:title", default="", namespaces=ATOM)
            published = entry.findtext("a:published", default="", namespaces=ATOM)
            group = entry.find("media:group", ATOM)
            desc = (group.findtext("media:description", default="", namespaces=ATOM)
                    if group is not None else "")
            hit = _mentions(f"{title} {desc}", member)
            flagged += 1 if hit else 0
            rows.append({
                "id": f"yt:{vid}", "kind": "video", "source": "YOUTUBE",
                "title": title[:400],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "outlet": name, "committee": "",
                "speakers": member if hit else "",
                "published": published[:25], "duration": "",
                "summary": (desc or "")[:4000], "transcript": "",
                "mentions_member": 1 if hit else 0, "matter_id": None,
                "fetched": now(),
            })
        found += _save(store, rows)
    events.emit(store, "media.youtube", "media", "youtube",
                f"YouTube: {found} videos, {flagged} mention {member}", "",
                {"errors": errors})
    return {"collected": found, "mentions_member": flagged, "errors": errors}


def collect_news(store, queries: Iterable[str] | None = None) -> dict:
    """Standing news searches. Google News publishes RSS per query, key-free."""
    init(store)
    queries = list(queries or [
        f'"{MEMBER_NAME}"',
        '"Staten Island" "City Council" budget',
        '"North Shore" Staten Island development',
    ])
    found, errors = 0, {}
    for q in queries:
        root = _fetch_xml(NEWS_FEED.format(query=urllib.parse.quote(q)))
        if root is None:
            errors[q] = "feed unreachable"
            continue
        rows = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            title = html.unescape(item.findtext("title") or "")
            src = item.find("source")
            rows.append({
                "id": "news:" + uuid.uuid5(uuid.NAMESPACE_URL, link or title).hex,
                "kind": "news", "source": "NEWS", "title": title[:400],
                "url": link, "outlet": (src.text if src is not None else "")[:120],
                "committee": "", "speakers": "",
                "published": (item.findtext("pubDate") or "")[:40],
                "duration": "",
                "summary": html.unescape(
                    re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))[:4000],
                "transcript": "",
                "mentions_member": 1 if _mentions(title) else 0,
                "matter_id": None, "fetched": now(),
            })
        found += _save(store, rows)
    events.emit(store, "media.news", "media", "news",
                f"News sweep: {found} items", "", {"queries": list(queries)})
    return {"collected": found, "queries": list(queries), "errors": errors}


def collect_press(store) -> dict:
    """The Council's own newsroom feed."""
    init(store)
    root = _fetch_xml(COUNCIL_PRESS)
    if root is None:
        return {"collected": 0, "error": "Council press feed unreachable"}
    rows = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        body = html.unescape(re.sub(r"<[^>]+>", " ",
                                    item.findtext("description") or ""))
        rows.append({
            "id": "press:" + uuid.uuid5(uuid.NAMESPACE_URL, link or title).hex,
            "kind": "press", "source": "COUNCIL_PRESS", "title": title[:400],
            "url": link, "outlet": "New York City Council", "committee": "",
            "speakers": MEMBER_NAME if _mentions(f"{title} {body}") else "",
            "published": (item.findtext("pubDate") or "")[:40], "duration": "",
            "summary": body[:4000], "transcript": "",
            "mentions_member": 1 if _mentions(f"{title} {body}") else 0,
            "matter_id": None, "fetched": now(),
        })
    n = _save(store, rows)
    events.emit(store, "media.press", "media", "press",
                f"Council press: {n} releases", "")
    return {"collected": n}


def collect_hearings(store, days: int = 60) -> dict:
    """Committee meetings from Legistar, with their video and agenda links.

    This is the authoritative record of what was said in committee, and the
    only video source the office should quote the Member from.
    """
    init(store)
    from ..live.feeds import upcoming_events
    res = upcoming_events(days=days, ttl=900)
    if not res.rows:
        return {"collected": 0, "error": res.error or "No events returned"}
    rows = []
    for e in res.rows:
        eid = e.get("EventId")
        body = e.get("EventBodyName") or ""
        rows.append({
            "id": f"event:{eid}", "kind": "hearing", "source": "LEGISTAR_EVENTS",
            "title": f"{body} — {e.get('EventDate', '')[:10]}".strip(" —"),
            "url": e.get("EventInSiteURL") or "",
            "outlet": "New York City Council", "committee": body[:200],
            "speakers": "", "published": (e.get("EventDate") or "")[:25],
            "duration": e.get("EventTime") or "",
            "summary": " ".join(filter(None, [
                e.get("EventLocation"), e.get("EventAgendaStatusName"),
                e.get("EventComment")]))[:4000],
            "transcript": "",
            "mentions_member": 0, "matter_id": None, "fetched": now(),
        })
    n = _save(store, rows)
    events.emit(store, "media.hearings", "media", "hearings",
                f"Legistar hearings: {n} in the next {days} days", "")
    return {"collected": n, "window_days": days}


def add_transcript(store, media_id: str, transcript: str,
                   speakers: str = "", actor: str = "") -> dict:
    """Attach a real transcript. Never generated -- pasted or captioned."""
    if not store.scalar("SELECT 1 FROM ws_media WHERE id=?", [media_id]):
        raise ValueError("That clip is not in the media library.")
    store.conn.execute(
        "UPDATE ws_media SET transcript=?, speakers=COALESCE(NULLIF(?,''),speakers), "
        "mentions_member=CASE WHEN ? THEN 1 ELSE mentions_member END WHERE id=?",
        (transcript[:200000], speakers[:400], 1 if _mentions(transcript) else 0,
         media_id))
    store.conn.commit()
    events.emit(store, "media.transcript", "media", media_id,
                "Transcript attached", actor)
    return {"ok": True, "chars": len(transcript)}


def add_clip(store, d: dict) -> dict:
    """Record a clip by hand: a local file, a TV segment, a radio hit."""
    title = str(d.get("title") or "").strip()
    if not title:
        raise ValueError("A clip needs a title.")
    row = {
        "id": d.get("id") or "clip:" + uuid.uuid4().hex,
        "kind": str(d.get("kind") or "statement")[:40],
        "source": str(d.get("source") or "MANUAL")[:40],
        "title": title[:400], "url": str(d.get("url") or "")[:500],
        "outlet": str(d.get("outlet") or "")[:120],
        "committee": str(d.get("committee") or "")[:200],
        "speakers": str(d.get("speakers") or MEMBER_NAME)[:400],
        "published": str(d.get("published") or "")[:40],
        "duration": str(d.get("duration") or "")[:40],
        "summary": str(d.get("summary") or "")[:4000],
        "transcript": str(d.get("transcript") or "")[:200000],
        "mentions_member": 1, "matter_id": None, "fetched": now(),
    }
    store.upsert("ws_media", [row])
    events.emit(store, "media.clip", "media", row["id"],
                f"Clip added: {title[:60]}", str(d.get("actor") or ""))
    return row


# ---------------------------------------------------------------- reading --
def library(store, kind: str = "", member_only: bool = False,
            limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM ws_media WHERE 1=1", []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if member_only:
        sql += " AND mentions_member = 1"
    try:
        return [dict(r) for r in store.q(
            sql + " ORDER BY published DESC, fetched DESC LIMIT ?", params + [limit])]
    except Exception:
        return []


def quotable(store, topic: str = "", limit: int = 25) -> list[dict]:
    """Clips with a real transcript -- the only things safe to quote from.

    A quote the office puts in a press release has to be traceable to a
    recording. Everything returned here has a URL and stored words; anything
    without a transcript is deliberately excluded.
    """
    init(store)
    sql = ("SELECT id, kind, title, url, published, committee, outlet, speakers, "
           "transcript FROM ws_media WHERE transcript != ''")
    params: list[Any] = []
    if topic:
        sql += " AND (transcript LIKE ? OR title LIKE ? OR summary LIKE ?)"
        params += [f"%{topic}%"] * 3
    rows = [dict(r) for r in store.q(sql + " ORDER BY published DESC LIMIT ?",
                                     params + [limit])]
    for r in rows:
        r["excerpts"] = _excerpts(r.pop("transcript") or "", topic)
    return rows


def _excerpts(transcript: str, topic: str, window: int = 320) -> list[str]:
    """Verbatim windows around the topic. The words are the transcript's."""
    if not transcript:
        return []
    if not topic:
        return [transcript[:window]]
    out, low, needle = [], transcript.lower(), topic.lower()
    start = 0
    while len(out) < 4:
        i = low.find(needle, start)
        if i < 0:
            break
        a = max(0, i - window // 2)
        out.append(("…" if a else "") + transcript[a:i + window // 2].strip() + "…")
        start = i + len(needle)
    return out


def coverage(store) -> dict:
    init(store)
    rows = [dict(r) for r in store.q(
        "SELECT kind, source, COUNT(*) AS n, "
        "SUM(mentions_member) AS about_member, "
        "SUM(CASE WHEN transcript != '' THEN 1 ELSE 0 END) AS with_transcript, "
        "MAX(published) AS newest FROM ws_media GROUP BY kind, source")]
    return {"by_kind": rows,
            "total": sum(r["n"] for r in rows),
            "quotable": sum(r["with_transcript"] or 0 for r in rows),
            "note": ("Only clips with a stored transcript can be quoted. "
                     "Collection needs network access to Legistar, YouTube and "
                     "the news feeds.")}


def refresh_all(store, days: int = 60) -> dict:
    """One sweep across every public source."""
    return {
        "hearings": collect_hearings(store, days=days),
        "youtube": collect_youtube(store),
        "news": collect_news(store),
        "press": collect_press(store),
        "coverage": coverage(store),
    }
