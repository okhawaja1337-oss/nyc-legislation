#!/usr/bin/env python3
"""
sources/media.py — portraits for officials, with a graceful fallback.

Real photos are best-effort from Wikipedia's public REST/API (keyless): we search
for the person and take their page thumbnail. Many NYC officials have Wikipedia
pages, so this fills most portraits in a live deployment; anyone without a match
falls back to a clean, deterministic initials avatar rendered as inline SVG, so
a card is never empty and nothing ever fabricates a face.

Defensive: any network failure returns None and the caller shows the avatar.
"""

import hashlib
import time
from urllib.parse import quote

try:
    import requests
except ImportError:
    requests = None

_WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"

# Palette for initials avatars (works on a light UI).
_AV_COLORS = ["#1d4ed8", "#0891b2", "#7c3aed", "#c026d3", "#db2777",
              "#e11d48", "#ea580c", "#ca8a04", "#16a34a", "#0d9488"]


def initials(name):
    parts = [p for p in (name or "").replace(".", "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def avatar_svg(name, size=64):
    """A deterministic inline-SVG initials avatar (no network). Returns SVG markup."""
    h = int(hashlib.sha1((name or "?").encode("utf-8", "ignore")).hexdigest(), 16)
    color = _AV_COLORS[h % len(_AV_COLORS)]
    fs = int(size * 0.4)
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{name}">'
            f'<rect width="{size}" height="{size}" rx="{size//2}" fill="{color}"/>'
            f'<text x="50%" y="50%" dy="0.35em" text-anchor="middle" fill="#fff" '
            f'font-family="Segoe UI,Helvetica,Arial,sans-serif" font-size="{fs}" '
            f'font-weight="700">{initials(name)}</text></svg>')


def avatar_data_uri(name, size=64):
    import base64
    svg = avatar_svg(name, size)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()


def wiki_photo(name, context="New York City Council", timeout=15):
    """Best-effort portrait URL + page URL from Wikipedia. (photo_url, page_url) or (None, page_url/None)."""
    if not requests or not (name or "").strip():
        return None, None
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{name} {context}".strip(), "gsrlimit": 1,
        "prop": "pageimages|info", "piprop": "thumbnail", "pithumbsize": 400,
        "inprop": "url", "origin": "*",
    }
    for attempt in range(2):
        try:
            r = requests.get(_WIKI_SEARCH, params=params, timeout=timeout,
                             headers={"User-Agent": "nyc-legislative-intelligence/1.0"})
            if r.status_code == 200:
                pages = ((r.json() or {}).get("query") or {}).get("pages") or {}
                for _, p in pages.items():
                    thumb = (p.get("thumbnail") or {}).get("source")
                    return thumb, p.get("fullurl") or (
                        "https://en.wikipedia.org/wiki/" + quote((p.get("title") or name).replace(" ", "_")))
                return None, None
            time.sleep(1)
        except requests.exceptions.RequestException:
            time.sleep(1)
    return None, None


def portrait_html(name, photo_url=None, size=64):
    """A circular portrait <img> if a photo URL is given, else the SVG avatar."""
    if photo_url:
        return (f'<img src="{photo_url}" width="{size}" height="{size}" alt="{name}" '
                f'style="width:{size}px;height:{size}px;border-radius:50%;object-fit:cover;'
                f'border:2px solid #e2e8f5;" '
                f'onerror="this.outerHTML=\'\'"/>')
    return avatar_svg(name, size)
