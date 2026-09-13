#!/usr/bin/env python3
"""
Transcripts: Council video and hearing captions.

A quote is the one deliverable this system refuses to generate. If the office
puts words in the Councilmember's mouth and a reporter checks the tape, that
is the story. So the rule is absolute: a quote must come from a transcript,
and a transcript must come from the tape.

This module is how the tape gets here. It finds the video, pulls its captions,
parses them into timestamped cues, and stores both the flat text (for search)
and the cues (so every quote carries a deep link to the second it was said).
A cited quote a reporter can click is worth more than a polished one nobody
can verify.

Caption transports, in order:

  1. ``timedtext``  -- YouTube's own caption endpoint. No key.
  2. ``innertube``  -- the player endpoint the web client itself calls, which
                       lists the caption tracks. No key.
  3. ``provider``   -- a third-party captions API, for when YouTube throttles
                       datacentre traffic. Keyed, and the URL is configurable
                       so the office is not locked to one vendor.
  4. ``sidecar``    -- a .vtt/.srt published next to hearing video (Granicus
                       and Legistar both do this).
  5. ``file``       -- a caption file dropped in by hand.

Auto-generated captions are marked as such. "Uh" and a mis-heard surname are
not a quote, and a draft that says where the text came from lets a press
secretary make that call instead of discovering it later.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..core import keys
from ..core.store import Store

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEDTEXT = "https://www.youtube.com/api/timedtext"
INNERTUBE = "https://www.youtube.com/youtubei/v1/player"
# The web client's public InnerTube key. Not a secret; it ships in the page.
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
WATCH = "https://www.youtube.com/watch?v={vid}&t={sec}s"
TIMEOUT = 45

# Third-party captions APIs, by the name put in `captions_provider`. Each is a
# URL template plus where the key goes and where the text comes back.
PROVIDERS: dict[str, dict] = {
    "supadata": {
        "url": "https://api.supadata.ai/v1/youtube/transcript?videoId={vid}&text=false",
        "header": "x-api-key", "path": ("content",),
        "cue": {"text": "text", "start": "offset", "dur": "duration", "scale": 0.001},
    },
    "youtube-transcript-io": {
        "url": "https://www.youtube-transcript.io/api/transcripts?id={vid}",
        "header": "Authorization", "prefix": "Basic ",
        "path": ("0", "tracks", "0", "transcript"),
        "cue": {"text": "text", "start": "start", "dur": "dur", "scale": 1.0},
    },
    "searchapi": {
        "url": ("https://www.searchapi.io/api/v1/search?engine=youtube_transcripts"
                "&video_id={vid}&api_key={key}"),
        "path": ("transcripts",),
        "cue": {"text": "text", "start": "start", "dur": "duration", "scale": 1.0},
    },
}

CUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_transcript_cues (
  id TEXT PRIMARY KEY,
  media_id TEXT,
  seq INTEGER,
  start_s REAL,
  end_s REAL,
  speaker TEXT,
  text TEXT
);
CREATE INDEX IF NOT EXISTS ix_cues_media ON ws_transcript_cues(media_id, seq);
CREATE INDEX IF NOT EXISTS ix_cues_start ON ws_transcript_cues(media_id, start_s);
"""


def init(store: Store) -> None:
    """Cues hang off a media row, so the media table has to exist first."""
    from ..workspace import media as media_mod
    media_mod.init(store)
    store.conn.executescript(CUE_SCHEMA)
    store.conn.commit()


def _get(url: str, headers: dict | None = None, data: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:500]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- parsers ----
CLOCK = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
SPEAKER = re.compile(r"^(?:>>\s*)?([A-Z][A-Za-z.'\- ]{2,40}):\s+")
TAG = re.compile(r"<[^>]+>")


def _seconds(stamp: str) -> float:
    m = CLOCK.search(stamp or "")
    if not m:
        return 0.0
    hours, minutes, secs, frac = m.groups()
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
            + int(frac.ljust(3, "0")) / 1000.0)


