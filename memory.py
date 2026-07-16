#!/usr/bin/env python3
"""
memory.py — the adaptive/learning layer.

A small SQLite store (under .appstate/) that persists across sessions and makes
the tool get smarter the more it's used:

  * activity log     — every meaningful thing you open/search/brief on;
  * interest profile — those events rolled up into weighted interests (top
    topics, the members and districts you follow most), so the app can lead with
    what you actually care about;
  * knowledge notes  — facts/observations you save against a member, topic, or
    bill, which the briefing/messaging tools pull back in automatically, so your
    curated knowledge compounds;
  * saved items      — briefings, ideas, statements you keep.

Everything is defensive: if the database can't be opened (read-only FS), it
degrades to an in-memory store for the session rather than breaking the app.
"""

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".appstate")
_DB_PATH = os.path.join(_DIR, "memory.db")

# Weights for how much different actions signal interest.
_WEIGHTS = {"view": 1.0, "search": 1.5, "brief": 3.0, "note": 4.0, "save": 3.0, "follow": 5.0}


def _now():
    return datetime.now(timezone.utc).isoformat()


class Memory:
    def __init__(self, path=_DB_PATH):
        self.ok = True
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self._init()
        except Exception:
            self.ok = False
            try:
                self.db = sqlite3.connect(":memory:", check_same_thread=False)
                self._init()
            except Exception:
                self.db = None

    def _init(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS activity (ts TEXT, kind TEXT, "
                        "etype TEXT, entity TEXT, meta TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "ts TEXT, etype TEXT, entity TEXT, note TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS saved (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "ts TEXT, kind TEXT, title TEXT, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS follows (etype TEXT, entity TEXT, ts TEXT, "
                        "PRIMARY KEY (etype, entity))")
        self.db.commit()

    # ---- write ----------------------------------------------------------
    def log(self, kind, etype, entity, meta=None):
        if not self.db or not entity:
            return
        try:
            self.db.execute("INSERT INTO activity VALUES (?,?,?,?,?)",
                            (_now(), kind, etype, str(entity), json.dumps(meta or {})))
            self.db.commit()
        except Exception:
            pass

    def add_note(self, etype, entity, note):
        if not self.db or not (note or "").strip():
            return
        try:
            self.db.execute("INSERT INTO notes (ts,etype,entity,note) VALUES (?,?,?,?)",
                            (_now(), etype, str(entity), note.strip()))
            self.db.commit()
            self.log("note", etype, entity)
        except Exception:
            pass

    def delete_note(self, note_id):
        try:
            self.db.execute("DELETE FROM notes WHERE id=?", (note_id,)); self.db.commit()
        except Exception:
            pass

    def save_item(self, kind, title, body):
        if not self.db:
            return
        try:
            self.db.execute("INSERT INTO saved (ts,kind,title,body) VALUES (?,?,?,?)",
                            (_now(), kind, title, body)); self.db.commit()
            self.log("save", kind, title)
        except Exception:
            pass

    def follow(self, etype, entity):
        try:
            self.db.execute("INSERT OR REPLACE INTO follows VALUES (?,?,?)", (etype, str(entity), _now()))
            self.db.commit(); self.log("follow", etype, entity)
        except Exception:
            pass

    def unfollow(self, etype, entity):
        try:
            self.db.execute("DELETE FROM follows WHERE etype=? AND entity=?", (etype, str(entity)))
            self.db.commit()
        except Exception:
            pass

    # ---- read -----------------------------------------------------------
    def _rows(self, sql, args=()):
        try:
            return self.db.execute(sql, args).fetchall()
        except Exception:
            return []

    def recent(self, n=12):
        return [{"ts": ts, "kind": k, "etype": et, "entity": e}
                for ts, k, et, e in self._rows(
                    "SELECT ts,kind,etype,entity FROM activity ORDER BY ts DESC LIMIT ?", (n,))]

    def interest_profile(self):
        """Weighted interests by entity type. Returns {etype: [(entity, weight), ...]}."""
        weights = {}
        for _ts, kind, etype, entity, _meta in self._rows(
                "SELECT ts,kind,etype,entity,meta FROM activity"):
            if not entity:
                continue
            weights.setdefault(etype, Counter())[entity] += _WEIGHTS.get(kind, 1.0)
        return {et: c.most_common(10) for et, c in weights.items()}

    def follows(self):
        out = {}
        for etype, entity, _ts in self._rows("SELECT etype,entity,ts FROM follows ORDER BY ts DESC"):
            out.setdefault(etype, []).append(entity)
        return out

    def is_following(self, etype, entity):
        return bool(self._rows("SELECT 1 FROM follows WHERE etype=? AND entity=?", (etype, str(entity))))

    def notes_for(self, etype, entity):
        return [{"id": i, "note": n, "ts": ts} for i, n, ts in self._rows(
            "SELECT id,note,ts FROM notes WHERE etype=? AND entity=? ORDER BY ts DESC", (etype, str(entity)))]

    def all_notes(self):
        return [{"id": i, "etype": et, "entity": e, "note": n, "ts": ts} for i, et, e, n, ts in self._rows(
            "SELECT id,etype,entity,note,ts FROM notes ORDER BY ts DESC")]

    def saved_items(self, kind=None):
        if kind:
            rows = self._rows("SELECT id,kind,title,body,ts FROM saved WHERE kind=? ORDER BY ts DESC", (kind,))
        else:
            rows = self._rows("SELECT id,kind,title,body,ts FROM saved ORDER BY ts DESC")
        return [{"id": i, "kind": k, "title": t, "body": b, "ts": ts} for i, k, t, b, ts in rows]

    def stats(self):
        a = self._rows("SELECT COUNT(*) FROM activity")
        n = self._rows("SELECT COUNT(*) FROM notes")
        s = self._rows("SELECT COUNT(*) FROM saved")
        f = self._rows("SELECT COUNT(*) FROM follows")
        return {"events": a[0][0] if a else 0, "notes": n[0][0] if n else 0,
                "saved": s[0][0] if s else 0, "follows": f[0][0] if f else 0}


def context_for_briefing(mem, etype, entity, topic=None):
    """Assemble the user's saved knowledge relevant to a briefing target.

    Returns a short text block (or '') the briefing tools inject so outputs
    reflect accumulated, user-curated knowledge — the adaptive payoff.
    """
    if not mem:
        return ""
    bits = []
    for note in mem.notes_for(etype, entity)[:6]:
        bits.append(f"- {note['note']}")
    if topic:
        for note in mem.notes_for("topic", topic)[:4]:
            bits.append(f"- ({topic}) {note['note']}")
    if not bits:
        return ""
    return "SAVED STAFF KNOWLEDGE (verified notes to weave in where relevant):\n" + "\n".join(bits)
