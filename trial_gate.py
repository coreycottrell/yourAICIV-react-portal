"""yourAICIV trial gate — portal side of the shared 7-day trial contract.

Contract (shared with the birth template, which writes the record at birth):

    {
      "trial": true,
      "started_at": "<ISO8601 UTC>",
      "duration_days": 7,
      "expires_at": "<ISO8601 UTC>",
      "payment_url": "https://buy.stripe.com/...",
      "brand": "yourAICIV",
      "reseller": "...",
      "model": "MiniMax-M3"
    }

    * No record, or "trial": false  -> not a trial -> no gating anywhere.
    * GET /api/trial -> {"trial", "day", "days_left", "expires_at", "expired",
                         "payment_url"} (+ "duration_days", "config_error", additive).
    * Trial (active OR expired): the operator tools are refused server-side:
      HTTP OPERATOR_HTTP_* -> 403 {"error": "operator_tools_locked"},
      WS   OPERATOR_WS_PATHS -> closed with code 4403.
    * Expired: every other /api/* request is refused with HTTP 402 and every
      /ws/* connection is closed with code 4402. The SPA shell and its static
      assets are still served so the browser can render the payment screen.
      Nothing on disk is touched: converting ("trial": false) restores full
      access within seconds.

Where the record lives (ONE canonical source):
    TRIAL_CONFIG_PATH — the operator copy the birth template publishes OUTSIDE
    the civ tree (template: `apply_trial_profile.py apply --operator-copy
    "$TRIAL_OPERATOR_COPY"`, convention /etc/aiciv/trial.json, root-owned,
    bind-mounted read-only into the portal). The AiCIV cannot write it.

    If TRIAL_CONFIG_PATH is unset (local development, or an install without
    an operator copy), the civ's own file is used: $CIV_ROOT/config/trial.json
    ($CIV_ROOT defaults to $HOME). The AiCIV can write that file, so a startup
    warning says so; production trials must set TRIAL_CONFIG_PATH.

    TRIAL_CONFIG_PATH is read from the process env, or, when the process env
    does not have it, from ~/.env / $CIV_ROOT/.env at import (env_file.py).
    The birth writes it to both, so a restart that drops the process env (the
    watchdog restarting the portal through start.sh) keeps the operator copy
    instead of silently falling back to the civ-writable file.

Failure policy: fail CLOSED when there is any sign this is a trial.
    * The record exists but is unreadable, not valid JSON, not an object, has a
      "trial" value other than true/false, or is a trial with no usable dates
      -> treated as an EXPIRED trial (402/4402, operator tools locked), with
      "config_error": true in /api/trial, and logged.
    * The record is missing but a trial marker exists (TRIAL_CONFIG_PATH is set
      and the civ's own config/trial.json exists, i.e. this civ was born as a
      trial) -> same fail-closed state. The operator restores the copy.
    * The record is missing and there is no marker -> not a trial (paid birth:
      the template writes no trial file at all).
Conversion is the template's `convert` command, which rewrites the record
atomically; a hand edit is no longer the conversion path, so a broken record
means something is wrong and must not silently ungate a trial.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from env_file import load_env_defaults

# Startup: a TRIAL_CONFIG_PATH missing from the process env comes from the
# civ's .env files (the process env wins). portal_server does this too, before
# importing this module; repeating it keeps this module right on its own.
load_env_defaults(("TRIAL_CONFIG_PATH",))

DEFAULT_DURATION_DAYS = 7
# Public Stripe payment link from the shared contract. Overridable per install.
DEFAULT_PAYMENT_URL = os.environ.get(
    "TRIAL_PAYMENT_URL", "https://buy.stripe.com/5kQeVe8Xe9D53GZdLb1Fe06"
)

# Paths that stay reachable after expiry (everything else under /api is 402).
ALLOWED_WHEN_EXPIRED = frozenset({"/api/trial"})

# Operator tools (Terminal, Sessions, Browser): refused for the whole trial,
# active or expired. They reach the AI's shell/tmux directly, which a trial
# client must not have. Paid installs are unaffected.
OPERATOR_HTTP_PATHS = frozenset({"/api/panes", "/api/inject/pane", "/api/resume"})
OPERATOR_HTTP_PREFIXES = ("/api/browser/",)
OPERATOR_WS_PATHS = frozenset({"/ws/terminal", "/ws/browser"})

_CACHE_TTL_SECS = 5.0
_cache: dict = {"path": None, "mtime": None, "at": 0.0, "raw": None}


def civ_trial_path() -> Path:
    """The civ's own config/trial.json (civ-writable; also the trial marker)."""
    civ_root = os.environ.get("CIV_ROOT", "").strip() or str(Path.home())
    return Path(civ_root) / "config" / "trial.json"