def parse_vtt(text: str) -> list[dict]:
    """WebVTT and SRT. They differ only in a header and a cue number."""
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if lines[0].strip().isdigit() and len(lines) > 1:   # SRT cue number
            lines = lines[1:]
        if "-->" not in lines[0]:
            continue
        left, _, right = lines[0].partition("-->")
        body = " ".join(TAG.sub("", l).strip() for l in lines[1:])
        body = html.unescape(body).strip()
        if not body:
            continue
        cues.append({"start_s": _seconds(left), "end_s": _seconds(right), "text": body})
    return _dedupe(cues)


def parse_timedtext_xml(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    cues = []
    for node in root.iter("text"):
        start = float(node.get("start") or 0)
        dur = float(node.get("dur") or 0)
        body = html.unescape(TAG.sub("", node.text or "")).strip()
        if body:
            cues.append({"start_s": start, "end_s": start + dur, "text": body})
    return _dedupe(cues)


def parse_json3(text: str) -> list[dict]:
    """YouTube's json3 caption format: events carrying per-word segments."""
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    cues = []
    for event in payload.get("events", []):
        segs = event.get("segs") or []
        body = "".join(s.get("utf8", "") for s in segs).strip()
        if not body or body == "\n":
            continue
        start = (event.get("tStartMs") or 0) / 1000.0
        cues.append({"start_s": start,
                     "end_s": start + (event.get("dDurationMs") or 0) / 1000.0,
                     "text": html.unescape(body)})
    return _dedupe(cues)


def _dedupe(cues: list[dict]) -> list[dict]:
    """
    Rolling auto-captions repeat the previous line as they build a sentence.
    Left alone, a 40-minute hearing becomes 300KB of the same phrase and every
    keyword search matches it six times.
    """
    out: list[dict] = []
    for cue in cues:
        if out and cue["text"] == out[-1]["text"]:
            out[-1]["end_s"] = max(out[-1]["end_s"], cue["end_s"])
            continue
        # A cue that merely extends the one before it is the same sentence
        # still being typed. Keeping the longer text loses nothing but the
        # intermediate timing; keeping both triples the transcript and makes
        # every keyword match six times. The floor only guards against
        # collapsing a one-word utterance into the reply that follows it.
        if out and cue["text"].startswith(out[-1]["text"]) and len(out[-1]["text"]) >= 6:
            out[-1] = {**cue, "start_s": out[-1]["start_s"]}
            continue
        out.append(dict(cue))
    for index, cue in enumerate(out):
        cue["seq"] = index
        cue["speaker"] = None
        m = SPEAKER.match(cue["text"])
        if m:
            cue["speaker"] = m.group(1).strip()
            cue["text"] = cue["text"][m.end():].strip()
    return [c for c in out if c["text"]]


def parse_any(text: str) -> list[dict]:
    head = (text or "").lstrip()[:200]
    if head.startswith("{"):
        return parse_json3(text)
    if head.startswith("<"):
        return parse_timedtext_xml(text)
    return parse_vtt(text)


# ------------------------------------------------------------- transports ----
def _from_timedtext(vid: str, lang: str = "en") -> dict:
    for params in ({"lang": lang, "v": vid, "fmt": "json3"},
                   {"lang": lang, "v": vid},
                   {"lang": lang, "v": vid, "kind": "asr", "fmt": "json3"}):
        status, body = _get(f"{TIMEDTEXT}?{urllib.parse.urlencode(params)}")
        if status == 200 and body.strip():
            cues = parse_any(body)
            if cues:
                return {"ok": True, "cues": cues,
                        "auto": params.get("kind") == "asr", "status": status}
    return {"ok": False, "status": status, "detail": (body or "")[:200]}


def _from_innertube(vid: str, lang: str = "en") -> dict:
    """Ask the player endpoint for the caption tracks, then fetch one."""
    body = json.dumps({
        "videoId": vid,
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240401.00.00"}},
    }).encode()
    status, raw = _get(f"{INNERTUBE}?key={INNERTUBE_KEY}",
                       {"Content-Type": "application/json"}, body)
    if status != 200:
        return {"ok": False, "status": status, "detail": raw[:200]}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"ok": False, "status": status, "detail": "unparseable player response"}
    tracks = (payload.get("captions", {})
              .get("playerCaptionsTracklistRenderer", {}).get("captionTracks", []))
    if not tracks:
        return {"ok": False, "status": status, "detail": "no caption tracks on this video"}
    chosen = next((t for t in tracks if (t.get("languageCode") or "").startswith(lang)
                   and t.get("kind") != "asr"), None) or tracks[0]
    url = chosen.get("baseUrl", "")
    if not url:
        return {"ok": False, "status": status, "detail": "caption track has no url"}
    status2, text = _get(url + "&fmt=json3")
    cues = parse_any(text) if status2 == 200 else []
    if not cues:
        status2, text = _get(url)
        cues = parse_any(text) if status2 == 200 else []
    if not cues:
        return {"ok": False, "status": status2, "detail": "caption track fetched but empty"}
    return {"ok": True, "cues": cues, "auto": chosen.get("kind") == "asr",
            "language": chosen.get("languageCode"), "status": 200}


