#!/usr/bin/env python3
"""
The source repositories, kept live.

The office's data has two homes on GitHub. The budget repository holds the
adopted Schedule C for six fiscal years, the Section 254 capital books, the
Transparency Resolutions, the tax forecast and the reconciliation workbooks.
The legislation repository holds the Council record. Both keep moving: a new
Transparency Resolution lands, a capital book gets a corrected reissue, the
Legistar mirror syncs another week of matters.

Until now the system read a copy of those files once and then had no idea the
originals had moved. That is the worst kind of wrong -- confidently current,
quietly months old -- and it is exactly what a briefing cannot afford.

So: a manifest. Every file in every source repository is hashed. Sync compares
the hash on disk to the hash the system last ingested, and answers three
questions the office actually asks.

  What is here that we have never loaded?      status 'new'
  What moved upstream since we loaded it?      status 'stale'
  What did we load, from exactly which bytes?  status 'fresh'

A stale file is routed back to the ingester that owns it and the reload is
recorded against the new hash. Nothing is inferred from a timestamp, because
a clone rewrites every mtime and a file that merely got re-checked-out has not
changed at all.

When there is no network, sync says so and works from the clone on disk. A
sync that silently reports success on month-old files would be worse than no
sync, so the offline path is a named outcome, never a fallback pretending to
be a refresh.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.store import Store

# Where a clone lives if the caller does not say. Beside the lake, not inside
# the package: a package directory gets replaced wholesale on upgrade.
DEFAULT_ROOT = Path(os.environ.get("D49_REPO_ROOT", Path.home() / ".d49-sources"))

GIT_TIMEOUT = 900          # a shallow clone of the budget books is ~150 MB
SKIP_DIRS = {".git", "__pycache__", ".github", "node_modules"}


class Repo:
    """One source repository and what the office uses it for."""

    def __init__(self, key: str, url: str, purpose: str,
                 subdirs: tuple[str, ...] = (), branch: str = "",
                 role: str = "data"):
        self.key = key
        self.url = url
        self.purpose = purpose
        self.subdirs = subdirs
        self.branch = branch
        # "data" repositories are ingested. "code" repositories are tracked so
        # the office knows which version it is running, and deliberately not
        # walked: this system's own repository contains the lake it builds,
        # and hashing a 144 MB output as though it were an input would be both
        # slow and a lie about where the data came from.
        self.role = role

    def path(self, root: Path | None = None) -> Path:
        return Path(root or DEFAULT_ROOT) / self.key

    def as_dict(self) -> dict:
        return {"key": self.key, "url": self.url, "purpose": self.purpose,
                "role": self.role}


REPOS: dict[str, Repo] = {
    "budget": Repo(
        "budget",
        "https://github.com/okhawaja1337-oss/Budget",
        "Adopted Schedule C FY2022-FY2027, Section 254 capital books, the "
        "Transparency Resolutions, the Council tax forecast, and the office's "
        "own District 49 reconciliation workbooks.",
        subdirs=("data", ""),
    ),
    "legislation": Repo(
        "legislation",
        "https://github.com/okhawaja1337-oss/nyc-legislation",
        "This system's own code. Tracked so the office knows which version it "
        "is running; not a data source -- the lake in it is this system's "
        "output, and the legislative data upstream of it is the Legistar "
        "mirror below.",
        role="code",
    ),
    "legistar": Repo(
        "legistar",
        "https://github.com/jehiah/nyc_legislation",
        "The upstream flat-file mirror of the NYC Council Legistar Web API: "
        "every introduction, resolution and land use item since 1996, with "
        "the Council's own summary, the full bill text, attachments and the "
        "action history. This is where new legislation appears first.",
        # Twenty-one thousand small JSON files. Hashing each one on every sync
        # would take longer than reading them, so this repository syncs by its
        # own last_sync.json high-water mark instead of by manifest.
        subdirs=("last_sync.json",),
        role="bulk",
    ),
}

# Which ingester owns which file. Matched on the file name, because the same
# book sits at the repository root in one year and under data/ in the next.
HANDLERS: tuple[tuple[str, str], ...] = (
    ("_schedule_c.json", "fiscal"),
    ("_transparency_reso_", "fiscal"),
    ("tax_forecast", "fiscal"),
    ("Reconciliation", "workbook"),
    ("Full_Breakdown", "breakdown"),
    ("SOURCES.md", "sources"),
    (".pdf", "reference"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def handler_for(path: Path | str) -> str:
    name = Path(path).name
    for needle, handler in HANDLERS:
        if needle.lower() in name.lower():
            return handler
    return ""


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """
    The file's content hash.

    Content, not mtime. Cloning rewrites every timestamp, so an mtime check
    would report the entire budget repository as changed every single sync and
    the office would learn to ignore the report inside a week.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------- git ----