def trial_config_path() -> Path:
    """The authoritative record: TRIAL_CONFIG_PATH, else the civ's own file."""
    override = os.environ.get("TRIAL_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    return civ_trial_path()


def describe_source() -> str:
    """One line for the startup log: where the trial record is read from."""
    path = trial_config_path()
    if os.environ.get("TRIAL_CONFIG_PATH", "").strip():
        return f"trial record: {path} (TRIAL_CONFIG_PATH, operator copy)"
    return (
        f"trial record: {path} (civ-writable; set TRIAL_CONFIG_PATH to the "
        "operator copy for production trials)"
    )


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_payment_url(value) -> str:
    # Only ever hand an https URL to the browser (never javascript:/data:).
    if isinstance(value, str) and value.strip().lower().startswith("https://"):
        return value.strip()
    return DEFAULT_PAYMENT_URL


_logged_errors: set = set()


def _log_once(msg: str) -> None:
    if msg not in _logged_errors:
        _logged_errors.add(msg)
        print(f"[trial] {msg}")


class TrialConfigError(Exception):
    """The trial record cannot be trusted: fail closed."""


def _trial_marker(path: Path) -> Optional[str]:
    """Why we believe this civ is a trial even though `path` is missing."""
    civ = civ_trial_path()
    if os.environ.get("TRIAL_CONFIG_PATH", "").strip() and civ != path:
        try:
            if civ.exists():
                return f"the civ's {civ} exists (born as a trial)"
        except OSError:
            return f"the civ's {civ} cannot be checked"
    return None


def _read_raw() -> Optional[dict]:
    """Read the trial record with a short mtime-aware cache.

    Returns None for "not a trial", the dict otherwise.
    Raises TrialConfigError when the record cannot be trusted (fail closed).
    """
    path = trial_config_path()
    now = time.monotonic()
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache.update(path=None)
        marker = _trial_marker(path)
        if marker:
            raise TrialConfigError(f"{path} is missing but {marker}")
        return None
    except OSError as exc:  # e.g. permission denied on the directory
        _cache.update(path=None)
        raise TrialConfigError(f"{path}: cannot stat ({exc})")
    key = (st.st_mtime_ns, st.st_size)
    if (
        _cache["path"] == str(path)
        and _cache["mtime"] == key
        and now - _cache["at"] < _CACHE_TTL_SECS
    ):
        if isinstance(_cache["raw"], TrialConfigError):
            raise _cache["raw"]
        return _cache["raw"]
    result: object
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise TrialConfigError(f"{path}: not a JSON object")
        if data.get("trial") not in (True, False):
            raise TrialConfigError(f'{path}: "trial" must be true or false')
        result = data
    except TrialConfigError as exc:
        result = exc
    except Exception as exc:  # unreadable / invalid JSON
        result = TrialConfigError(f"{path}: unreadable ({exc})")
    _cache.update(path=str(path), mtime=key, at=now, raw=result)
    if isinstance(result, TrialConfigError):
        raise result
    return result  # type: ignore[return-value]


def configured_model() -> str:
    """The "model" recorded in the trial record (kept after conversion), or ""."""
    try:
        raw = _read_raw()
    except TrialConfigError:
        return ""
    model = raw.get("model") if raw else None
    return model.strip() if isinstance(model, str) else ""


def fail_closed_state(reason: str = "") -> dict:
    """What /api/trial reports when the record cannot be trusted."""
    if reason:
        _log_once(f"FAIL CLOSED: {reason}")
    return {
        "trial": True,
        "day": DEFAULT_DURATION_DAYS,
        "days_left": 0,
        "duration_days": DEFAULT_DURATION_DAYS,
        "expires_at": "",
        "expired": True,
        "payment_url": DEFAULT_PAYMENT_URL,
        "config_error": True,
    }


def compute_state(raw: Optional[dict], now: Optional[datetime] = None) -> dict:
    """Pure function: contract dict -> /api/trial response."""
    not_trial = {
        "trial": False,
        "day": 0,
        "days_left": 0,
        "duration_days": 0,
        "expires_at": "",
        "expired": False,
        "payment_url": "",
    }
    if not raw or raw.get("trial") is not True:
        return not_trial

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        duration = int(raw.get("duration_days") or DEFAULT_DURATION_DAYS)
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION_DAYS
    duration = max(1, duration)

    started = _parse_iso(raw.get("started_at"))
    expires = _parse_iso(raw.get("expires_at"))
    if expires is None and started is not None:
        expires = started + timedelta(days=duration)
    if expires is None:
        return fail_closed_state("trial record has neither expires_at nor started_at")
    if started is None:
        started = expires - timedelta(days=duration)

    expired = now >= expires
    elapsed_days = (now - started).total_seconds() / 86400.0
    day = min(duration, max(1, int(math.floor(elapsed_days)) + 1))
    remaining = (expires - now).total_seconds() / 86400.0
    days_left = 0 if expired else max(1, int(math.ceil(remaining)))

    return {
        "trial": True,
        "day": duration if expired else day,
        "days_left": days_left,
        "duration_days": duration,
        "expires_at": _iso(expires),
        "expired": expired,
        "payment_url": _safe_payment_url(raw.get("payment_url")),
    }


def trial_state(now: Optional[datetime] = None) -> dict:
    try:
        raw = _read_raw()
    except TrialConfigError as exc:
        return fail_closed_state(str(exc))
    return compute_state(raw, now)


def is_expired_trial() -> bool:
    """For background loops: skip work that would spend a turn on an expired trial."""
    state = trial_state()
    return bool(state.get("trial") and state.get("expired"))


def _expired_body(state: dict) -> bytes:
    return json.dumps(
        {
            "error": "trial_expired",
            "message": (
                "Your 7-day trial has ended. Everything your AiCIV built is saved "
                "and comes back the moment you subscribe."
            ),
            "payment_url": state.get("payment_url") or DEFAULT_PAYMENT_URL,
            "expires_at": state.get("expires_at", ""),
        }
    ).encode()


def is_operator_path(kind: str, path: str) -> bool:
    if kind == "websocket":
        return path in OPERATOR_WS_PATHS
    return path in OPERATOR_HTTP_PATHS or path.startswith(OPERATOR_HTTP_PREFIXES)


_OPERATOR_LOCKED_BODY = json.dumps(
    {
        "error": "operator_tools_locked",
        "message": "Operator tools (Terminal, Sessions, Browser) are turned off during the free trial.",
    }
).encode()


async def _send_json(send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _close_ws(receive, send, code: int, reason: str) -> None:
    # Accept, then close, so browsers actually see the code
    # (closing before accept surfaces as an HTTP 403 handshake failure).
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code, "reason": reason})


