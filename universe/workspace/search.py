#!/usr/bin/env python3
"""
Search across everything, fast.

One box over bills, funding lines, organizations, contacts, hearings, staff
notes, deliverables, media and tasks. Fifty thousand funding rows and twenty
thousand matters have to answer in the time it takes to finish typing, so:

* **FTS5 with a porter tokenizer** does the text work; SQLite indexes do the
  field work. No table scan on a filter the UI actually offers.
* **The query language is the power.** ``pillar:health fy:2027 amount:>50000
  -org:"Project Hospitality"`` is parsed into bound parameters, never string
  concatenation. Bare words become a phrase-aware FTS match.
* **Facets come from the same WHERE clause** as the results, so the counts
  next to each filter are the counts you will actually get.
* **Typeahead** uses an FTS prefix query and returns in a few milliseconds.

Nothing here interpolates user text into SQL. Every value is bound.
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

# Fields a user may filter on by name, mapped to their column.
FIELDS: dict[str, str] = {
    "kind": "kind", "type": "kind",
    "year": "year", "fy": "fy",
    "status": "status", "committee": "committee", "cmte": "committee",
    "sponsor": "sponsor", "member": "sponsor", "funder": "sponsor",
    "agency": "agency", "channel": "channel", "district": "district",
    "source": "source_id", "org": "org", "pillar": "pillar",
    "ein": "ein", "amount": "amount",
}
NUMERIC = {"year", "fy", "district", "amount"}
# Enumerated columns match exactly; everything else is free text a user types
# only part of, so "org:Project Hospitality" finds "Project Hospitality, Inc."
EXACT = {"kind", "source_id", "district", "status"}
FACETS = ("kind", "fy", "status", "committee", "sponsor", "agency",
          "pillar", "channel")
SORTS = {
    "relevance": None,
    "newest": "updated DESC",
    "oldest": "updated ASC",
    "amount": "amount DESC",
    "title": "title ASC",
}

TOKEN = re.compile(r'''
    (?P<neg>-)?
    (?:(?P<field>[a-zA-Z_]+):)?
    (?:"(?P<phrase>[^"]*)"|(?P<word>\S+))
''', re.X)
RANGE = re.compile(r"^(?P<lo>-?[\d.]+)?\.\.(?P<hi>-?[\d.]+)?$")
COMPARE = re.compile(r"^(?P<op>>=|<=|>|<)(?P<val>-?[\d.]+)$")


class Query:
    """A parsed query: text terms for FTS, field clauses for SQL."""

    def __init__(self, raw: str = ""):
        self.raw = raw or ""
        self.text: list[str] = []
        self.not_text: list[str] = []
        self.clauses: list[tuple[str, str, Any]] = []   # (column, op, value)
        self.not_clauses: list[tuple[str, str, Any]] = []
        self._parse()

    def _parse(self) -> None:
        for m in TOKEN.finditer(self.raw):
            value = m.group("phrase") if m.group("phrase") is not None else m.group("word")
            if not value:
                continue
            field = (m.group("field") or "").lower()
            negated = bool(m.group("neg"))
            if field in ("or", "and"):          # bare operators: leave to FTS
                self.text.append(value)
                continue
            column = FIELDS.get(field)
            if column:
                for col, op, val in self._clause(column, value):
                    (self.not_clauses if negated else self.clauses).append((col, op, val))
            else:
                (self.not_text if negated else self.text).append(value)

    @staticmethod
    def _clause(column: str, value: str) -> list[tuple[str, str, Any]]:
        """Turn one field:value into SQL operators, including ranges."""
        if column in NUMERIC:
            rng = RANGE.match(value)
            if rng:
                out = []
                if rng.group("lo"):
                    out.append((column, ">=", float(rng.group("lo"))))
                if rng.group("hi"):
                    out.append((column, "<=", float(rng.group("hi"))))
                return out
            cmp_ = COMPARE.match(value)
            if cmp_:
                return [(column, cmp_.group("op"), float(cmp_.group("val")))]
            try:
                return [(column, "=", float(value))]
            except ValueError:
                return [(column, "LIKE", f"%{value}%")]
        if "*" in value:
            return [(column, "LIKE", value.replace("*", "%"))]
        if column in EXACT:
            return [(column, "=", value)]
        # Free text: contains, case-insensitive (SQLite LIKE is ASCII-insensitive).
        return [(column, "LIKE", f"%{value}%")]

    # ------------------------------------------------------------ rendering --
    def where(self) -> tuple[str, list[Any]]:
        sql, params = ["1=1"], []
        for col, op, val in self.clauses:
            sql.append(f"AND {col} {op} ?")
            params.append(val)
        for col, op, val in self.not_clauses:
            sql.append(f"AND COALESCE({col} {op} ?, 0) = 0")
            params.append(val)
        return " ".join(sql), params

    def fts(self) -> str:
        """An FTS5 MATCH expression. Quoting keeps user text inert."""
        parts = []
        for t in self.text:
            if t.upper() in ("OR", "AND", "NOT"):
                parts.append(t.upper())
            else:
                parts.append('"' + t.replace('"', "") + '"')
        expr = " ".join(parts)
        for t in self.not_text:
            expr += ' NOT "' + t.replace('"', "") + '"'
        return expr.strip()

    def __bool__(self) -> bool:
        return bool(self.text or self.not_text or self.clauses or self.not_clauses)


# ------------------------------------------------------------------ search --
def search(store, q: str = "", filters: dict | None = None, limit: int = 60,
           offset: int = 0, sort: str = "relevance",
           with_facets: bool = True) -> dict:
    """Run a search and return rows, facet counts and timing."""
    started = time.perf_counter()
    filters = {k: v for k, v in (filters or {}).items() if v not in ("", None)}
    query = Query(q)

    # UI dropdowns are clauses too, so facets and results always agree.
    for key, val in filters.items():
        col = FIELDS.get(key)
        if col:
            for c, op, v in Query._clause(col, str(val)):
                query.clauses.append((c, op, v))

    where, params = query.where()
    match = query.fts()

    if match:
        base = ("FROM workspace_records r JOIN workspace_fts f ON f.key = r.key "
                f"WHERE {where.replace('1=1', 'workspace_fts MATCH ?')}")
        bound = [match, *params]
        order = "ORDER BY bm25(workspace_fts)" if sort == "relevance" \
            else f"ORDER BY r.{SORTS.get(sort) or 'updated DESC'}"
        select = ("SELECT r.*, snippet(workspace_fts, 2, '<b>', '</b>', '…', 16) AS snip, "
                  "bm25(workspace_fts) AS score ")
    else:
        base = f"FROM workspace_records r WHERE {where}"
        bound = list(params)
        order = f"ORDER BY r.{SORTS.get(sort) or 'updated DESC'}"
        select = "SELECT r.*, '' AS snip, 0 AS score "

    try:
        rows = [dict(x) for x in store.q(
            f"{select}{base} {order} LIMIT ? OFFSET ?", [*bound, limit, offset])]
        total = store.scalar(f"SELECT COUNT(*) {base}", bound) or 0
    except Exception as exc:                     # a bad FTS expression, usually
        return {"query": q, "error": f"That search could not be run: {exc}",
                "rows": [], "total": 0, "facets": {}, "ms": 0}

    facets = _facets(store, base, bound) if with_facets else {}
    return {
        "query": q, "filters": filters, "sort": sort,
        "rows": rows, "total": total, "limit": limit, "offset": offset,
        "facets": facets,
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "parsed": {"text": query.text, "not_text": query.not_text,
                   "clauses": [[c, o, v] for c, o, v in query.clauses],
                   "excluded": [[c, o, v] for c, o, v in query.not_clauses]},
    }


FACET_CAP = 40000          # rows scanned for facet counts before sampling


def _facets(store, base: str, bound: list, top: int = 12,
            cap: int = FACET_CAP) -> dict:
    """Counts per value for every facet, in a single pass.

    Eight separate GROUP BY queries cost eight scans of the same matching set
    and dominated the response. One scan that pulls just the facet columns and
    tallies them in Python is an order of magnitude cheaper, and the counts are
    identical because they come from the same WHERE clause.

    Above ``cap`` matching rows the scan is capped and the counts are scaled,
    flagged as estimated -- a facet chip does not need to be exact to be useful,
    and the total beside it always is.
    """
    cols = ", ".join(f"r.{c}" for c in FACETS)
    try:
        rows = store.q(f"SELECT {cols} {base} LIMIT ?", [*bound, cap])
    except Exception:
        return {}

    tally: dict[str, dict[Any, int]] = {c: {} for c in FACETS}
    for row in rows:
        for col in FACETS:
            v = row[col]
            if v is None or v == "":
                continue
            tally[col][v] = tally[col].get(v, 0) + 1

    scanned = len(rows)
    estimated = scanned >= cap
    out: dict[str, Any] = {}
    for col, counts in tally.items():
        if not counts:
            continue
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
        out[col] = [{"value": v, "n": n} for v, n in ranked]
    if estimated:
        out["_estimated"] = True
        out["_scanned"] = scanned
    return out


def suggest(store, prefix: str, limit: int = 8) -> list[dict]:
    """Typeahead. An FTS prefix query, so it answers in milliseconds."""
    prefix = (prefix or "").strip()
    if len(prefix) < 2:
        return []
    safe = prefix.replace('"', "")
    try:
        rows = store.q(
            "SELECT r.key, r.kind, r.title, r.year, r.org "
            "FROM workspace_records r JOIN workspace_fts f ON f.key = r.key "
            "WHERE workspace_fts MATCH ? ORDER BY bm25(workspace_fts) LIMIT ?",
            [f'"{safe}"*', limit])
    except Exception:
        return []
    return [dict(r) for r in rows]


# Words that carry no signal in a question. Dropping them is what lets
# "What did we fund for seniors?" behave like "fund seniors".
STOPWORDS = {
    "a", "об", "about", "all", "an", "and", "any", "are", "as", "at", "be",
    "been", "but", "by", "can", "did", "do", "does", "for", "from", "get",
    "give", "had", "has", "have", "how", "i", "in", "is", "it", "its", "know",
    "me", "much", "my", "need", "of", "on", "or", "our", "out", "please",
    "show", "so", "some", "tell", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "up", "us", "want", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your",
}


def search_nl(store, question: str, limit: int = 60, **kw) -> dict:
    """Search the way a person asks, not the way an index expects.

    A question typed in full sentences would otherwise require every word --
    including "what" and "did" -- to appear in the same record, which matches
    nothing. So retrieval degrades in three steps and reports which one
    answered:

    1. the query exactly as typed, operators and all;
    2. content words only, all of them required;
    3. content words, any of them, ranked by relevance.

    The first step that returns anything wins, so a precise query is never
    loosened and a conversational one still finds the record.
    """
    exact = search(store, question, limit=limit, **kw)
    if exact.get("total") or exact.get("error"):
        return {**exact, "strategy": "exact"}

    q = Query(question)
    if q.clauses or q.not_clauses:
        # Field filters were given; keep them and loosen only the free text.
        fields = " ".join(
            f'{name}:"{val}"' for name, _, val in
            [(k, o, v) for k, o, v in q.clauses]
            for name in [next((f for f, c in FIELDS.items() if c == k), k)])
    else:
        fields = ""

    content = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'&-]*", question or "")
               if w.lower() not in STOPWORDS and len(w) > 2]
    if not content:
        return {**exact, "strategy": "exact"}

    tightened = search(store, (fields + " " + " ".join(content)).strip(),
                       limit=limit, **kw)
    if tightened.get("total"):
        return {**tightened, "strategy": "content-words",
                "note": f"Matched on: {', '.join(content)}"}

    loosened = search(store, (fields + " " + " OR ".join(content)).strip(),
                      limit=limit, **kw)
    return {**loosened, "strategy": "any-word",
            "note": (f"No record contains all of {', '.join(content)}; "
                     f"showing records matching any of them, best first.")}


def record(store, key: str) -> dict | None:
    r = store.one("SELECT * FROM workspace_records WHERE key=?", [key])
    if not r:
        return None
    out = dict(r)
    out["notes"] = [dict(x) for x in store.q(
        "SELECT * FROM workspace_comments WHERE task_id=? ORDER BY id", [f"record:{key}"])]
    out["changes"] = [dict(x) for x in store.q(
        "SELECT * FROM workspace_changes WHERE record_key=? ORDER BY id DESC LIMIT 25",
        [key])]
    out["tasks"] = [dict(x) for x in store.q("""
        SELECT t.id, t.title, t.status, t.owner FROM ws_links l
        JOIN workspace_tasks t ON t.id = l.task_id WHERE l.record_key = ?""", [key])]
    return out
