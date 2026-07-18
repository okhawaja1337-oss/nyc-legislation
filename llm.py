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

    def __init__(self, api_key=None, model=FAST_MODEL, pause=0.0,
                 use_council=False, council_url=None):
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.pause = pause
        self.s = requests.Session() if requests else None
        self.use_council = bool(use_council)
        self.council_url = council_url

    @property
    def ready(self):
        # Ready if we can reach a model — either the council or a direct key.
        if self.use_council and self._council_ready():
            return True
        return bool(self.key) and self.s is not None

    def _council_ready(self):
        if not self.use_council:
            return False
        try:
            import council as _council
            return _council.available(self.council_url)
        except Exception:
            return False

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
                 system=None, timeout=180, allow_council=True):
        """Return the model's text for a single-turn prompt.

        When council routing is on (and `allow_council`), the prompt goes through
        the multi-model council deliberation; on any failure it falls back to the
        single Anthropic model so nothing breaks.
        """
        if self.use_council and allow_council:
            try:
                import council as _council
                content = (system + "\n\n" + prompt) if system else prompt
                resp = _council.deliberate(content, base_url=self.council_url,
                                           web_search=allow_web, timeout=max(timeout, 300))
                if resp:
                    return resp.strip()
            except Exception:
                pass  # fall back to the single model below
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
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
        """Return a parsed JSON object from the model, recovering from fences/prose.

        JSON stays on the single model (council synthesis isn't reliably JSON)."""
        txt = self.complete(prompt, max_tokens=max_tokens, model=model, timeout=timeout,
                            allow_council=False)
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