def _git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return False, "git is not installed on this machine"
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} timed out after {GIT_TIMEOUT}s"
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def fetch(repo: Repo, root: Path | None = None) -> dict:
    """
    Bring the clone up to date, or say plainly why it could not be.

    A shallow clone is enough: the office needs the current books, not their
    history. History is what the reconciliation workbook is for.
    """
    dest = repo.path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").exists():
        ok, out = _git(["pull", "--ff-only", "--depth", "1"], cwd=dest)
        action = "pull"
    else:
        args = ["clone", "--depth", "1"]
        if repo.branch:
            args += ["--branch", repo.branch]
        ok, out = _git(args + [repo.url, str(dest)])
        action = "clone"
    head = ""
    if (dest / ".git").exists():
        got, rev = _git(["rev-parse", "--short", "HEAD"], cwd=dest)
        head = rev if got else ""
    return {"repo": repo.key, "action": action, "ok": ok, "head": head,
            "path": str(dest), "present": dest.exists(),
            "detail": out[-400:] if out else ""}


# ------------------------------------------------------- manifest ----
def walk(repo: Repo, root: Path | None = None) -> Iterable[Path]:
    base = repo.path(root)
    if not base.exists():
        return []
    roots = [base / s for s in repo.subdirs if (base / s).exists()] or [base]
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        for p in sorted(r.rglob("*")):
            if not p.is_file() or p in seen:
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            seen.add(p)
            out.append(p)
    return out


