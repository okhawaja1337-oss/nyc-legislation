#!/usr/bin/env python3
"""
retrieval.py — fast, local, dependency-free semantic-ish search.

A TF-IDF inverted index over the loaded bills so natural-language queries
("tenants facing eviction in the outer boroughs") return the most relevant bills
instantly, and any bill can surface its most-similar neighbours — all in-process,
no embedding API, no network. For a session's worth of bills (hundreds to a few
thousand) this is effectively instant.

Pure stdlib (re, math, collections). Deterministic and unit-testable.
"""

import math
import re
from collections import Counter, defaultdict

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(("the a an and or of to in for on at by with from as is are be this that "
             "it its shall will would may a4 no not all any such other than into "
             "law local relating requiring providing act bill").split())


def _tokens(text):
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) > 2 and t not in _STOP]


class Index:
    """Build once per corpus, then query many times."""

    def __init__(self):
        self.docs = []            # list of doc ids
        self.meta = {}            # id -> arbitrary payload (the row)
        self.tf = {}              # id -> {term: count}
        self.norm = {}            # id -> vector norm (for cosine)
        self.df = Counter()       # term -> #docs
        self.idf = {}
        self.postings = defaultdict(list)  # term -> [ids]
        self.n = 0

    @classmethod
    def build(cls, rows, text_of):
        """rows: iterable of payloads; text_of(row) -> str; row must have an id via _id()."""
        ix = cls()
        for row in rows:
            did = _id(row)
            toks = _tokens(text_of(row))
            if not toks:
                continue
            ix.docs.append(did)
            ix.meta[did] = row
            tf = Counter(toks)
            ix.tf[did] = tf
            for term in tf:
                ix.df[term] += 1
                ix.postings[term].append(did)
        ix.n = len(ix.docs)
        for term, d in ix.df.items():
            ix.idf[term] = math.log((1 + ix.n) / (1 + d)) + 1.0
        for did, tf in ix.tf.items():
            ix.norm[did] = math.sqrt(sum((c * ix.idf.get(t, 0.0)) ** 2 for t, c in tf.items())) or 1.0
        return ix

    def search(self, query, top=25):
        """Ranked [(row, score)] for a free-text query."""
        q = Counter(_tokens(query))
        if not q or not self.n:
            return []
        qw = {t: c * self.idf.get(t, 0.0) for t, c in q.items() if t in self.idf}
        if not qw:
            return []
        qnorm = math.sqrt(sum(w * w for w in qw.values())) or 1.0
        scores = defaultdict(float)
        for term, w in qw.items():
            for did in self.postings.get(term, ()):
                scores[did] += w * (self.tf[did][term] * self.idf.get(term, 0.0))
        ranked = sorted(((did, s / (self.norm[did] * qnorm)) for did, s in scores.items()),
                        key=lambda x: -x[1])[:top]
        return [(self.meta[did], round(sc, 4)) for did, sc in ranked]

    def related(self, row, top=8):
        """Most similar rows to a given row (excludes itself)."""
        did = _id(row)
        if did not in self.tf:
            return []
        base = self.tf[did]
        query = " ".join(t for t, c in base.most_common(20) for _ in range(min(c, 3)))
        return [(r, s) for r, s in self.search(query, top=top + 1) if _id(r) != did][:top]


def _id(row):
    return row.get("MatterId") or row.get("File") or id(row)


def bill_text(row):
    """The searchable text for a bill row."""
    return " ".join([row.get("File", "") or "", row.get("Title", "") or "",
                     row.get("Type", "") or "", row.get("Topic tags", "") or "",
                     row.get("Boroughs named", "") or "", row.get("Prime Sponsor", "") or "",
                     row.get("Name", "") or ""])