class TrialGateMiddleware:
    """Pure-ASGI middleware enforcing the trial server-side.

    Trial expired (or record untrusted -> fail closed):
        HTTP  /api/* (except ALLOWED_WHEN_EXPIRED) -> 402 JSON
        WS    /ws/*                                -> closed with code 4402
    Trial active or expired:
        HTTP  operator paths -> 403 {"error": "operator_tools_locked"}
        WS    operator paths -> closed with code 4403
    Everything else (SPA shell, assets, /health) passes through.
    """

    def __init__(self, app, state_fn: Callable[[], dict] = trial_state):
        self.app = app
        self.state_fn = state_fn

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        path = scope.get("path", "")
        gated_http = kind == "http" and path.startswith("/api/") and path not in ALLOWED_WHEN_EXPIRED
        gated_ws = kind == "websocket" and path.startswith("/ws/")
        if not (gated_http or gated_ws):
            return await self.app(scope, receive, send)

        state = self.state_fn()
        if not state.get("trial"):
            return await self.app(scope, receive, send)

        if state.get("expired"):
            if gated_ws:
                return await _close_ws(receive, send, 4402, "trial_expired")
            return await _send_json(send, 402, _expired_body(state))

        if is_operator_path(kind, path):
            if gated_ws:
                return await _close_ws(receive, send, 4403, "operator_tools_locked")
            return await _send_json(send, 403, _OPERATOR_LOCKED_BODY)

        return await self.app(scope, receive, send)