def manifest(store: Store, repo: Repo, root: Path | None = None) -> dict:
    """Hash every file and classify it against what the system has ingested."""
    base = repo.path(root)
    known = {r["path"]: dict(r) for r in store.q(
        "SELECT * FROM repo_files WHERE repo = ?", (repo.key,))}
    stamp = _now()
    rows: list[tuple] = []
    counts = {"fresh": 0, "stale": 0, "new": 0, "unhandled": 0}

    for path in walk(repo, root):
        rel = f"{repo.key}/{path.relative_to(base).as_posix()}"
        digest = sha256(path)
        handler = handler_for(path)
        prior = known.pop(rel, None)
        if not handler:
            status = "unhandled"
        elif prior and prior.get("ingested_sha") == digest:
            status = "fresh"
        elif prior and prior.get("ingested_sha"):
            status = "stale"
        else:
            status = "new"
        counts[status] = counts.get(status, 0) + 1
        rows.append((rel, repo.key, digest, path.stat().st_size, stamp,
                     (prior or {}).get("ingested"),
                     (prior or {}).get("ingested_sha"),
                     handler, status, str(path)))

    with store.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO repo_files (path, repo, sha, bytes, seen, "
            "ingested, ingested_sha, handler, status, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        # A file that vanished upstream must not keep claiming to be fresh.
        if known:
            c.executemany("DELETE FROM repo_files WHERE path = ?",
                          [(p,) for p in known])
    return {"repo": repo.key, "files": len(rows), "counts": counts,
            "removed": sorted(known)}


# --------------------------------------------------------- ingest ----
def _run_handler(store: Store, handler: str, paths: list[Path],
                 base: Path) -> dict:
    """Route a group of changed files to the ingester that owns them."""
    if handler == "fiscal":
        from ..ingest.fiscal import ingest_all
        # The fiscal ingester works on a directory: it needs all six years
        # together to build the year-over-year series, so one changed file
        # still means reloading the set it belongs to.
        return ingest_all(store, paths[0].parent)
    if handler == "workbook":
        from ..ingest.office import ingest_workbook
        return ingest_workbook(store, sorted(paths)[-1])
    if handler == "breakdown":
        from ..ingest.office import ingest_breakdown
        return ingest_breakdown(store, paths[0])
    if handler == "sources":
        from ..ingest.office import ingest_sources_md
        return ingest_sources_md(store, paths[0])
    if handler == "reference":
        # The printed books are the evidence behind the parsed data, not a
        # second copy of it. They are registered so a citation can point at
        # the exact PDF, and deliberately not parsed: the office already did
        # that work by hand, and re-deriving it would put a second, weaker
        # answer next to a reconciled one.
        return {"registered": len(paths)}
    return {"skipped": handler}


def ingest_changed(store: Store, repo: Repo, root: Path | None = None,
                   force: bool = False) -> dict:
    """Re-ingest everything the manifest marked new or stale."""
    base = repo.path(root)
    wanted = ("new", "stale", "fresh") if force else ("new", "stale")
    rows = store.q(
        f"SELECT * FROM repo_files WHERE repo = ? AND handler != '' "
        f"AND status IN ({','.join('?' * len(wanted))})",
        (repo.key, *wanted))
    groups: dict[str, list[Path]] = {}
    for r in rows:
        groups.setdefault(r["handler"], []).append(Path(r["note"]))

    done: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for handler, paths in sorted(groups.items()):
        live = [p for p in paths if p.exists()]
        if not live:
            continue
        try:
            done[handler] = _run_handler(store, handler, live, base)
        except Exception as exc:                      # noqa: BLE001
            # One bad book must not abort the other five. The failure is
            # recorded against the file so the next sync retries it and the
            # office can see exactly which source is not loading.
            failed[handler] = f"{type(exc).__name__}: {exc}"
            continue
        stamp = _now()
        with store.tx() as c:
            c.executemany(
                "UPDATE repo_files SET ingested=?, ingested_sha=sha, "
                "status='fresh' WHERE path=?",
                [(stamp, f"{repo.key}/{p.relative_to(base).as_posix()}")
                 for p in live])
    if failed:
        with store.tx() as c:
            c.executemany(
                "UPDATE repo_files SET status='failed', note=note||' | '||? "
                "WHERE repo=? AND handler=?",
                [(msg, repo.key, h) for h, msg in failed.items()])
    return {"repo": repo.key, "ingested": done, "failed": failed}


# ----------------------------------------------------------- sync ----
def sync(store: Store, keys: Iterable[str] | None = None,
         root: Path | None = None, pull: bool = True,
         force: bool = False) -> dict:
    """
    Bring every source repository current and load whatever moved.

    Returns one honest record per repository. ``pulled`` false with ``present``
    true means the office is working from the clone on disk and the report is
    as old as that clone -- which is a usable answer, and a very different one
    from "up to date".
    """
    out: dict[str, Any] = {"at": _now(), "repos": {}}
    for key in (keys or REPOS.keys()):
        repo = REPOS.get(key)
        if repo is None:
            out["repos"][key] = {"error": f"unknown repository '{key}'"}
            continue
        record: dict[str, Any] = {"purpose": repo.purpose, "url": repo.url}
        if pull:
            record["fetch"] = fetch(repo, root)
        else:
            # Still read HEAD. "Which commit am I running" is answerable from
            # the clone on disk and does not need the network; reporting it as
            # unknown because we skipped the pull is a needless blind spot.
            got, rev = _git(["rev-parse", "--short", "HEAD"],
                            cwd=repo.path(root))
            record["fetch"] = {"ok": False, "action": "skipped",
                               "present": repo.path(root).exists(),
                               "head": rev if got else "",
                               "detail": "pull not requested"}
        if not repo.path(root).exists():
            record["state"] = "absent"
            record["says"] = ("Never cloned, and this run could not reach "
                              "GitHub. Nothing from this source is loaded.")
            out["repos"][key] = record
            continue
        record["state"] = "current" if record["fetch"].get("ok") else "on-disk"
        record["role"] = repo.role
        if repo.role == "data":
            record["manifest"] = manifest(store, repo, root)
            record["load"] = ingest_changed(store, repo, root, force=force)
        elif repo.role == "bulk":
            record["load"] = _sync_legistar(store, repo, root, force=force)
        record["says"] = _says(record)
        out["repos"][key] = record

    store.set_meta("repos.last_sync", out)
    store.journal("repos.sync", {"repos": list(out["repos"])})
    return out


def _sync_legistar(store: Store, repo: Repo, root: Path | None,
                   force: bool = False) -> dict:
    """
    Load whatever moved in the legislative mirror.

    Incremental by the LastModified stamp the mirror puts on every matter, so
    a daily sync reads the handful of bills that changed rather than all
    21,628. A full pass takes about two and a half minutes; an incremental one
    takes seconds, which is the difference between a sync the office runs and
    one it does not.
    """
    from ..ingest.legistar import ingest
    last = store.get_meta("legistar.last_ingest") or {}
    since = None if force else (last.get("high_water") or None)
    try:
        out = ingest(store, repo.path(root), since=since)
    except Exception as exc:                             # noqa: BLE001
        return {"failed": {"legistar": f"{type(exc).__name__}: {exc}"}}
    return {"ingested": {"legistar": out}}


def _says(record: dict) -> str:
    """One sentence a staffer can act on."""
    if record.get("role") == "bulk":
        got = (record.get("load", {}).get("ingested", {}).get("legistar")
               or {})
        if record.get("load", {}).get("failed"):
            return ("The legislative mirror failed to load: "
                    + "; ".join(record["load"]["failed"].values()))
        moved = (got.get("new", 0), got.get("updated", 0))
        where = ("Pulled from GitHub." if record["state"] == "current"
                 else "Could not reach GitHub; read the clone on disk.")
        if not any(moved):
            return f"{where} No legislation had changed since the last load."
        return (f"{where} {moved[0]} new bill(s), {moved[1]} with a changed "
                f"status, committee or sponsor count.")
    if record.get("role") == "code":
        head = record.get("fetch", {}).get("head") or "unknown"
        return (f"Code, not data. Running {head}."
                + ("" if record["state"] == "current"
                   else " Could not reach GitHub to check for a newer version."))
    counts = record.get("manifest", {}).get("counts", {})
    moved = counts.get("new", 0) + counts.get("stale", 0)
    failed = record.get("load", {}).get("failed", {})
    if record["state"] == "on-disk":
        base = ("Could not reach GitHub; working from the copy already on "
                "disk. Anything published upstream since then is not here.")
    elif moved:
        base = f"Pulled from GitHub. {moved} file(s) had moved and were reloaded."
    else:
        base = "Pulled from GitHub. Nothing had changed since the last load."
    if failed:
        base += (" " + f"{len(failed)} source(s) failed to load: "
                 + "; ".join(sorted(failed)) + ".")
    return base


def status(store: Store) -> dict:
    """What the office has, from where, and how old it is."""
    last = store.get_meta("repos.last_sync") or {}
    rows = store.q(
        "SELECT repo, status, COUNT(*) n, SUM(bytes) b, MAX(ingested) last "
        "FROM repo_files GROUP BY repo, status ORDER BY repo, status")
    by_repo: dict[str, Any] = {}
    for r in rows:
        entry = by_repo.setdefault(r["repo"], {
            "files": 0, "bytes": 0, "by_status": {}, "last_ingest": None,
            "purpose": REPOS[r["repo"]].purpose if r["repo"] in REPOS else "",
            "url": REPOS[r["repo"]].url if r["repo"] in REPOS else "",
            "role": REPOS[r["repo"]].role if r["repo"] in REPOS else "data"})
        entry["files"] += r["n"]
        entry["bytes"] += r["b"] or 0
        entry["by_status"][r["status"]] = r["n"]
        if r["last"] and (not entry["last_ingest"] or r["last"] > entry["last_ingest"]):
            entry["last_ingest"] = r["last"]
    for key, repo in REPOS.items():
        by_repo.setdefault(key, {
            "files": 0, "bytes": 0, "by_status": {}, "last_ingest": None,
            "purpose": repo.purpose, "url": repo.url, "role": repo.role})
        by_repo[key]["cloned"] = repo.path().exists()
        by_repo[key]["role"] = repo.role
        if repo.role == "bulk":
            # A bulk repository keeps no per-file manifest, so reporting
            # "0 files, never loaded" from the manifest table would say the
            # legislative record is missing when 21,627 matters are loaded.
            from ..ingest.legistar import coverage as legistar_coverage
            last = store.get_meta("legistar.last_ingest") or {}
            cov = legistar_coverage(store)
            # What is loaded, not what the last run happened to touch. An
            # incremental sync that found nothing new would otherwise report
            # "0 matters" for a corpus holding 21,627 of them.
            by_repo[key].update({
                "last_ingest": last.get("at"),
                "high_water": last.get("high_water"),
                "records": cov.get("matters"),
                "by_status": {k: v for k, v in
                              (("with text", (cov.get("with_text") or {}).get("texts")),
                               ("with summary", (cov.get("with_text") or {}).get("summaries")))
                              if v}})
    return {"last_sync": last.get("at"), "repos": by_repo,
            "root": str(DEFAULT_ROOT)}


def stale(store: Store) -> list[dict]:
    """Files that moved upstream and have not been reloaded."""
    return [dict(r) for r in store.q(
        "SELECT path, repo, handler, status, ingested FROM repo_files "
        "WHERE status IN ('stale','new','failed') ORDER BY repo, path")]


def adopt(store: Store, repo_key: str, path: Path | str) -> dict:
    """
    Point a repository at a clone that already exists elsewhere.

    The session that built this system had both repositories checked out
    already. Re-cloning 150 MB to reach files sitting on the same disk is
    waste, so a known-good clone can simply be adopted.
    """
    repo = REPOS.get(repo_key)
    if repo is None:
        return {"ok": False, "error": f"unknown repository '{repo_key}'"}
    src, dest = Path(path), repo.path()
    if not (src / ".git").exists() and not src.exists():
        return {"ok": False, "error": f"{src} is not a checkout"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.resolve() == src.resolve():
        return {"ok": True, "path": str(dest), "action": "already"}
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    try:
        dest.symlink_to(src, target_is_directory=True)
        action = "linked"
    except OSError:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
        action = "copied"
    return {"ok": True, "path": str(dest), "action": action,
            "source": str(src)}
