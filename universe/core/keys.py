#!/usr/bin/env python3
"""
Credential resolution.

One rule governs this module: **a secret never enters the repository.** Not in
a source file, not in a config file inside the working tree, not in a commit.
Keys live in the environment or in a file under the user's home directory with
owner-only permissions, and this module is the only thing that reads them.

Resolution order, first hit wins:

    1. an explicit argument (a caller that already has the key)
    2. the environment variable
    3. the file named by $D49_CONFIG
    4. ~/.d49/config.json                      <- where `universe key set` writes
    5. <repo>/.d49-local.json                  <- gitignored escape hatch

Anthropic keys are a *list*, not a single value. The office was given two; a
key can be revoked, rate-limited or rotated mid-week, and a briefing that dies
because the first key is cold is a briefing that did not get written. The
caller asks for a key, uses it, and on an auth or rate error calls `rotate()`
to get the next one.

Nothing here ever prints a key. `status()` reports *where* a credential came
from and its last four characters, which is enough to tell two keys apart and
not enough to use one.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

HOME_CONFIG = Path.home() / ".d49" / "config.json"
REPO_LOCAL = Path(__file__).resolve().parents[2] / ".d49-local.json"

# name -> (env var, whether it is a list)
CREDENTIALS: dict[str, tuple[str, bool]] = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY", True),
    "captions_api_key": ("CAPTIONS_API_KEY", False),
    "youtube_api_key": ("YOUTUBE_API_KEY", False),
    "google_api_key": ("GOOGLE_API_KEY", False),
    "google_oauth_token": ("GOOGLE_OAUTH_TOKEN", False),
    "google_service_account": ("GOOGLE_SERVICE_ACCOUNT_JSON", False),
    "socrata_app_token": ("SOCRATA_APP_TOKEN", False),
    "legistar_token": ("LEGISTAR_TOKEN", False),
}

# Non-secret settings that may also live in the config file.
SETTINGS = ("d49_calendar_id", "calendar_ics_url", "sheets", "model",
            "captions_provider", "llm_council_url")


# ------------------------------------------------------------------ files ----
def _read(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}


def config_files() -> list[Path]:
    """Every config file consulted, in resolution order."""
    paths = []
    named = os.environ.get("D49_CONFIG")
    if named:
        paths.append(Path(named).expanduser())
    paths += [HOME_CONFIG, REPO_LOCAL]
    return paths


def load() -> dict:
    """Merged config. Earlier files win; the environment wins over all files."""
    merged: dict = {}
    for path in reversed(config_files()):        # later files first, so earlier overwrite
        merged.update(_read(path))
    return merged


def _source_of(name: str) -> str:
    env, _ = CREDENTIALS.get(name, (name.upper(), False))
    if os.environ.get(env):
        return f"environment ({env})"
    for path in config_files():
        if name in _read(path):
            return str(path)
    return "not set"


# ------------------------------------------------------------- accessors ----
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # One env var may carry several keys, comma or whitespace separated.
        return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def get_all(name: str) -> list[str]:
    """Every value configured for a credential, in preference order."""
    env, _ = CREDENTIALS.get(name, (name.upper(), False))
    found = _as_list(os.environ.get(env))
    if found:
        return found
    cfg = load()
    found = _as_list(cfg.get(name))
    if found:
        return found
    # Accept a pluralised key in the file: "anthropic_api_keys": [...]
    return _as_list(cfg.get(f"{name}s"))


def get(name: str, default: str | None = None, index: int = 0) -> str | None:
    values = get_all(name)
    if index < len(values):
        return values[index]
    return values[0] if values and index else default


def setting(name: str, default: Any = None) -> Any:
    env = os.environ.get(name.upper())
    if env:
        return env
    return load().get(name, default)


def redact(value: str | None) -> str:
    if not value:
        return "—"
    tail = value[-4:]
    return f"…{tail} ({len(value)} chars)"


def status() -> dict:
    """What is configured, where it came from, and nothing usable."""
    out = {}
    for name in CREDENTIALS:
        values = get_all(name)
        out[name] = {
            "configured": bool(values),
            "count": len(values),
            "source": _source_of(name),
            "fingerprints": [redact(v) for v in values],
        }
    return out


# --------------------------------------------------------------- writing ----
def save(values: dict, path: Path | None = None) -> Path:
    """
    Write credentials to the user's home config, owner-read-only.

    Refuses to write anywhere inside the repository. A secret in the working
    tree is one `git add -A` away from being public, and that mistake is not
    recoverable -- the key has to be revoked, not un-pushed.
    """
    target = Path(path).expanduser() if path else HOME_CONFIG
    target = target.resolve() if target.exists() else target
    repo = Path(__file__).resolve().parents[2]
    try:
        inside = target.resolve().is_relative_to(repo)
    except (OSError, ValueError):
        inside = False
    if inside and target.name != ".d49-local.json":
        raise ValueError(
            f"refusing to write credentials inside the repository ({target}). "
            f"Use {HOME_CONFIG} or an environment variable.")

    existing = _read(target)
    existing.update({k: v for k, v in values.items() if v is not None})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)     # 0600
    except OSError:
        pass
    return target


def clear(name: str, path: Path | None = None) -> bool:
    target = Path(path).expanduser() if path else HOME_CONFIG
    data = _read(target)
    if name not in data and f"{name}s" not in data:
        return False
    data.pop(name, None)
    data.pop(f"{name}s", None)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return True
