"""Read startup settings from the civ's .env files, so every restart keeps them.

Why: the birth writes PORTAL_PUBLIC_URL (and, for trials, TRIAL_CONFIG_PATH)
to ~/.env AND to the portal's process env. A restart that does not carry the
process env (the watchdog restarting the portal through start.sh, a container
restart) used to drop both. Loading them from ~/.env at startup keeps them.

Rules:
    * The process env wins. A key is taken from a file only when the process
      env does not have it (unset, or set to an empty/blank value).
    * Files, in order: ~/.env, then $CIV_ROOT/.env when that is a different
      file. The first file that has a non-empty value for a key wins; inside
      one file the last line for a key wins (as when the file is sourced).
    * Only the keys in PERSISTED_KEYS are loaded, so a stray PORT or HOME line
      in ~/.env cannot change how the portal starts.
    * Missing or unreadable files are skipped silently. The file is parsed,
      never executed.

Syntax: KEY=VALUE lines. Blank lines and lines starting with # are ignored.
An optional leading `export ` is allowed. Values may be wrapped in single or
double quotes; an unquoted value ends at ` #` (an inline comment).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, MutableMapping, Optional

# The settings a portal restart must keep. start.sh exports the same list.
PERSISTED_KEYS = ("PORTAL_PUBLIC_URL", "TRIAL_CONFIG_PATH")

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
        return value[1:]  # unterminated quote: keep the rest as-is
    # Unquoted: a # that follows whitespace starts a comment.
    value = re.split(r"\s#", value, maxsplit=1)[0]
    return value.strip()


def parse_env_file(path) -> dict:
    """KEY -> value for every KEY=VALUE line. Missing/unreadable file -> {}."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def env_file_paths(environ: Optional[MutableMapping] = None) -> list:
    """~/.env, then $CIV_ROOT/.env if it is a different file."""
    env = os.environ if environ is None else environ
    home = (env.get("HOME") or "").strip() or str(Path.home())
    paths = [Path(home) / ".env"]
    civ_root = (env.get("CIV_ROOT") or "").strip()
    if civ_root:
        civ_env = Path(civ_root) / ".env"
        try:
            same = civ_env.resolve() == paths[0].resolve()
        except OSError:
            same = civ_env == paths[0]
        if not same:
            paths.append(civ_env)
    return paths


def load_env_defaults(
    keys: Iterable[str] = PERSISTED_KEYS,
    paths: Optional[Iterable] = None,
    environ: Optional[MutableMapping] = None,
) -> dict:
    """Fill missing `keys` in the process env from the .env files.

    Returns {key: value} for what was loaded (empty when nothing was).
    """
    env = os.environ if environ is None else environ
    files = env_file_paths(env) if paths is None else [Path(p) for p in paths]
    wanted = [k for k in keys if not (env.get(k) or "").strip()]
    loaded: dict = {}
    if not wanted:
        return loaded
    for path in files:
        values = parse_env_file(path)
        for key in wanted:
            if key in loaded:
                continue
            value = (values.get(key) or "").strip()
            if value:
                loaded[key] = value
    for key, value in loaded.items():
        env[key] = value
    return loaded
