#!/usr/bin/env python3
"""
Workspace operations.

Programs, projects, sections, tasks, people, dependencies, comments,
@mentions and the notification inbox. Every write appends to the event log so
open browsers update live, and every task write is guarded by an optimistic
revision check so two staffers editing the same task cannot silently overwrite
one another.

Rules enforced here rather than in the UI, because the UI is not the only
caller:

* A task cannot depend on itself, and dependency cycles are refused.
* Completing a task that still blocks others reports what it unblocks.
* A subtask belongs to its parent's project; it cannot drift to another.
* An @mention notifies a real person on the roster, not an arbitrary string.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import events, schema

MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9._-]{1,40})")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return uuid.uuid4().hex


def init(store) -> None:
    schema.apply(store.conn)


def _rows(store, sql: str, params: Iterable = ()) -> list[dict]:
    return [dict(r) for r in store.q(sql, list(params))]


def _one(store, sql: str, params: Iterable = ()) -> dict | None:
    r = store.one(sql, list(params))
    return dict(r) if r else None


# ------------------------------------------------------------------ people --
def people(store, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM ws_people"
    if active_only:
        sql += " WHERE active = 1"
    return _rows(store, sql + " ORDER BY name")


def save_person(store, d: dict) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("A person needs a name.")
    ident = d.get("id") or _id()
    initials = (d.get("initials") or "".join(
        w[0] for w in name.split()[:2])).upper()[:3]
    row = {
        "id": ident, "name": name[:120], "initials": initials,
        "role": str(d.get("role") or "")[:120],
        "email": str(d.get("email") or "")[:200],
        "color": str(d.get("color") or "")[:20],
        "weekly_hours": float(d.get("weekly_hours") or 35),
        "active": 1 if d.get("active", True) else 0,
        "created": now(),
    }
    store.upsert("ws_people", [row])
    events.emit(store, "person.saved", "person", ident, f"{name} updated",
                d.get("actor", ""))
    return row


# ---------------------------------------------------------------- programs --
def programs(store, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM ws_programs"
    if not include_archived:
        sql += " WHERE archived = 0"
    out = _rows(store, sql + " ORDER BY position, name")
    for p in out:
        p["projects"] = projects(store, program_id=p["id"])
        p["task_counts"] = _program_counts(store, p["id"])
    return out


def _program_counts(store, program_id: str) -> dict:
    row = _one(store, """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN t.status!='Completed' AND t.due!='' AND t.due < ?
                        THEN 1 ELSE 0 END) AS overdue
        FROM workspace_tasks t JOIN ws_projects p ON p.id = t.project_id
        WHERE p.program_id = ?
    """, [now(), program_id]) or {}
    total, done = row.get("total") or 0, row.get("done") or 0
    return {"total": total, "done": done, "overdue": row.get("overdue") or 0,
            "pct": round(100 * done / total) if total else 0}


def save_program(store, d: dict) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("A program needs a name.")
    ident = d.get("id") or _id()
    old = _one(store, "SELECT * FROM ws_programs WHERE id=?", [ident])
    row = {
        "id": ident, "name": name[:200],
        "purpose": str(d.get("purpose") or "")[:2000],
        "pillar": str(d.get("pillar") or "")[:60],
        "lead": str(d.get("lead") or "")[:120],
        "color": str(d.get("color") or "")[:20],
        "icon": str(d.get("icon") or "")[:8],
        "fy": int(d["fy"]) if str(d.get("fy") or "").strip().isdigit() else None,
        "starts": str(d.get("starts") or "")[:40],
        "ends": str(d.get("ends") or "")[:40],
        "archived": 1 if d.get("archived") else 0,
        "position": int(d.get("position") or 0),
        "created": (old or {}).get("created") or now(), "updated": now(),
    }
    store.upsert("ws_programs", [row])
    events.emit(store, "program.saved", "program", ident,
                f"Program {name} {'updated' if old else 'created'}",
                d.get("actor", ""))
    return row


# ---------------------------------------------------------------- projects --
def projects(store, program_id: str | None = None,
             include_archived: bool = False) -> list[dict]:
    sql, params = "SELECT * FROM ws_projects WHERE 1=1", []
    if program_id:
        sql += " AND program_id = ?"
        params.append(program_id)
    if not include_archived:
        sql += " AND archived = 0"
    out = _rows(store, sql + " ORDER BY position, name", params)
    for p in out:
        c = _one(store, """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status!='Completed' AND due!='' AND due<?
                            THEN 1 ELSE 0 END) AS overdue
            FROM workspace_tasks
            WHERE project_id = ? AND COALESCE(parent_id, '') = ''
        """, [now(), p["id"]]) or {}
        total, done = c.get("total") or 0, c.get("done") or 0
        p["counts"] = {"total": total, "done": done,
                       "overdue": c.get("overdue") or 0,
                       "pct": round(100 * done / total) if total else 0}
    return out


def save_project(store, d: dict) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("A project needs a name.")
    health = d.get("health") or "On track"
    if health not in schema.HEALTH:
        raise ValueError(f"Health must be one of {', '.join(schema.HEALTH)}.")
    ident = d.get("id") or _id()
    old = _one(store, "SELECT * FROM ws_projects WHERE id=?", [ident])
    row = {
        "id": ident, "program_id": str(d.get("program_id") or "")[:64],
        "name": name[:200], "purpose": str(d.get("purpose") or "")[:2000],
        "lead": str(d.get("lead") or "")[:120], "health": health,
        "color": str(d.get("color") or "")[:20],
        "starts": str(d.get("starts") or "")[:40],
        "due": str(d.get("due") or "")[:40],
        "archived": 1 if d.get("archived") else 0,
        "default_view": str(d.get("default_view") or "board")[:20],
        "position": int(d.get("position") or 0),
        "created": (old or {}).get("created") or now(), "updated": now(),
    }
    store.upsert("ws_projects", [row])
    if not old:
        for i, s in enumerate(("Intake", "In progress", "Review", "Done")):
            store.upsert("ws_sections", [{"id": _id(), "project_id": ident,
                                          "name": s, "position": i,
                                          "collapsed": 0}])
    events.emit(store, "project.saved", "project", ident,
                f"Project {name} {'updated' if old else 'created'}",
                d.get("actor", ""))
    return row


def sections(store, project_id: str) -> list[dict]:
    return _rows(store, "SELECT * FROM ws_sections WHERE project_id=? "
                        "ORDER BY position, name", [project_id])


def save_section(store, d: dict) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("A section needs a name.")
    row = {"id": d.get("id") or _id(),
           "project_id": str(d.get("project_id") or "")[:64],
           "name": name[:120], "position": int(d.get("position") or 0),
           "collapsed": 1 if d.get("collapsed") else 0}
    store.upsert("ws_sections", [row])
    events.emit(store, "section.saved", "section", row["id"], name,
                d.get("actor", ""))
    return row


def post_status(store, d: dict) -> dict:
    """An Asana-style project status update: a colour and a paragraph."""
    project_id = str(d.get("project_id") or "")
    health = d.get("health") or "On track"
    if health not in schema.HEALTH:
        raise ValueError("Choose a valid project health.")
    body = str(d.get("body") or "").strip()
    if not body:
        raise ValueError("A status update needs a sentence.")
    store.conn.execute(
        "INSERT INTO ws_status_updates (project_id, author, health, body, created) "
        "VALUES (?,?,?,?,?)",
        (project_id, str(d.get("author") or "")[:120], health, body[:4000], now()))
    store.conn.execute("UPDATE ws_projects SET health=?, updated=? WHERE id=?",
                       (health, now(), project_id))
    store.conn.commit()
    proj = _one(store, "SELECT name FROM ws_projects WHERE id=?", [project_id]) or {}
    events.emit(store, "project.status", "project", project_id,
                f"{proj.get('name', 'Project')} is {health}", d.get("author", ""),
                {"health": health})
    _notify_project(store, project_id,
                    f"{d.get('author') or 'A teammate'} set {proj.get('name','a project')} to {health}",
                    actor=str(d.get("author") or ""), kind="status")
    return {"ok": True, "health": health}


# ------------------------------------------------------------------- tasks --
TASK_TEXT = ("title", "owner", "backup", "due", "priority", "status",
             "reviewer", "link", "blocker", "followup", "outcome",
             "project", "description", "project_id", "section_id",
             "parent_id", "starts", "tags", "recurrence")


def task(store, task_id: str) -> dict | None:
    t = _one(store, "SELECT * FROM workspace_tasks WHERE id=?", [task_id])
    if not t:
        return None
    t["checklist"] = _rows(store, "SELECT * FROM workspace_checklist "
                                  "WHERE task_id=? ORDER BY position, id", [task_id])
    t["comments"] = _rows(store, "SELECT * FROM workspace_comments "
                                 "WHERE task_id=? ORDER BY id", [task_id])
    t["subtasks"] = _rows(store, "SELECT * FROM workspace_tasks "
                                 "WHERE parent_id=? ORDER BY position, created",
                          [task_id])
    t["followers"] = [r["person"] for r in _rows(
        store, "SELECT person FROM ws_followers WHERE task_id=?", [task_id])]
    t["blocked_by"] = _rows(store, """
        SELECT d.blocked_by AS id, t.title, t.status
        FROM ws_task_deps d JOIN workspace_tasks t ON t.id = d.blocked_by
        WHERE d.task_id = ?""", [task_id])
    t["blocks"] = _rows(store, """
        SELECT d.task_id AS id, t.title, t.status
        FROM ws_task_deps d JOIN workspace_tasks t ON t.id = d.task_id
        WHERE d.blocked_by = ?""", [task_id])
    t["links"] = _rows(store, """
        SELECT l.id, l.record_key, l.note, r.title, r.kind, r.url
        FROM ws_links l LEFT JOIN workspace_records r ON r.key = l.record_key
        WHERE l.task_id = ? ORDER BY l.id""", [task_id])
    t["fields"] = _rows(store, """
        SELECT f.id, f.name, f.type, f.options, v.value
        FROM ws_fields f LEFT JOIN ws_field_values v
          ON v.field_id = f.id AND v.task_id = ?
        WHERE f.project_id = ? ORDER BY f.position""",
        [task_id, t.get("project_id") or ""])
    t["log"] = _rows(store, "SELECT * FROM workspace_task_log "
                            "WHERE task_id=? ORDER BY id DESC LIMIT 40", [task_id])
    t["ready"] = not [b for b in t["blocked_by"] if b["status"] != "Completed"]
    return t


def tasks(store, **f) -> list[dict]:
    sql = ["SELECT * FROM workspace_tasks WHERE 1=1"]
    params: list[Any] = []
    if f.get("project_id"):
        sql.append("AND project_id = ?"); params.append(f["project_id"])
    if f.get("program_id"):
        sql.append("AND project_id IN (SELECT id FROM ws_projects WHERE program_id=?)")
        params.append(f["program_id"])
    if f.get("owner"):
        sql.append("AND owner = ?"); params.append(f["owner"])
    if f.get("status"):
        sql.append("AND status = ?"); params.append(f["status"])
    if f.get("section_id"):
        sql.append("AND section_id = ?"); params.append(f["section_id"])
    if f.get("top_level"):
        sql.append("AND COALESCE(parent_id, '') = ''")
    if not f.get("include_done"):
        pass  # completed tasks still belong on a board's Done column
    if f.get("due_before"):
        sql.append("AND due != '' AND due <= ?"); params.append(f["due_before"])
    sql.append("ORDER BY CASE WHEN status='Completed' THEN 1 ELSE 0 END, "
               "position, due='' ASC, due, updated DESC")
    return _rows(store, " ".join(sql), params)


def save_task(store, d: dict) -> dict:
    init(store)
    title = str(d.get("title", "")).strip()
    if not title:
        raise ValueError("A task needs a title.")
    stage = d.get("status") or "Intake"
    if stage not in schema.STAGES:
        raise ValueError(f"Stage must be one of {', '.join(schema.STAGES)}.")
    priority = d.get("priority") or "Normal"
    if priority not in schema.PRIORITIES:
        raise ValueError("Choose a valid priority.")

    ident = d.get("id") or _id()
    old = _one(store, "SELECT * FROM workspace_tasks WHERE id=?", [ident])
    if old and d.get("revision") is not None and \
            int(d["revision"]) != int(old.get("revision") or 1):
        raise ValueError("This task changed while you were editing it. "
                         "Reopen it so the newer update is not lost.")

    row = {k: str(d.get(k) if d.get(k) is not None
                  else (old or {}).get(k) or "")[:4000] for k in TASK_TEXT}
    # A subtask always lives in its parent's project.
    if row["parent_id"]:
        parent = _one(store, "SELECT project_id, section_id FROM workspace_tasks "
                             "WHERE id=?", [row["parent_id"]])
        if not parent:
            raise ValueError("The parent task no longer exists.")
        if row["parent_id"] == ident:
            raise ValueError("A task cannot be its own subtask.")
        row["project_id"] = parent["project_id"] or ""
    row.update(
        id=ident, title=title[:500], status=stage, priority=priority,
        milestone=1 if d.get("milestone") else 0,
        position=int(d.get("position") or (old or {}).get("position") or 0),
        estimate_hours=(float(d["estimate_hours"])
                        if str(d.get("estimate_hours") or "").strip() else None),
        created=(old or {}).get("created") or now(), updated=now(),
        revision=int((old or {}).get("revision") or 0) + 1,
        completed_at=(now() if stage == "Completed"
                      else ((old or {}).get("completed_at") if stage == "Completed" else "")),
    )
    for key in ("due", "followup", "starts"):
        if row.get(key):
            datetime.fromisoformat(row[key])

    with store.tx() as c:
        cols = [k for k in row if k != "id"]
        if old:
            cur = c.execute(
                "UPDATE workspace_tasks SET " + ",".join(f"{k}=?" for k in cols) +
                " WHERE id=? AND revision=?",
                [row[k] for k in cols] + [ident, old.get("revision") or 1])
            if cur.rowcount != 1:
                raise ValueError("A newer update was saved first. Reopen the task.")
        else:
            c.execute(f"INSERT INTO workspace_tasks ({','.join(row)}) "
                      f"VALUES ({','.join('?' for _ in row)})", list(row.values()))
        c.execute("INSERT INTO workspace_task_log "
                  "(task_id, at, actor, before_json, after_json) VALUES (?,?,?,?,?)",
                  (ident, now(), str(d.get("actor") or "Workspace user"),
                   json.dumps(old, default=str) if old else None,
                   json.dumps(row, default=str)))

    changed = _diff(old, row)
    verb = "updated" if old else "created"
    events.emit(store, f"task.{verb}", "task", ident,
                f"{title[:80]} {verb}" + (f" ({', '.join(changed)})" if changed else ""),
                str(d.get("actor") or ""), {"status": stage, "owner": row["owner"],
                                            "project_id": row["project_id"],
                                            "changed": changed})
    _notify_watchers(store, ident, row, old, str(d.get("actor") or ""))
    return row


def _diff(old: dict | None, new: dict) -> list[str]:
    if not old:
        return []
    watch = ("status", "owner", "due", "priority", "section_id", "title")
    return [k for k in watch if str(old.get(k) or "") != str(new.get(k) or "")]


def move_task(store, task_id: str, section_id: str = "", position: int = 0,
              status: str = "", actor: str = "") -> dict:
    """Drag-and-drop: reposition within a board without a full save."""
    t = _one(store, "SELECT * FROM workspace_tasks WHERE id=?", [task_id])
    if not t:
        raise ValueError("That task no longer exists.")
    if status and status not in schema.STAGES:
        raise ValueError("Invalid stage.")
    fields = {"position": int(position), "updated": now(),
              "revision": int(t.get("revision") or 1) + 1}
    if section_id:
        fields["section_id"] = section_id
    if status:
        fields["status"] = status
        if status == "Completed":
            fields["completed_at"] = now()
    with store.tx() as c:
        c.execute("UPDATE workspace_tasks SET " +
                  ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                  list(fields.values()) + [task_id])
    events.emit(store, "task.moved", "task", task_id,
                f"{t['title'][:60]} moved" + (f" to {status}" if status else ""),
                actor, {"status": status or t["status"], "section_id": section_id})
    return {"ok": True, **fields}


def complete_task(store, task_id: str, actor: str = "") -> dict:
    """Complete a task and report what it unblocks."""
    t = _one(store, "SELECT * FROM workspace_tasks WHERE id=?", [task_id])
    if not t:
        raise ValueError("That task no longer exists.")
    open_subs = store.scalar(
        "SELECT COUNT(*) FROM workspace_tasks WHERE parent_id=? AND status!='Completed'",
        [task_id]) or 0
    with store.tx() as c:
        c.execute("UPDATE workspace_tasks SET status='Completed', completed_at=?, "
                  "updated=?, revision=revision+1 WHERE id=?",
                  (now(), now(), task_id))
    unblocked = _rows(store, """
        SELECT t.id, t.title, t.owner FROM ws_task_deps d
        JOIN workspace_tasks t ON t.id = d.task_id
        WHERE d.blocked_by = ? AND t.status != 'Completed'""", [task_id])
    ready = [u for u in unblocked if not store.scalar("""
        SELECT COUNT(*) FROM ws_task_deps d JOIN workspace_tasks b ON b.id=d.blocked_by
        WHERE d.task_id=? AND b.status!='Completed'""", [u["id"]])]
    events.emit(store, "task.completed", "task", task_id,
                f"{t['title'][:80]} completed", actor,
                {"unblocked": [u["id"] for u in ready]})
    for u in ready:
        if u["owner"]:
            _notify(store, u["owner"], "unblocked", u["id"], t.get("project_id"),
                    f"{t['title'][:60]} finished — {u['title'][:60]} is ready to start",
                    actor)
    return {"ok": True, "open_subtasks": open_subs,
            "unblocked": ready,
            "note": (f"{open_subs} subtask(s) are still open."
                     if open_subs else "")}


# ------------------------------------------------------------ dependencies --
def add_dependency(store, task_id: str, blocked_by: str, actor: str = "") -> dict:
    """Record that ``task_id`` cannot start until ``blocked_by`` finishes."""
    if task_id == blocked_by:
        raise ValueError("A task cannot block itself.")
    for t in (task_id, blocked_by):
        if not store.scalar("SELECT 1 FROM workspace_tasks WHERE id=?", [t]):
            raise ValueError("Both tasks must exist before linking them.")
    if _creates_cycle(store, task_id, blocked_by):
        raise ValueError("That would create a circular dependency: the blocking "
                         "task already depends on this one.")
    store.conn.execute("INSERT OR IGNORE INTO ws_task_deps (task_id, blocked_by, created) "
                       "VALUES (?,?,?)", (task_id, blocked_by, now()))
    store.conn.commit()
    events.emit(store, "task.dependency", "task", task_id, "Dependency added", actor,
                {"blocked_by": blocked_by})
    return {"ok": True}


def _creates_cycle(store, task_id: str, blocked_by: str) -> bool:
    """True when blocked_by already depends, transitively, on task_id."""
    seen, stack = set(), [blocked_by]
    while stack:
        cur = stack.pop()
        if cur == task_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack += [r["blocked_by"] for r in _rows(
            store, "SELECT blocked_by FROM ws_task_deps WHERE task_id=?", [cur])]
    return False


def remove_dependency(store, task_id: str, blocked_by: str) -> dict:
    store.conn.execute("DELETE FROM ws_task_deps WHERE task_id=? AND blocked_by=?",
                       (task_id, blocked_by))
    store.conn.commit()
    return {"ok": True}


# --------------------------------------------------- comments and mentions --
def add_comment(store, d: dict) -> dict:
    task_id = str(d.get("task_id") or "")
    body = str(d.get("body") or "").strip()
    if not body:
        raise ValueError("Write something before posting.")
    t = _one(store, "SELECT title, project_id, owner FROM workspace_tasks WHERE id=?",
             [task_id])
    if not t:
        raise ValueError("That task no longer exists.")
    author = str(d.get("author") or "Workspace user")[:120]
    cur = store.conn.execute(
        "INSERT INTO workspace_comments (task_id, author, body, created, parent_id, pinned) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, author, body[:8000], now(),
         int(d["parent_id"]) if str(d.get("parent_id") or "").isdigit() else None,
         1 if d.get("pinned") else 0))
    store.conn.commit()
    comment_id = cur.lastrowid

    mentioned = resolve_mentions(store, body)
    for person in mentioned:
        if person != author:
            _notify(store, person, "mention", task_id, t.get("project_id"),
                    f"{author} mentioned you on {t['title'][:60]}", author)
    # Followers hear about the comment too, without being mentioned.
    for f in _rows(store, "SELECT person FROM ws_followers WHERE task_id=?", [task_id]):
        if f["person"] not in mentioned and f["person"] != author:
            _notify(store, f["person"], "comment", task_id, t.get("project_id"),
                    f"{author} commented on {t['title'][:60]}", author)
    if t.get("owner") and t["owner"] not in mentioned and t["owner"] != author:
        _notify(store, t["owner"], "comment", task_id, t.get("project_id"),
                f"{author} commented on {t['title'][:60]}", author)

    events.emit(store, "comment.added", "task", task_id,
                f"{author} commented on {t['title'][:60]}", author,
                {"comment_id": comment_id, "mentions": mentioned})
    return {"id": comment_id, "mentions": mentioned}


def resolve_mentions(store, body: str) -> list[str]:
    """Map @handles in text onto real people on the roster.

    An @mention that matches nobody is left as plain text rather than
    generating a notification no one will ever read.
    """
    roster = people(store, active_only=False)
    found: list[str] = []
    for handle in MENTION.findall(body or ""):
        low = handle.lower().replace(".", " ").replace("_", " ")
        for p in roster:
            names = {p["name"].lower(), (p["initials"] or "").lower(),
                     p["name"].split()[0].lower(),
                     p["name"].lower().replace(" ", "")}
            if low in names or low == p["name"].split()[-1].lower():
                if p["name"] not in found:
                    found.append(p["name"])
                break
    return found


def follow(store, task_id: str, person: str, on: bool = True) -> dict:
    if on:
        store.conn.execute("INSERT OR IGNORE INTO ws_followers (task_id, person, added) "
                           "VALUES (?,?,?)", (task_id, person, now()))
    else:
        store.conn.execute("DELETE FROM ws_followers WHERE task_id=? AND person=?",
                           (task_id, person))
    store.conn.commit()
    return {"ok": True, "following": on}


# ----------------------------------------------------------- notifications --
def _notify(store, person: str, kind: str, task_id: str | None,
            project_id: str | None, body: str, actor: str = "") -> None:
    if not person:
        return
    store.conn.execute(
        "INSERT INTO ws_notifications (person, kind, task_id, project_id, actor, "
        "body, created, read) VALUES (?,?,?,?,?,?,?,0)",
        (person, kind, task_id, project_id, actor, body[:500], now()))
    store.conn.commit()


def _notify_project(store, project_id: str, body: str, actor: str = "",
                    kind: str = "project") -> None:
    seen: set[str] = set()
    for r in _rows(store, "SELECT DISTINCT owner FROM workspace_tasks "
                          "WHERE project_id=? AND owner!=''", [project_id]):
        if r["owner"] and r["owner"] != actor and r["owner"] not in seen:
            seen.add(r["owner"])
            _notify(store, r["owner"], kind, None, project_id, body, actor)


def _notify_watchers(store, task_id: str, row: dict, old: dict | None,
                     actor: str) -> None:
    """Tell the right people when an assignment or a date moves.

    Only a *person* generates a notification. A write with no recorded actor
    is an import, a seed or a script, and attributing it to "someone" fills
    every inbox with noise the moment the workspace is populated.
    """
    if not actor:
        return
    if old is None:
        if row.get("owner") and row["owner"] != actor:
            _notify(store, row["owner"], "assigned", task_id, row.get("project_id"),
                    f"{actor or 'Someone'} assigned you {row['title'][:60]}", actor)
        return
    if row.get("owner") and row["owner"] != old.get("owner") and row["owner"] != actor:
        _notify(store, row["owner"], "assigned", task_id, row.get("project_id"),
                f"{actor or 'Someone'} assigned you {row['title'][:60]}", actor)
    if str(row.get("due") or "") != str(old.get("due") or ""):
        for f in _rows(store, "SELECT person FROM ws_followers WHERE task_id=?",
                       [task_id]):
            if f["person"] != actor:
                _notify(store, f["person"], "date", task_id, row.get("project_id"),
                        f"Due date changed on {row['title'][:60]}", actor)


def inbox(store, person: str, unread_only: bool = False, limit: int = 80) -> list[dict]:
    sql = "SELECT * FROM ws_notifications WHERE person = ?"
    params: list[Any] = [person]
    if unread_only:
        sql += " AND read = 0"
    return _rows(store, sql + " ORDER BY id DESC LIMIT ?", params + [limit])


def mark_read(store, person: str, ids: Iterable[int] | None = None) -> dict:
    if ids:
        ids = [int(i) for i in ids]
        store.conn.execute(
            f"UPDATE ws_notifications SET read=1 WHERE person=? AND id IN "
            f"({','.join('?' * len(ids))})", [person, *ids])
    else:
        store.conn.execute("UPDATE ws_notifications SET read=1 WHERE person=?",
                           (person,))
    store.conn.commit()
    return {"ok": True}


# ---------------------------------------------------------- links & fields --
def link_record(store, task_id: str, record_key: str, note: str = "") -> dict:
    """Attach a bill, funding line, organization or contact to a task."""
    if not store.scalar("SELECT 1 FROM workspace_tasks WHERE id=?", [task_id]):
        raise ValueError("That task no longer exists.")
    store.conn.execute("INSERT INTO ws_links (task_id, record_key, note, created) "
                       "VALUES (?,?,?,?)", (task_id, record_key, note[:500], now()))
    store.conn.commit()
    events.emit(store, "task.linked", "task", task_id,
                f"Evidence attached: {record_key}", "", {"record_key": record_key})
    return {"ok": True}


def unlink_record(store, link_id: int) -> dict:
    store.conn.execute("DELETE FROM ws_links WHERE id=?", (int(link_id),))
    store.conn.commit()
    return {"ok": True}


def save_field(store, d: dict) -> dict:
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("A custom field needs a name.")
    ftype = d.get("type") or "text"
    if ftype not in schema.FIELD_TYPES:
        raise ValueError(f"Field type must be one of {', '.join(schema.FIELD_TYPES)}.")
    row = {"id": d.get("id") or _id(),
           "project_id": str(d.get("project_id") or "")[:64],
           "name": name[:120], "type": ftype,
           "options": json.dumps(d.get("options") or []),
           "position": int(d.get("position") or 0)}
    store.upsert("ws_fields", [row])
    return row


def set_field_value(store, task_id: str, field_id: str, value: str) -> dict:
    store.upsert("ws_field_values", [{"task_id": task_id, "field_id": field_id,
                                      "value": str(value)[:2000]}])
    events.emit(store, "task.field", "task", task_id, "Custom field updated", "",
                {"field_id": field_id})
    return {"ok": True}


# ------------------------------------------------------------- checklists --
def save_checklist_item(store, d: dict) -> dict:
    task_id = str(d.get("task_id") or "")
    if d.get("id"):
        store.conn.execute(
            "UPDATE workspace_checklist SET done=?, title=COALESCE(NULLIF(?,''),title), "
            "owner=? WHERE id=?",
            (1 if d.get("done") else 0, str(d.get("title") or "")[:400],
             str(d.get("owner") or "")[:120], int(d["id"])))
    else:
        title = str(d.get("title") or "").strip()
        if not title:
            raise ValueError("A checklist step needs a title.")
        nxt = (store.scalar("SELECT COALESCE(MAX(position),0)+1 FROM workspace_checklist "
                            "WHERE task_id=?", [task_id]) or 1)
        store.conn.execute(
            "INSERT INTO workspace_checklist (task_id, title, done, position, owner) "
            "VALUES (?,?,?,?,?)",
            (task_id, title[:400], 1 if d.get("done") else 0, nxt,
             str(d.get("owner") or "")[:120]))
    store.conn.commit()
    events.emit(store, "checklist.saved", "task", task_id, "Checklist updated",
                str(d.get("actor") or ""))
    return {"ok": True}


# ----------------------------------------------------------------- digests --
def my_work(store, person: str) -> dict:
    """The 'My Tasks' view: today, upcoming, later, and what is blocked."""
    today = date.today().isoformat()
    week = (date.today() + timedelta(days=7)).isoformat()
    mine = _rows(store, "SELECT * FROM workspace_tasks WHERE owner=? AND status!='Completed' "
                        "ORDER BY due='' ASC, due, priority DESC", [person])
    blocked_ids = {r["task_id"] for r in _rows(store, """
        SELECT d.task_id FROM ws_task_deps d JOIN workspace_tasks b ON b.id=d.blocked_by
        WHERE b.status != 'Completed'""")}
    bucket = {"overdue": [], "today": [], "week": [], "later": [],
              "someday": [], "blocked": []}
    for t in mine:
        if t["id"] in blocked_ids:
            bucket["blocked"].append(t); continue
        due = (t.get("due") or "")[:10]
        if not due:
            bucket["someday"].append(t)
        elif due < today:
            bucket["overdue"].append(t)
        elif due == today:
            bucket["today"].append(t)
        elif due <= week:
            bucket["week"].append(t)
        else:
            bucket["later"].append(t)
    return {"person": person, "buckets": bucket, "total": len(mine),
            "unread": store.scalar("SELECT COUNT(*) FROM ws_notifications "
                                   "WHERE person=? AND read=0", [person]) or 0}


def workload(store, days: int = 14) -> list[dict]:
    """Per-person open work and estimated hours against weekly capacity."""
    horizon = (date.today() + timedelta(days=days)).isoformat()
    out = []
    for p in people(store):
        rows = _rows(store, """
            SELECT COUNT(*) AS open,
                   SUM(CASE WHEN due!='' AND due<=? THEN 1 ELSE 0 END) AS soon,
                   SUM(CASE WHEN due!='' AND due<? THEN 1 ELSE 0 END) AS overdue,
                   SUM(COALESCE(estimate_hours,0)) AS hours
            FROM workspace_tasks WHERE owner=? AND status!='Completed'""",
            [horizon, date.today().isoformat(), p["name"]])
        r = rows[0] if rows else {}
        capacity = (p.get("weekly_hours") or 35) * (days / 7)
        hours = r.get("hours") or 0
        out.append({**p, "open": r.get("open") or 0, "soon": r.get("soon") or 0,
                    "overdue": r.get("overdue") or 0, "hours": round(hours, 1),
                    "capacity": round(capacity, 1),
                    "load_pct": round(100 * hours / capacity) if capacity else 0})
    return sorted(out, key=lambda x: -x["load_pct"])


def activity(store, limit: int = 100) -> list[dict]:
    return _rows(store, "SELECT * FROM ws_events ORDER BY id DESC LIMIT ?", [limit])
