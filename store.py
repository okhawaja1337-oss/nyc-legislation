#!/usr/bin/env python3
"""
store.py — tiny JSON-file persistence for things that should survive a rerun or
a process restart within a deployment (the Activity watchlist, saved searches).

Deliberately minimal and dependency-free. Data lives under an `.appstate/`
directory next to the app; that survives Streamlit reruns and process restarts
inside a running deployment (it does NOT survive a fresh redeploy or an
ephemeral container being reclaimed — for permanent history, the scheduled
backend keeps the durable record). Every operation is defensive: a corrupt or
unwritable file degrades to in-memory behavior rather than crashing the app.
"""

import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".appstate")


def _path(name):
    return os.path.join(_DIR, f"{name}.json")


def load(name, default=None):
    try:
        with open(_path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {} if default is None else default


def save(name, data):
    """Persist `data` (JSON-serializable). Returns True on success."""
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(name))  # atomic swap; never leaves a half-written file
        return True
    except Exception:
        return False


def available():
    """Whether the store directory is writable (for a UI hint)."""
    try:
        os.makedirs(_DIR, exist_ok=True)
        probe = os.path.join(_DIR, ".probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False