def _from_provider(vid: str) -> dict:
    name = keys.setting("captions_provider", "supadata")
    key = keys.get("captions_api_key")
    spec = PROVIDERS.get(name)
    if not key:
        return {"ok": False, "detail": "no captions_api_key configured"}
    if not spec:
        return {"ok": False, "detail": f"unknown captions_provider {name!r}; "
                                       f"expected one of {sorted(PROVIDERS)}"}
    url = spec["url"].format(vid=vid, key=urllib.parse.quote(key))
    headers = {}
    if spec.get("header"):
        headers[spec["header"]] = f"{spec.get('prefix', '')}{key}"
    status, body = _get(url, headers)
    if status != 200:
        return {"ok": False, "status": status, "detail": body[:200]}
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return {"ok": False, "status": status, "detail": "unparseable provider response"}
    for step in spec["path"]:
        if payload is None:
            break
        payload = (payload[int(step)] if isinstance(payload, list) and step.isdigit()
                   else payload.get(step) if isinstance(payload, dict) else None)
    if not isinstance(payload, list):
        return {"ok": False, "status": status, "detail": "provider returned no cue list"}
    shape = spec["cue"]
    scale = shape.get("scale", 1.0)
    cues = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        start = float(item.get(shape["start"]) or 0) * scale
        cues.append({"start_s": start,
                     "end_s": start + float(item.get(shape["dur"]) or 0) * scale,
                     "text": html.unescape(str(item.get(shape["text"]) or "")).strip()})
    cues = _dedupe([c for c in cues if c["text"]])
    return ({"ok": True, "cues": cues, "auto": True, "provider": name}
            if cues else {"ok": False, "detail": "provider returned an empty transcript"})


def _from_sidecar(url: str) -> dict:
    """A caption file published next to hearing video."""
    status, body = _get(url)
    if status != 200:
        return {"ok": False, "status": status, "detail": body[:200]}
    cues = parse_any(body)
    return ({"ok": True, "cues": cues, "auto": False, "status": 200}
            if cues else {"ok": False, "status": status, "detail": "no cues in sidecar"})


VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")


def video_id(url_or_id: str) -> str | None:
    raw = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    m = VIDEO_ID.search(raw)
    return m.group(1) if m else None


