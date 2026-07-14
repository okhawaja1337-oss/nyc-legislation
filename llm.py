#!/usr/bin/env python3
"""
llm.py — shared, self-contained Anthropic client for the v2 features
(Briefing Studio, Policy Lab, cross-level analysis).

Kept independent of app.py on purpose: app.py is the Streamlit entry point and
runs top-to-bottom, so importing from it would re-execute the whole UI. This
module only needs `requests` and the standard library.

Everything is defensive: no key -> RuntimeError the UI can catch and turn into a
friendly "add your key" message; network hiccups are retried; malformed JSON is
recovered where possible.
"""

import json
import os
import re
import time

try:
    import requests
except ImportError:  # offline / unit-test contexts
    requests = None

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Sensible defaults. Callers may override per-call.
FAST_MODEL = "claude-haiku-4-5-20251001"     # cheap, matches the rest of the app
SMART_MODEL = "claude-sonnet-5"              # richer prose for briefings / ideation


class LLM:
    """Thin wrapper over the Anthropic Messages API.

    Usage:
        llm = LLM(api_key=key)
        text = llm.complete("Write three bullets about ferries.")
        obj  = llm.complete_json("Return {\"a\":1}")
    """

    def __init__(self, api_key=None, model=FAST_MODEL, pause=0.0):
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.pause = pause
        self.s = requests.Session() if requests else None

    @property
    def ready(self):
        return bool(self.key) and self.s is not None

    def _post(self, body, timeout=150):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        if not self.s:
            raise RuntimeError("requests is not available")
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        last = None
        for attempt in range(4):
            try:
                r = self.s.post(ANTHROPIC_URL, headers=headers, json=body, timeout=timeout)
            except requests.exceptions.RequestException as e:  # type: ignore
                last = e
                time.sleep(min(2 ** attempt, 12))
                continue
            if r.status_code == 200:
                if self.pause:
                    time.sleep(self.pause)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504, 529):
                time.sleep(min(2 ** attempt, 12))
                continue
            r.raise_for_status()
        if last:
            raise last
        raise RuntimeError("LLM request failed")

    def complete(self, prompt, max_tokens=1600, model=None, allow_web=False,
                 system=None, timeout=180):
        """Return the model's text for a single-turn prompt."""
        body = {"model": model or self.model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        if allow_web:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
        data = self._post(body, timeout=timeout)
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()

    def complete_json(self, prompt, max_tokens=1400, model=None, timeout=120):
        """Return a parsed JSON object from the model, recovering from fences/prose."""
        txt = self.complete(prompt, max_tokens=max_tokens, model=model, timeout=timeout)
        return extract_json(txt)


def extract_json(txt):
    """Best-effort parse of a JSON object/array out of an LLM response."""
    s = re.sub(r"^```(json)?|```$", "", (txt or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return {}
