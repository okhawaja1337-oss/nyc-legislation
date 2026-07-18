#!/usr/bin/env python3
"""
council.py — optional bridge to an LLM Council Plus server.

When a council server is running (default http://localhost:8001), analysis can be
routed through its 3-stage multi-model deliberation (individual responses →
anonymous peer ranking → chairman synthesis) instead of a single model — the
"use the council for all analysis" preference. This is a thin REST client around
`POST /api/ask` (execution_mode "full"); if the server is unreachable, callers
fall back to the single model, so nothing breaks when it's offline.

Set LLM_COUNCIL_URL to point at a remote server. Pure `requests`; defensive.
"""

import os
import time

try:
    import requests
except ImportError:
    requests = None

DEFAULT_URL = os.environ.get("LLM_COUNCIL_URL", "http://localhost:8001")

# Short cache of the last health probe so we don't hammer the server every call.
_health_cache = {"url": None, "ok": False, "ts": 0.0}


def _now():
    try:
        return time.monotonic()
    except Exception:
        return 0.0


def available(base_url=None, ttl=30):
    """True if the council server answers its health check (cached briefly)."""
    if not requests:
        return False
    url = (base_url or DEFAULT_URL).rstrip("/")
    c = _health_cache
    if c["url"] == url and (_now() - c["ts"]) < ttl:
        return c["ok"]
    ok = False
    try:
        r = requests.get(f"{url}/api/health", timeout=4)
        ok = r.status_code == 200
    except Exception:
        ok = False
    _health_cache.update({"url": url, "ok": ok, "ts": _now()})
    return ok


def deliberate(content, base_url=None, web_search=False, models=None,
               chairman_model=None, execution_mode="full", timeout=300):
    """Run a council deliberation; return the chairman's synthesized text or None.

    execution_mode: "full" (3 stages), "chat_ranking" (2 stages), "chat_only" (1).
    """
    if not requests:
        return None
    url = (base_url or DEFAULT_URL).rstrip("/")
    payload = {"content": content, "web_search": bool(web_search),
               "execution_mode": execution_mode}
    if models:
        payload["models"] = models
    if chairman_model:
        payload["chairman_model"] = chairman_model
    try:
        r = requests.post(f"{url}/api/ask", json=payload, timeout=timeout)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    # chat_only+1 model -> {"response":...}; full -> {"response": <chairman>, ...};
    # chat_only+N -> {"responses":[{response}...]}
    if isinstance(data, dict):
        if data.get("response"):
            return data["response"]
        resps = data.get("responses")
        if isinstance(resps, list) and resps:
            best = next((x.get("response") for x in resps if x.get("response") and not x.get("error")), None)
            return best
    return None