def fetch(url_or_id: str, transport: str = "auto", lang: str = "en") -> dict:
    """Captions for one video or hearing, by whichever door opens."""
    vid = video_id(url_or_id)
    sidecar = url_or_id if url_or_id.lower().endswith((".vtt", ".srt")) else None
    attempts: list[dict] = []
    order = ([transport] if transport != "auto"
             else (["sidecar"] if sidecar else []) +
                  (["timedtext", "innertube", "provider"] if vid else []))
    if not order:
        return {"ok": False, "attempts": [{"transport": "none", "ok": False,
                "detail": f"no video id or caption file in {url_or_id!r}"}]}

    for name in order:
        got = ({"sidecar": lambda: _from_sidecar(sidecar or url_or_id),
                "timedtext": lambda: _from_timedtext(vid, lang),
                "innertube": lambda: _from_innertube(vid, lang),
                "provider": lambda: _from_provider(vid)}
               .get(name, lambda: {"ok": False, "detail": "unknown transport"})())
        attempts.append({"transport": name, "ok": got.get("ok", False),
                         "status": got.get("status"), "cues": len(got.get("cues", [])),
                         "detail": got.get("detail")})
        if got.get("ok"):
            return {"ok": True, "transport": name, "video_id": vid,
                    "cues": got["cues"], "auto_generated": got.get("auto", False),
                    "attempts": attempts}
    return {"ok": False, "video_id": vid, "attempts": attempts, "how_to_fix": HOW_TO_FIX}


HOW_TO_FIX = (
    "No caption transport answered. YouTube throttles caption requests from "
    "datacentre addresses, so on a server the reliable path is a captions "
    "provider: run `universe connect key set captions_api_key` and "
    "`universe connect key set captions_provider supadata`. For a hearing, a "
    "Granicus or Legistar .vtt URL also works, as does dropping the caption "
    "file in with `universe connect captions --file <path> --media <id>`."
)


# ------------------------------------------------------------------ store ----
def flatten(cues: Iterable[dict]) -> str:
    """Cues to readable text, one line per speaker turn."""
    out: list[str] = []
    speaker = None
    for cue in cues:
        who = cue.get("speaker")
        if who and who != speaker:
            speaker = who
            out.append(f"\n{who}: {cue['text']}")
        elif out:
            out[-1] = f"{out[-1]} {cue['text']}"
        else:
            out.append(cue["text"])
    return "\n".join(l.strip() for l in out).strip()


def save(store: Store, media_id: str, cues: list[dict], *,
         source: str = "captions", auto: bool = False,
         video: str | None = None) -> dict:
    """Store cues and the flat transcript against a media row."""
    from ..workspace import media as media_mod
    init(store)
    store.conn.execute("DELETE FROM ws_transcript_cues WHERE media_id=?", (media_id,))
    rows = [{"id": f"{media_id}#{c.get('seq', i)}", "media_id": media_id,
             "seq": c.get("seq", i), "start_s": c.get("start_s"),
             "end_s": c.get("end_s"), "speaker": c.get("speaker"),
             "text": c["text"]}
            for i, c in enumerate(cues)]
    if rows:
        store.upsert("ws_transcript_cues", rows)
    text = flatten(cues)
    note = ("auto-generated captions; verify wording against the tape before "
            "quoting" if auto else "published captions")
    media_mod.add_transcript(store, media_id, text, source=f"{source} ({note})")
    store.conn.commit()
    store.journal("captions.saved", {"media": media_id, "cues": len(rows),
                                     "auto": auto, "source": source})
    return {"media_id": media_id, "cues": len(rows), "characters": len(text),
            "auto_generated": auto, "video_id": video}


def pull(store: Store, media_id: str, url_or_id: str = "",
         transport: str = "auto") -> dict:
    """Fetch and store captions for one media row."""
    row = store.one("SELECT * FROM ws_media WHERE id=?", (media_id,))
    target = url_or_id or (row["url"] if row else "")
    if not target:
        return {"ok": False, "error": f"no url known for media {media_id!r}"}
    got = fetch(target, transport)
    if not got["ok"]:
        return {**got, "media_id": media_id}
    return {"ok": True, "transport": got["transport"],
            **save(store, media_id, got["cues"], source=got["transport"],
                   auto=got["auto_generated"], video=got.get("video_id"))}


def pull_missing(store: Store, limit: int = 25, member_only: bool = True) -> dict:
    """Backfill: every video that mentions the Member and has no transcript."""
    sql = ("SELECT id, url, title FROM ws_media WHERE (transcript IS NULL OR "
           "transcript='') AND url IS NOT NULL AND url<>''")
    if member_only:
        sql += " AND mentions_member=1"
    sql += " ORDER BY published DESC LIMIT ?"
    done, failed = [], []
    for row in store.q(sql, (limit,)):
        got = pull(store, row["id"], row["url"])
        (done if got.get("ok") else failed).append(
            {"id": row["id"], "title": row["title"],
             "cues": got.get("cues"), "why": None if got.get("ok")
             else (got.get("attempts") or [{}])[-1].get("detail")})
    return {"transcribed": len(done), "failed": len(failed),
            "done": done, "unavailable": failed,
            "how_to_fix": HOW_TO_FIX if failed and not done else None}


# ------------------------------------------------------------- quotations ----
def quotes(store: Store, topic: str, limit: int = 12,
           speaker: str = "", media_id: str = "") -> list[dict]:
    """
    Verbatim passages, each with its timestamp and a deep link.

    This is the only sanctioned source of a quote in the whole system. It
    returns what was said, when, and where to hear it -- and nothing that was
    not said.
    """
    init(store)
    words = [w for w in re.findall(r"[A-Za-z0-9']{3,}", topic or "")]
    if not words:
        return []
    clauses = " OR ".join("c.text LIKE ?" for _ in words)
    params: list[Any] = [f"%{w}%" for w in words]
    sql = (f"SELECT c.*, m.title, m.url, m.published, m.kind, m.source, "
           f"       m.transcript_source "
           f"FROM ws_transcript_cues c JOIN ws_media m ON m.id=c.media_id "
           f"WHERE ({clauses})")
    if speaker:
        sql += " AND c.speaker LIKE ?"
        params.append(f"%{speaker}%")
    if media_id:
        sql += " AND c.media_id=?"
        params.append(media_id)
    sql += " ORDER BY m.published DESC, c.seq ASC LIMIT ?"
    params.append(limit * 6)

    out: list[dict] = []
    seen: set[str] = set()
    for row in store.q(sql, params):
        window = _window(store, row["media_id"], row["seq"])
        text = " ".join(c["text"] for c in window).strip()
        fingerprint = text[:120].lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        start = int(row["start_s"] or 0)
        vid = video_id(row["url"] or "")
        out.append({
            "quote": text,
            "speaker": row["speaker"],
            "said_at": f"{start // 3600:d}:{start % 3600 // 60:02d}:{start % 60:02d}",
            "start_s": start,
            "media_id": row["media_id"],
            "title": row["title"],
            "published": row["published"],
            "link": WATCH.format(vid=vid, sec=start) if vid else row["url"],
            "hits": sum(1 for w in words if w.lower() in text.lower()),
            "transcript_source": row["transcript_source"],
            "verify": ("verbatim from the stored transcript; check the tape at the "
                       "timestamp before publication"
                       + (" -- these captions were auto-generated"
                          if "auto-generated" in (row["transcript_source"] or "")
                          else "")),
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda q: (-q["hits"], q["published"] or ""))
    return out


def _window(store: Store, media_id: str, seq: int, span: int = 2) -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT text FROM ws_transcript_cues WHERE media_id=? AND seq BETWEEN ? AND ? "
        "ORDER BY seq", (media_id, max(0, seq - span), seq + span))]


def coverage(store: Store) -> dict:
    init(store)
    total = store.scalar("SELECT COUNT(*) FROM ws_media WHERE url IS NOT NULL") or 0
    with_text = store.scalar(
        "SELECT COUNT(*) FROM ws_media WHERE transcript IS NOT NULL AND transcript<>''") or 0
    cues = store.scalar("SELECT COUNT(*) FROM ws_transcript_cues") or 0
    return {"media": total, "with_transcript": with_text, "cues": cues,
            "coverage_pct": round(100.0 * with_text / total, 1) if total else 0.0,
            "quotable": with_text > 0,
            "note": ("Quotes can only be drawn from the transcribed items. "
                     "Everything else is summarisable but not quotable.")}
