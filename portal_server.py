#!/usr/bin/env python3
"""yourAICIV Portal Server — per-AiCIV backend for the yourAICIV web portal.

Serves the React SPA (react-portal/dist) and the /api + /ws endpoints it uses.
Auth via Bearer token (.portal-token). JSONL-based chat history.
Trial mode: see trial_gate.py (config/trial.json at the civ root).
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

# Ensure HOME is set correctly for the aiciv user.
# docker exec -u aiciv inherits the caller's HOME (often /root) rather than /home/aiciv.
# Fix it here so Path.home() returns the right path throughout the server.
if os.environ.get("HOME", "/root") == "/root" and os.path.isdir("/home/aiciv"):
    os.environ["HOME"] = "/home/aiciv"

# Keep the birth settings across every restart path: a key missing from the
# process env (e.g. the watchdog restarted us through start.sh) is taken from
# ~/.env or $CIV_ROOT/.env. The process env still wins. Runs before trial_gate
# is imported so it sees TRIAL_CONFIG_PATH. See env_file.py.
from env_file import load_env_defaults  # noqa: E402

_ENV_FILE_LOADED = load_env_defaults()

from site_proxy import ClientSiteMiddleware, registry_path as client_sites_registry
from trial_gate import (
    TrialGateMiddleware,
    configured_model,
    describe_source as trial_source,
    is_expired_trial,
    trial_state,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = Path(os.environ.get("PORTAL_TOKEN_FILE", "") or (SCRIPT_DIR / ".portal-token"))
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()
PORTAL_VERSION = "1.0.1"
RELEASE_NOTES_FILE = SCRIPT_DIR / "release_notes.json"
# Auto-detect CIV_NAME and HUMAN_NAME from identity file — works in any fleet container.
# Falls back to generic defaults if identity file not found (local dev).
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = _identity.get("civ_id", "aiciv")
    HUMAN_NAME = _identity.get("human_name", "User")
except Exception:
    CIV_NAME = "aiciv"
    HUMAN_NAME = "User"
# Auto-derive Claude project JSONL directory.
# Claude encodes the PROJECT directory (git root) by replacing '/' with '-'.
# We scan ALL project directories for JSONL files to find the active one.
_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Primary LOG_ROOT: try the most recently modified project directory
LOG_ROOT = _PROJECTS_DIR  # fallback — _get_all_session_log_paths handles the real search
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORTAL_CHAT_LOG = SCRIPT_DIR / "portal-chat.jsonl"
UPLOADS_DIR = Path.home() / "portal_uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
# Paths to CIV log files used for client data import
AGENTS_DB    = SCRIPT_DIR / "agents.db"
AGENTMAIL_DB = SCRIPT_DIR / "agentmail.db"

# AgentCal configuration
def _read_env_key(key: str) -> str:
    """Read a key from ~/.env file."""
    env_path = Path.home() / ".env"
    if env_path.exists():
        for ln in env_path.read_text().splitlines():
            if ln.startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    return os.environ.get(key, "")

AGENTCAL_API_KEY = _read_env_key("AICIVCAL_API_KEY")
# Service URLs come from ~/.env or the environment. No defaults on purpose:
# an unset URL disables that integration instead of calling someone's host.
AGENTCAL_BASE = _read_env_key("AICIVCAL_URL")
AGENTCAL_CALENDAR_ID_FILE = SCRIPT_DIR / ".aicivcal-calendar-id"
AGENTCAL_CALENDAR_ID = AGENTCAL_CALENDAR_ID_FILE.read_text().strip() if AGENTCAL_CALENDAR_ID_FILE.exists() else ""
AGENTCAL_FIRED_IDS_FILE = SCRIPT_DIR / ".agentcal-fired-ids.json"

# AgentSheets configuration
AGENTSHEETS_URL = _read_env_key("AGENTSHEETS_URL")
AGENTSHEETS_API_KEY = _read_env_key("AGENTSHEETS_API_KEY") or ""

# AgentAuth configuration (Ed25519 challenge-response → JWT)
AGENTAUTH_URL = _read_env_key("AGENTAUTH_URL") or ""
AGENTAUTH_PRIVATE_KEY = _read_env_key("AGENTAUTH_PRIVATE_KEY") or ""
AGENTAUTH_PUBLIC_KEY = _read_env_key("AGENTAUTH_PUBLIC_KEY") or ""
_agentauth_jwt: str = ""
_agentauth_jwt_exp: float = 0.0

async def _get_agentauth_jwt() -> str:
    """Return a cached AgentAuth JWT, refreshing via challenge-response if needed."""
    global _agentauth_jwt, _agentauth_jwt_exp
    if not AGENTAUTH_URL or not AGENTAUTH_PRIVATE_KEY:
        return ""
    # Return cached token if still valid (5-min buffer)
    if _agentauth_jwt and time.time() < (_agentauth_jwt_exp - 300):
        return _agentauth_jwt
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        # Build private key from raw seed bytes
        priv_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(AGENTAUTH_PRIVATE_KEY))
        civ_id = CIV_NAME or "aiciv"
        async with httpx.AsyncClient(timeout=10) as client:
            # Step 1: request challenge
            resp = await client.post(f"{AGENTAUTH_URL}/challenge", json={"civ_id": civ_id})
            resp.raise_for_status()
            challenge = resp.json().get("challenge", "")
            if not challenge:
                print("[agentauth] empty challenge returned")
                return ""
            # Step 2: sign challenge (decode b64 → sign raw bytes → encode b64)
            import base64 as _b64
            challenge_bytes = _b64.b64decode(challenge)
            sig_bytes = priv_key.sign(challenge_bytes)
            signature_b64 = _b64.b64encode(sig_bytes).decode()
            # Step 3: verify and get JWT
            resp = await client.post(f"{AGENTAUTH_URL}/verify", json={"civ_id": civ_id, "challenge": challenge, "signature": signature_b64})
            resp.raise_for_status()
            data = resp.json()
            _agentauth_jwt = data.get("token", "") or data.get("jwt", "")
            # TTL from server (default 1h)
            expires_in = data.get("expires_in", 3600)
            _agentauth_jwt_exp = time.time() + expires_in
            if isinstance(_agentauth_jwt_exp, str):
                _agentauth_jwt_exp = datetime.fromisoformat(_agentauth_jwt_exp.replace("Z", "+00:00")).timestamp()
            print(f"[agentauth] JWT acquired for {civ_id}, expires in {int(_agentauth_jwt_exp - time.time())}s")
            return _agentauth_jwt
    except Exception as e:
        print(f"[agentauth] JWT refresh failed: {e}")
        return _agentauth_jwt  # return stale token if available

async def _get_civauth_headers() -> dict:
    """Return auth headers for CivOS services. Uses AgentAuth JWT if available, else per-service keys."""
    jwt = await _get_agentauth_jwt()
    if jwt:
        return {"Authorization": f"Bearer {jwt}"}
    # Fallback: per-service API keys (e.g. AgentCal)
    if AGENTCAL_API_KEY:
        return {"Authorization": f"Bearer {AGENTCAL_API_KEY}"}
    return {}


# Allowed directories for file downloads (generic — works in any customer container)
DOWNLOAD_ALLOWED_DIRS = [
    Path.home() / "exports",
    Path.home() / "to-human",
    SCRIPT_DIR,
    Path.home() / "portal_uploads",
]

# OAuth flow state
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OAUTH_URL_PATTERN = re.compile(r'https://[^\s\x1b\x07\]]*oauth/authorize\?[^\s\x1b\x07\]]+')
_captured_oauth_url = None

# Evolution markers
EVOLUTION_DONE_MARKER = Path.home() / "memories" / "identity" / ".evolution-done"
FIRST_BOOT_MARKER = Path.home() / ".first-boot-fired"
FIRST_BOOT_SKILL_PATH = Path.home() / ".claude" / "skills" / "first-visit-evolution" / "SKILL.md"
FIRST_BOOT_PROMPT_PATH = Path.home() / ".claude" / "skills" / "first-visit-evolution" / "prompt.txt"
# Minimum seconds between launching the evolution Claude and typing the
# awakening prompt (the TUI must be ready for input). 30s = the timing every
# birth has used so far.
try:
    FIRST_BOOT_SETTLE_S = max(0.0, float(os.environ.get("PORTAL_FIRST_BOOT_SETTLE_S", "30")))
except ValueError:
    FIRST_BOOT_SETTLE_S = 30.0

if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token in {TOKEN_FILE} (not printed)")



def _run_subprocess_sync(cmd, timeout=5, check=False, capture=False, text=False):
    """Run a subprocess with mandatory timeout. Used by sync callers only."""
    try:
        return subprocess.run(
            cmd, timeout=timeout, check=check,
            capture_output=capture, text=text,
            stderr=subprocess.DEVNULL if not capture else None,
        )
    except subprocess.TimeoutExpired:
        return None
    except subprocess.CalledProcessError:
        return None
    except Exception:
        return None


async def _run_subprocess_async(cmd, timeout=5, check=False):
    """Run a subprocess WITHOUT blocking the asyncio event loop.
    This is the ONLY way subprocess should be called from async code."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, timeout=timeout, check=check,
                    stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                )
            ),
            timeout=timeout + 2  # extra 2s for executor overhead
        )
    except (asyncio.TimeoutError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError, Exception):
        return None


async def _run_subprocess_output(cmd, timeout=5):
    """Run subprocess and capture output without blocking the event loop."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, timeout=timeout, capture_output=True,
                    text=True, check=False,
                )
            ),
            timeout=timeout + 2
        )
        return result.stdout if result and result.returncode == 0 else ""
    except (asyncio.TimeoutError, Exception):
        return ""


# Cached tmux session name — refreshed every 30s to avoid repeated subprocess calls
_tmux_session_cache: tuple = (0.0, "")  # (last_check_time, session_name)
_TMUX_CACHE_TTL = 30.0


def get_tmux_session() -> str:
    """Find the live primary Claude Code session for this container.
    Result is cached for 30s to avoid hammering tmux."""
    global _tmux_session_cache
    now = time.time()
    if now - _tmux_session_cache[0] < _TMUX_CACHE_TTL and _tmux_session_cache[1]:
        return _tmux_session_cache[1]

    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name],
                                    stderr=subprocess.DEVNULL, timeout=3)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    result = None

    # FIRST: Find the currently attached session — mirrors telegram_bridge logic.
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        for line in out.splitlines():
            parts = line.strip().rsplit(":", 1)
            if len(parts) == 2 and parts[1].strip().isdigit() and int(parts[1].strip()) > 0:
                attached = parts[0].strip()
                if attached:
                    result = attached
                    break
    except Exception:
        pass

    if not result:
        marker = Path.home() / ".current_session"
        if marker.exists():
            name = marker.read_text().strip()
            if name and alive(name):
                result = name

    if not result:
        try:
            out = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                          stderr=subprocess.DEVNULL, text=True, timeout=3)
            sessions = out.strip().splitlines()
            for line in sessions:
                if CIV_NAME in line.lower():
                    result = line.strip()
                    break
            if not result and sessions:
                result = sessions[0].strip()
        except Exception:
            pass

    if not result:
        result = f"{CIV_NAME}-primary"

    _tmux_session_cache = (now, result)
    return result


def _find_current_session_id():
    """Find the current Claude Code session ID from history.jsonl."""
    try:
        if not HISTORY_FILE.exists():
            return None
        with HISTORY_FILE.open("r") as f:
            f.seek(0, 2)
            length = f.tell()
            window = min(16384, length)
            f.seek(max(0, length - window))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                proj = entry.get("project", "")
                if proj and (CIV_NAME in proj or str(Path.home()) in proj):
                    return entry.get("sessionId")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


_project_jsonl_cache: tuple = (0.0, [])  # (last_scan_time, results)
_PROJECT_JSONL_CACHE_TTL = 30.0  # Re-scan filesystem at most every 30 seconds

def _find_all_project_jsonl():
    """Find all JSONL session files across ALL project directories, sorted by mtime descending.
    Cached for 30s to avoid hammering the filesystem on every WebSocket poll."""
    global _project_jsonl_cache
    now = time.time()
    if now - _project_jsonl_cache[0] < _PROJECT_JSONL_CACHE_TTL and _project_jsonl_cache[1]:
        return _project_jsonl_cache[1]

    all_logs = []
    try:
        if not _PROJECTS_DIR.exists():
            return []
        for proj_dir in _PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for jf in proj_dir.glob("*.jsonl"):
                try:
                    all_logs.append((jf.stat().st_mtime, jf))
                except OSError:
                    continue
        all_logs.sort(key=lambda x: x[0], reverse=True)
    except Exception:
        pass
    result = [p for _, p in all_logs]
    _project_jsonl_cache = (now, result)
    return result


def _get_all_session_log_paths(max_files=3):
    """Get paths to recent JSONL session logs across ALL project directories, ordered oldest-first.
    Reduced from 10 to 3 files for performance — parsing 10x 50-97MB files every 0.8s was burning 66% CPU."""
    # Chat reads only THIS civ's own project dir (derived from $HOME, e.g. /home/aiciv ->
    # -home-aiciv). Other project dirs hold headless sessions run from worktrees/tools
    # (plugins, SDK runs, isolated workflow agents) whose "user" turns are tool prompts,
    # never the human; reading them made tool prompts appear as the human.
    own = "-" + str(Path.home()).strip("/").replace("/", "-")
    logs = [p for p in _find_all_project_jsonl() if Path(p).parent.name == own]
    return list(reversed(logs[:max_files]))


def _despace(text):
    """Collapse spaced-out text like 'H  e  l  l  o' back to 'Hello'.
    Some older JSONL sessions store text with spaces between every character."""
    if not text or len(text) < 6:
        return text
    # Check if text follows the pattern: char, spaces, char, spaces...
    # Sample first 40 chars to detect the pattern
    sample = text[:40]
    # Pattern: single non-space char followed by 1-2 spaces, repeating
    spaced_chars = 0
    i = 0
    while i < len(sample):
        if i + 1 < len(sample) and sample[i] != " " and sample[i + 1] == " ":
            spaced_chars += 1
            i += 1
            while i < len(sample) and sample[i] == " ":
                i += 1
        else:
            i += 1
    # If >60% of non-space chars are followed by spaces, it's spaced text
    non_space = sum(1 for c in sample if c != " ")
    if non_space > 0 and spaced_chars / non_space > 0.6:
        # Collapse: take every non-space char, but preserve intentional word gaps
        result = []
        i = 0
        while i < len(text):
            if text[i] != " ":
                result.append(text[i])
                i += 1
                # Skip the inter-character spaces (1-2 spaces)
                spaces = 0
                while i < len(text) and text[i] == " ":
                    spaces += 1
                    i += 1
                # 3+ spaces likely means intentional word boundary
                if spaces >= 3:
                    result.append(" ")
            else:
                i += 1
        return "".join(result)
    return text


def _is_real_user_message(text):
    """Check if a user message is a real human message (not system/teammate noise)."""
    if not text or len(text) < 2:
        return False
    # Telegram messages from user - always real
    if "[TELEGRAM" in text:
        return True
    # Portal-sent messages (stored in portal chat log)
    if text.startswith("[PORTAL]"):
        return True
    # Filter out noise
    noise_markers = [
        "<teammate-message", "<system-reminder", "system-reminder",
        "Base directory for this skill", "teammate_id=",
        "<tool_result", "<function_calls", "hook success",
        "Session Ledger", "MEMORY INJECTION", "<task-notification",
        "[Image: source:", "PHOTO saved to:",
        "This session is being continued from a previous",
        "Called the Read tool", "Called the Bash tool",
        "Called the Write tool", "Called the Glob tool",
        "Called the Grep tool", "Result of calling",
        "Context restored",
        "Summary:  ",                  # Agent task summaries
        "` regex", "` sed", "| sed",   # Code snippets leaking as messages
        "re.search(r'", "re.DOTALL",
        "<command-name>", "<command-message>",  # CLI commands
        "<command-args>", "<local-command",
        "local-command-caveat", "local-command-stdout",
        "Compacted (ctrl+o",           # Compaction messages
        "&& [ -x ", "| cut -d",        # Shell code fragments
        "[portal",                     # Portal messages from session JSONL (already in portal-chat.jsonl)
    ]
    for marker in noise_markers:
        if marker in text[:300]:
            return False
    # Skip messages that look like code/config (too many special chars)
    special = sum(1 for c in text[:200] if c in '{}[]|\\`$()#')
    if len(text) < 200 and special > len(text) * 0.15:
        return False
    return True


def _clean_user_text(text):
    """Clean up user message text for display."""
    # Strip Telegram prefix for cleaner display
    if "[TELEGRAM" in text:
        # Format: [TELEGRAM private:NNN from @Username] actual message
        idx = text.find("]")
        if idx > 0:
            return text[idx + 1:].strip()
    if text.startswith("[PORTAL] "):
        return text[9:]
    return text


def _is_real_assistant_message(text):
    """Check if an assistant message is substantive (not just tool calls or noise)."""
    if not text or len(text) < 10:
        return False
    stripped = text.strip()
    # Reject short non-alphanumeric noise (pipes, brackets, stray chars)
    if len(stripped) <= 3 and not any(c.isalnum() for c in stripped):
        return False
    return True


_jsonl_cache: dict = {}  # path -> (mtime, messages, fsize, last_parse_time)
_TAIL_BYTES = 500_000   # read last 500KB of large files (reduced from 2MB — stability fix 2026-03-14)
_CACHE_MIN_INTERVAL = 10.0  # Don't re-parse any file more than once per 10 seconds (was 3s — CPU stability fix)

# Cache for portal-chat.jsonl — avoids re-reading 8k-line file on every /api/chat/history request
# Tuple: (mtime: float, fsize: int, messages: list)
_portal_chat_cache: tuple = (0.0, 0, [])

# IDs already written to portal-chat.jsonl — prevents duplicate mirror writes
_portal_log_ids: set = set()

# Active WebSocket connections for pushing thinking blocks
_chat_ws_clients: set = set()

# Hashes of thinking blocks already sent — prevents duplicates across reconnects
_sent_thinking_hashes: set = set()


def _trim_portal_chat_log(max_entries=3000):
    """Trim portal-chat.jsonl to last max_entries, deduplicating by ID.
    Prevents unbounded growth. Called periodically in the background."""
    global _portal_chat_cache
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        entries = []
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if len(entries) <= max_entries:
            return  # No trim needed
        # Sort by timestamp, deduplicate, keep last max_entries
        entries.sort(key=lambda m: float(m.get("timestamp", 0) or 0))
        seen: dict = {}
        for i, e in enumerate(entries):
            seen[e.get("id", str(i))] = e
        trimmed = list(seen.values())[-max_entries:]
        # Atomic write
        import tempfile, os
        tmp = PORTAL_CHAT_LOG.parent / f".portal-chat-trim-{os.getpid()}.jsonl"
        with tmp.open("w") as f:
            for e in trimmed:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, PORTAL_CHAT_LOG)
        # Invalidate cache so next read picks up trimmed version
        _portal_chat_cache = (0.0, 0, [])
        print(f"[portal] Trimmed portal-chat.jsonl: {len(entries)} → {len(trimmed)} entries")
    except Exception as e:
        print(f"[portal] Trim failed: {e}")


def _init_portal_log_ids():
    """Load IDs already in portal-chat.jsonl so we don't re-mirror them."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    mid = entry.get("id")
                    if mid:
                        _portal_log_ids.add(mid)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass


def _mirror_to_portal_log(msg):
    """Write a discovered session message to portal-chat.jsonl so it survives refreshes."""
    mid = msg.get("id")
    if not mid:
        return
    # Guard: never persist noise-only messages to the log (prevents stale pipe/char glitches)
    msg_text = msg.get("text", "").strip()
    if not msg_text or len(msg_text) < 3:
        return
    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
        return  # Skip stray pipe/bracket/noise artifacts
    if mid in _portal_log_ids:
        # Already mirrored — skip. Overwriting every time was causing 22s+ history loads
        # by rewriting the entire 3.4MB portal-chat.jsonl hundreds of times per request.
        return
    _portal_log_ids.add(mid)
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(msg) + "\n")
    except Exception:
        pass


def _overwrite_portal_log_entry(mid: str, updated_msg: dict) -> None:
    """Atomically rewrite portal-chat.jsonl replacing the entry for mid with updated_msg.
    Uses temp-file + rename for crash safety (Fix 4)."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        lines = []
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get("id") == mid:
                        lines.append(json.dumps(updated_msg) + "\n")
                    else:
                        lines.append(line)
                except json.JSONDecodeError:
                    lines.append(line)
        tmp = PORTAL_CHAT_LOG.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(lines))
        tmp.replace(PORTAL_CHAT_LOG)
    except Exception:
        pass


def _parse_jsonl_messages_from_file(log_path):
    """Parse a single JSONL log into clean chat messages.
    Tail-reads large files and caches by mtime for fast repeated calls."""
    messages = []
    if not log_path or not log_path.exists():
        return messages

    try:
        stat = log_path.stat()
        mtime = stat.st_mtime
        fsize = stat.st_size
        cached = _jsonl_cache.get(str(log_path))
        # Cache key includes BOTH mtime AND file size to catch writes within same second
        if cached and cached[0] == mtime and cached[2] == fsize:
            return cached[1]
        # Rate-limit re-parsing: even if file changed, don't re-parse more often than _CACHE_MIN_INTERVAL
        # This prevents CPU spin on large actively-growing JSONL files (70MB+ during long sessions)
        if cached and len(cached) >= 4 and (time.time() - cached[3]) < _CACHE_MIN_INTERVAL:
            return cached[1]

        # Read only the tail of large files to avoid parsing megabytes each poll
        with log_path.open("rb") as fb:
            if stat.st_size > _TAIL_BYTES:
                fb.seek(-_TAIL_BYTES, 2)
                fb.readline()  # skip partial first line
            raw = fb.read()
        lines_iter = raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return messages

    try:
        for line in lines_iter:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Handle queue-operation enqueue entries — these contain the actual user input text
                if entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue":
                    raw_content = entry.get("content", "")
                    if isinstance(raw_content, str) and len(raw_content) >= 2:
                        raw_content = raw_content.strip()
                        if raw_content and _is_real_user_message(raw_content):
                            ts_q = entry.get("timestamp")
                            if isinstance(ts_q, str):
                                try:
                                    dt_q = datetime.fromisoformat(ts_q.replace("Z", "+00:00"))
                                    ts_q = dt_q.timestamp()
                                except (ValueError, AttributeError):
                                    ts_q = time.time()
                            elif not isinstance(ts_q, (int, float)):
                                ts_q = time.time()
                            messages.append({
                                "role": "user",
                                "text": _clean_user_text(raw_content),
                                "timestamp": int(ts_q),
                                "id": entry.get("uuid", f"q-{log_path.stem[:8]}-{len(messages)}")
                            })
                    continue

                msg = entry.get("message", {})
                role = msg.get("role", entry.get("type", ""))

                if role not in ("user", "assistant"):
                    continue

                content_blocks = msg.get("content", []) or []
                text_parts = []    # For normal text blocks
                char_parts = []    # For single-character string blocks
                is_char_stream = False
                for block in content_blocks:
                    if isinstance(block, str):
                        # Single char blocks: preserve spaces for word boundaries
                        if len(block) <= 2:  # single chars including '\n'
                            char_parts.append(block)
                            is_char_stream = True
                        else:
                            s = block.strip()
                            if s:
                                text_parts.append(s)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        t = (block.get("text") or "").strip()
                        if t:
                            text_parts.append(t)

                # Build combined text
                if is_char_stream and len(char_parts) > 10:
                    # Join character stream directly (preserves spaces/newlines)
                    combined = "".join(char_parts).strip()
                    # Also append any text blocks
                    if text_parts:
                        combined += "\n\n" + "\n\n".join(text_parts)
                elif text_parts:
                    combined = "\n\n".join(text_parts)
                else:
                    continue

                if not combined or len(combined) < 2:
                    continue

                # Collapse spaced-out text from older sessions
                combined = _despace(combined)

                # Filter based on role
                if role == "user":
                    if not _is_real_user_message(combined):
                        continue
                    combined = _clean_user_text(combined)
                elif role == "assistant":
                    if not _is_real_assistant_message(combined):
                        continue

                ts = entry.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts = ts / 1000  # ms to seconds
                elif isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except (ValueError, AttributeError):
                        ts = time.time()
                else:
                    ts = time.time()

                messages.append({
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                })
    except Exception:
        pass

    _jsonl_cache[str(log_path)] = (mtime, messages, stat.st_size, time.time())
    return messages


def _load_portal_messages():
    """Load messages sent via the portal chat, filtering out noise.
    Uses mtime+size cache to avoid re-reading 8k+ line file on every request (was 75ms/call)."""
    global _portal_chat_cache
    messages = []
    if not PORTAL_CHAT_LOG.exists():
        return messages
    try:
        stat = PORTAL_CHAT_LOG.stat()
        mtime = stat.st_mtime
        fsize = stat.st_size
        cached_mtime, cached_fsize, cached_msgs = _portal_chat_cache
        # Cache hit: file unchanged since last read
        if mtime == cached_mtime and fsize == cached_fsize and cached_msgs:
            return cached_msgs
        # Cache miss: re-read file
        # Use errors='replace' to handle surrogate chars that break UTF-8 serialization
        with PORTAL_CHAT_LOG.open("r", errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Filter noise from portal log (stray pipes, single chars, etc.)
                    msg_text = entry.get("text", "").strip()
                    if not msg_text:
                        continue
                    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
                        continue  # Skip stray pipe/bracket/noise artifacts
                    messages.append(entry)
                except json.JSONDecodeError:
                    continue
        # Update cache
        _portal_chat_cache = (mtime, fsize, messages)
    except Exception:
        pass
    return messages


def _save_portal_message(text, role="user"):
    """Save a message sent via the portal."""
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
    }
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log."""
    session_msgs = []
    portal_msgs = []

    # JSONL session logs -- authoritative source for message text (Fix 3)
    # Uses tail-read (last 500KB) + 10s cache — safe even for 138MB files.
    # The CPU killer was /api/context reading the FULL file, not this parser.
    for log_path in _get_all_session_log_paths(max_files=1):
        session_msgs.extend(_parse_jsonl_messages_from_file(log_path))

    # Portal-sent messages
    portal_msgs.extend(_load_portal_messages())

    # Tag by source so dedup can prefer session JSONL over portal-chat.jsonl (Fix 3)
    for m in session_msgs:
        m['_src'] = 'session'
    for m in portal_msgs:
        m['_src'] = 'portal'

    all_messages = session_msgs + portal_msgs

    # Sort by timestamp
    all_messages.sort(key=lambda m: m["timestamp"])

    # Deduplicate by ID -- session JSONL always wins (most complete, authoritative text)
    seen_idx: dict = {}
    for i, m in enumerate(all_messages):
        existing_idx = seen_idx.get(m["id"])
        if existing_idx is None or m['_src'] == 'session':
            seen_idx[m["id"]] = i
    deduped = [all_messages[i] for i in sorted(seen_idx.values())]

    return deduped[-last_n:] if len(deduped) > last_n else deduped


def _token_matches(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), BEARER_TOKEN.encode())


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return _token_matches(auth[7:])
    return _token_matches(request.query_params.get("token") or "")


_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def is_loopback_caller(request: Request) -> bool:
    """True only for a process on this machine talking to the portal directly.

    The server binds 0.0.0.0, so a remote caller must never pass. A reverse
    proxy on the same host would make every visitor look like 127.0.0.1, so a
    request that carries proxy forwarding headers is not treated as local.
    """
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if not ip.is_loopback:
        return False
    return not any(request.headers.get(h) for h in _PROXY_HEADERS)


# ---------------------------------------------------------------------------

# ── Favicon ──────────────────────────────────────────────────────────────

async def favicon(request: Request):
    """Serve the favicon (a local favicon.ico override, else the SPA's SVG mark)."""
    ico = SCRIPT_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return await favicon_svg(request)


async def favicon_svg(request: Request):
    """Serve the brand mark shipped with the SPA build."""
    svg = REACT_DIST / "favicon.svg"
    if svg.exists():
        return FileResponse(str(svg), media_type="image/svg+xml")
    return Response(status_code=204)

async def favicon_png(request: Request):
    """Serve 32px favicon PNG."""
    png = SCRIPT_DIR / "favicon-32.png"
    if png.exists():
        return FileResponse(str(png), media_type="image/png")
    return Response(status_code=204)

async def apple_touch_icon(request: Request):
    """Serve Apple touch icon."""
    icon = SCRIPT_DIR / "apple-touch-icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)

# Routes
# ---------------------------------------------------------------------------
async def api_trial(request: Request) -> JSONResponse:
    """GET /api/trial — shared yourAICIV trial contract (see trial_gate.py).

    Deliberately unauthenticated: the payment screen must render even when the
    browser has lost its token, and the response only carries the trial dates
    and the public payment link.
    """
    resp = JSONResponse(trial_state())
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "uptime": int(time.time() - START_TIME)})


async def index(request: Request) -> Response:
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response(
        "<h1>Portal build not found</h1><p>Run <code>npm ci && npm run build</code> in react-portal/.</p>",
        media_type="text/html", status_code=503,
    )



async def index_react(request: Request) -> Response:
    """Serve React portal at /react path."""
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response("<h1>React Portal not found — run npm run build in react-portal/</h1>",
                    media_type="text/html", status_code=503)


async def api_status(request: Request) -> JSONResponse:
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    session = get_tmux_session()
    tmux_alive = False
    r = await _run_subprocess_async(["tmux", "has-session", "-t", session])
    if r is not None and r.returncode == 0:
        tmux_alive = True

    claude_running = False
    out = await _run_subprocess_output(["pgrep", "-f", "claude"])
    if out and out.strip():
        claude_running = True

    tg_running = False
    out = await _run_subprocess_output(["pgrep", "-f", "telegram"])
    if out and out.strip():
        tg_running = True

    ctx_pct = None
    try:
        ctx_file = Path("/tmp/claude_context_used.txt")
        if ctx_file.exists():
            ctx_pct = float(ctx_file.read_text().strip())
    except Exception:
        pass

    return JSONResponse({
        "civ": CIV_NAME, "uptime": int(time.time() - START_TIME),
        "tmux_session": session, "tmux_alive": tmux_alive,
        "claude_running": claude_running, "tg_bot_running": tg_running,
        "ctx_pct": ctx_pct,
        "timestamp": int(time.time()),
        "version": PORTAL_VERSION,
    })


async def api_release_notes(request: Request) -> JSONResponse:
    """Return release notes and current version."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = json.loads(RELEASE_NOTES_FILE.read_text())
        data["current_version"] = PORTAL_VERSION
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"current_version": PORTAL_VERSION, "releases": [], "error": str(e)})


async def api_chat_history(request: Request) -> JSONResponse:
    """Return recent chat messages from JSONL session log."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    last_n = int(request.query_params.get("last", "100"))
    last_n = min(last_n, 500)

    messages = _parse_all_messages(last_n=last_n)

    # Note: mirroring moved to websocket loop only — doing it here caused 22s+ load times
    # by rewriting portal-chat.jsonl hundreds of times per history request.

    # Sanitize messages to remove surrogate characters that break UTF-8 encoding
    def _sanitize(obj):
        if isinstance(obj, str):
            return obj.encode('utf-8', errors='replace').decode('utf-8')
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    messages = _sanitize(messages)
    return JSONResponse({"messages": messages, "count": len(messages), "timestamp": int(time.time())})


async def api_chat_send(request: Request) -> JSONResponse:
    """Inject a message into the tmux session. Response comes via /api/chat/stream or history."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    _save_portal_message(message, role="user")

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        tagged = f"[portal-react] {message}"
    else:
        tagged = f"[portal] {message}"

    session = get_tmux_session()
    if _signin_holds_pane():
        return JSONResponse({"error": SIGNIN_HOLD_MESSAGE, "held_for_signin": True}, status_code=409)
    try:
        # Leading newline clears any partial input in buffer
        # All subprocess calls use async wrapper to avoid blocking the event loop
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", session, "-l", f"\n{tagged}"], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"], check=True)
        # 5x Enter retries (matches Telegram bridge pattern) — ensures Claude
        # processes the message even if busy with tool calls or generation
        async def _retry_enters():
            for _ in range(5):
                await asyncio.sleep(0.5)
                await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"])
        asyncio.ensure_future(_retry_enters())
        return JSONResponse({"status": "sent", "timestamp": int(time.time())})
    except Exception as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_notify(request: Request) -> JSONResponse:
    """Save a system notification to portal chat (role=assistant, no tmux injection)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    entry = _save_portal_message(message, role="assistant")

    # Push immediately to all connected WS clients — bypasses 0.8s poll delay
    if _chat_ws_clients and entry:
        import asyncio as _asyncio
        _asyncio.create_task(_push_message_to_clients(entry))

    return JSONResponse({"status": "saved", "id": entry["id"], "timestamp": entry["timestamp"]})


async def ws_chat(websocket: WebSocket) -> None:
    """Stream new chat messages via WebSocket. Polls JSONL log for new entries."""
    token = websocket.query_params.get("token", "")
    if not _token_matches(token or ""):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _chat_ws_clients.add(websocket)
    seen_texts: dict[str, int] = {}   # id -> len(text) of last sent version
    first_seen: dict[str, float] = {} # id -> time.time() when first noticed (Fix 2)
    stable_counts: dict[str, int] = {}# id -> consecutive polls with same length (Fix 1)
    # Fix 5 (truncation): track IDs where we already sent the final stable version.
    # Prevents re-sending indefinitely once the complete message is delivered.
    stable_sent: set = set()

    # Register initial batch of recent messages as "seen" to avoid re-sending old messages.
    # Only NEW messages (arriving after connect) will be pushed via the poll loop below.
    messages = _parse_all_messages(last_n=200)
    for msg in messages:
        seen_texts[msg["id"]] = len(msg.get("text", ""))
        stable_sent.add(msg["id"])  # existing messages already complete — skip final-send

    try:
        while True:
            messages = _parse_all_messages(last_n=200)
            for msg in messages:
                msg_id = msg["id"]
                msg_len = len(msg.get("text", ""))
                prev_len = seen_texts.get(msg_id, -1)

                # Fix 2: skip brand-new messages on their very first poll (wait ~0.8s)
                if msg_id not in first_seen:
                    first_seen[msg_id] = time.time()
                    continue  # skip first poll cycle for all new messages

                msg_age = time.time() - first_seen[msg_id]

                # Check if text is still growing
                if prev_len >= 0 and msg_len == prev_len:
                    # Fix 1: stable — increment counter
                    stable_counts[msg_id] = stable_counts.get(msg_id, 0) + 1
                else:
                    # Text changed (new or grown) — reset stability counter
                    stable_counts[msg_id] = 0

                # ── Send path ──────────────────────────────────────────────────────
                # Noise guard (shared by all send paths below)
                _ws_text = msg.get("text", "").strip()
                _is_noise = (not _ws_text or len(_ws_text) < 3 or
                             (len(_ws_text) <= 2 and not any(c.isalnum() for c in _ws_text)))

                if _is_noise:
                    continue

                is_stable = stable_counts.get(msg_id, 0) >= 2

                if prev_len < 0 or (msg_len > prev_len + 20 and msg_age > 0.8):
                    # NEW message or text grew significantly — send current version
                    seen_texts[msg_id] = msg_len
                    # Persist to portal log once stable
                    if is_stable and msg_id not in _portal_log_ids:
                        _mirror_to_portal_log(msg)
                    await websocket.send_text(json.dumps(msg))

                elif is_stable and msg_id not in stable_sent:
                    # Fix 5 (truncation root cause):
                    # Message stopped growing. We may have sent a partial version earlier
                    # (when the growth threshold was met but the message wasn't complete).
                    # Re-send the NOW-COMPLETE text so the client can update its bubble
                    # in-place via the knownMsgIds path. This is the definitive final send.
                    # Only fires ONCE per message (stable_sent prevents re-send every poll).
                    stable_sent.add(msg_id)
                    # Persist complete version to portal log
                    if msg_id not in _portal_log_ids:
                        _mirror_to_portal_log(msg)
                    else:
                        # Overwrite any partial version already persisted
                        _overwrite_portal_log_entry(msg_id, msg)
                    # Only re-send if we previously sent a partial version (prev_len >= 0)
                    # and the final text is longer. No-op for brand-new stable messages
                    # that were already sent complete on the first pass.
                    if prev_len >= 0 and msg_len != prev_len:
                        seen_texts[msg_id] = msg_len
                        await websocket.send_text(json.dumps(msg))

                elif is_stable and msg_id not in _portal_log_ids:
                    # Fix 1: message stopped growing — persist now even if below growth threshold
                    _mirror_to_portal_log(msg)

            await asyncio.sleep(1.5)  # Poll interval — increased from 0.8s to reduce CPU (still near-real-time)
            # Server-side keepalive ping every 20s to prevent Cloudflare/client 30s stale detection
            _now = time.time()
            if not hasattr(websocket, '_last_ping'):
                websocket._last_ping = _now
            if _now - websocket._last_ping >= 20:
                try:
                    await websocket.send_text(json.dumps({"type": "ping", "ts": int(_now)}))
                    websocket._last_ping = _now
                except Exception:
                    break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _chat_ws_clients.discard(websocket)


async def api_chat_upload(request: Request) -> JSONResponse:
    """Accept a file upload, save to UPLOADS_DIR + docs/from-telegram/, log to portal chat, inject tmux notification."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        form = await request.form()
        uploaded = form.get("file")
        if not uploaded or not hasattr(uploaded, "read"):
            return JSONResponse({"error": "no file"}, status_code=400)

        caption = str(form.get("caption", "")).strip()

        content = await uploaded.read()
        if len(content) > UPLOAD_MAX_BYTES:
            return JSONResponse({"error": "file too large (max 50 MB)"}, status_code=413)

        original_name = getattr(uploaded, "filename", None) or "upload"
        # Sanitize: keep alphanumerics, dots, dashes, underscores
        safe_name = "".join(c for c in original_name if c.isalnum() or c in "._-") or "upload"
        timestamp_ms = int(time.time() * 1000)
        stored_name = f"{timestamp_ms}_{secrets.token_hex(4)}_{safe_name}"
        dest = UPLOADS_DIR / stored_name
        dest.write_bytes(content)

        # Also save a named copy to portal_uploads/from-portal/ for easy reference
        from_portal_dir = UPLOADS_DIR / "from-portal"
        from_portal_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        portal_copy_name = f"portal_{timestamp_str}_{safe_name}"
        portal_copy_path = from_portal_dir / portal_copy_name
        portal_copy_path.write_bytes(content)

        # Detect if this is an image
        is_image = safe_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'))

        # Save ONE combined user message to portal chat log (image + caption together)
        # Include stored_name so frontend can render inline image via /api/chat/uploads/
        chat_text = f"[Image: {stored_name}]" if is_image else f"[File: {stored_name}]"
        if caption:
            chat_text += f"\n{caption}"
        user_entry = _save_portal_message(chat_text, role="user")

        # Inject notification into AI's tmux session (mirrors Telegram bridge pattern)
        # CRITICAL: Must be SINGLE LINE — multi-line paste triggers Claude Code's
        # "Pasted text" confirmation prompt and blocks automatic processing.
        notify_parts = [f"[Portal Upload from {HUMAN_NAME}] File saved to: {portal_copy_path}"]
        if caption:
            notify_parts.append(f"INSTRUCTIONS from {HUMAN_NAME}: {caption}")
        if is_image:
            notify_parts.append(f"[Image: {original_name} — USE Read tool on {portal_copy_path} TO VIEW]")
        notification = " ".join(notify_parts)

        session = get_tmux_session()
        tmux_ok = False
        try:
            if _signin_holds_pane():
                raise RuntimeError("a sign-in owns the pane; upload notice not typed")
            # Leading newline clears any partial input in buffer — async to avoid blocking event loop
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "-l", f"\n{notification}"],
                timeout=5, check=True,
            )
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "Enter"],
                timeout=5, check=True,
            )
            tmux_ok = True
            # 5x Enter retries — ensures Claude processes even if busy
            async def _retry_enters():
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"])
            asyncio.ensure_future(_retry_enters())
        except Exception:
            pass  # Don't fail the upload if tmux injection fails

        # Auto-acknowledge in portal chat so user sees confirmation immediately
        ack_parts = [f"Received your file: {original_name}"]
        if is_image:
            ack_parts.append("(image — viewing now)")
        if caption:
            ack_parts.append(f'Instructions noted: "{caption}"')
        if tmux_ok:
            ack_parts.append("Processing...")
        else:
            ack_parts.append("(tmux injection failed — will check docs/from-telegram/ manually)")
        ack_text = " ".join(ack_parts)
        ack_entry = _save_portal_message(ack_text, role="assistant")

        return JSONResponse({
            "ok": True,
            "filename": stored_name,
            "original": original_name,
            "path": str(dest),
            "copy_path": str(portal_copy_path),
            "size": len(content),
            "ack": ack_text,
            "user_msg_id": user_entry["id"],
            "ack_msg_id": ack_entry["id"],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_chat_serve_upload(request: Request) -> Response:
    """Serve an uploaded file. Token auth via query param or Bearer header."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filename = request.path_params.get("filename", "")
    # Prevent path traversal
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    filepath = UPLOADS_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(filepath))


async def api_download(request: Request) -> Response:
    """Serve a file download from whitelisted directories."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filepath_str = request.query_params.get("path", "")
    if not filepath_str:
        return JSONResponse({"error": "missing 'path' query parameter"}, status_code=400)
    try:
        filepath = Path(filepath_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    # Security: reject path traversal and check whitelist
    if ".." in filepath_str:
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    allowed = any(
        filepath == d or d in filepath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": f"path not in allowed directories"}, status_code=403)
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(filepath), filename=filepath.name)


async def api_download_list(request: Request) -> JSONResponse:
    """List files in an allowed directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dir_str = request.query_params.get("dir", "")
    if not dir_str:
        # Return list of allowed base directories
        return JSONResponse({
            "dirs": [str(d) for d in DOWNLOAD_ALLOWED_DIRS if d.exists()]
        })
    try:
        dirpath = Path(dir_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    allowed = any(
        dirpath == d or d in dirpath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": "directory not in allowed list"}, status_code=403)
    if not dirpath.exists() or not dirpath.is_dir():
        return JSONResponse({"error": "directory not found"}, status_code=404)
    items = []
    for item in sorted(dirpath.iterdir()):
        items.append({
            "name": item.name,
            "path": str(item),
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None,
        })
    return JSONResponse({"dir": str(dirpath), "items": items})


# ---------------------------------------------------------------------------
# Deliverables
# ---------------------------------------------------------------------------

async def api_deliverable(request: Request) -> JSONResponse:
    """Accept a file deliverable from the AI, copy to uploads, post download link to portal chat."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        src_path_str = body.get("path", "").strip()
        display_name = body.get("name", "").strip()
        caption = body.get("message", "").strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not src_path_str:
        return JSONResponse({"error": "missing 'path'"}, status_code=400)
    src_path = Path(src_path_str).resolve()
    if not src_path.exists() or not src_path.is_file():
        return JSONResponse({"error": f"file not found: {src_path_str}"}, status_code=404)

    if not display_name:
        display_name = src_path.name
    safe_name = "".join(c for c in display_name if c.isalnum() or c in "._-") or "deliverable"
    stored_name = f"{int(time.time() * 1000)}_{safe_name}"
    dest = UPLOADS_DIR / stored_name
    dest.write_bytes(src_path.read_bytes())

    serve_url = f"/api/chat/uploads/{stored_name}"
    # Use PORTAL_FILE tag format — rendered by portal HTML as styled download card
    lines = []
    if caption:
        lines.append(caption)
    lines.append(f"[PORTAL_FILE:{stored_name}:{display_name}]")
    entry = _save_portal_message("\n\n".join(lines), role="assistant")

    # Push immediately to all connected WS clients — bypasses 0.8s poll delay
    # so file download cards appear live without requiring a page refresh.
    if _chat_ws_clients and entry:
        import asyncio as _asyncio
        _asyncio.create_task(_push_message_to_clients(entry))

    return JSONResponse({"ok": True, "filename": stored_name, "url": serve_url})


_pane_cache: tuple = (0.0, "")  # (last_check_time, pane_id)
_PANE_CACHE_TTL = 10.0


def _find_primary_pane():
    """Find the tmux pane ID running the primary Claude Code instance.
    Result cached for 10s to avoid subprocess calls on every poll."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    try:
        # List all panes with their IDs
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        panes = [p.strip() for p in out.splitlines() if p.strip()]
        if not panes:
            _pane_cache = (now, session)
            return session
        _pane_cache = (now, panes[0])
        return panes[0]
    except Exception:
        _pane_cache = (now, session)
        return session


async def _find_primary_pane_async():
    """Async version of _find_primary_pane — use from async functions."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    out = await _run_subprocess_output(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"], timeout=3
    )
    panes = [p.strip() for p in out.splitlines() if p.strip()] if out else []
    if not panes:
        _pane_cache = (now, session)
        return session
    _pane_cache = (now, panes[0])
    return panes[0]


async def ws_terminal(websocket: WebSocket) -> None:
    """Stream tmux pane content via WebSocket. Read-only."""
    token = websocket.query_params.get("token", "")
    if not _token_matches(token or ""):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pane_target = await _find_primary_pane_async()
    last_content = ""

    try:
        while True:
            content = await _run_subprocess_output(
                ["tmux", "capture-pane", "-t", pane_target, "-p"], timeout=3
            )
            content = content.strip() if content else "[tmux session not found]"

            if content != last_content:
                await websocket.send_text(content)
                last_content = content

            await asyncio.sleep(1.0)  # Terminal poll — increased from 0.5s to reduce CPU
    except (WebSocketDisconnect, Exception):
        pass


def _launch_model_flag() -> str:
    """The same model the AiCIV's own launchers use: $CIV_ROOT/config/launch_model.txt (the M3 trial pins
    MiniMax-M3 there). No pin -> no --model flag, so the civ's .claude/settings.json decides. Never a
    hardcoded model name: a fixed name here broke the trial's M3-only check (Witness ticket 3350)."""
    root = Path(os.environ.get("CIV_ROOT") or Path.home())
    try:
        model = (root / "config" / "launch_model.txt").read_text().strip()
    except OSError:
        return ""
    if model and re.fullmatch(r"[A-Za-z0-9._\-\[\]]{1,80}", model):
        return f" --model {model}"
    return ""


async def api_context(request: Request) -> JSONResponse:
    """Return real context window usage from the latest Claude session JSONL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        # Model → max context mapping (input token capacity)
        MODEL_CONTEXT = {
            "claude-opus-4-6":          1_000_000,
            "claude-opus-4-6[1m]":      1_000_000,
            "claude-sonnet-4-6":        200_000,
            "claude-sonnet-4-6[1m]":    1_000_000,
            "claude-haiku-4-5-20251001": 200_000,
        }
        DEFAULT_MAX = 1_000_000  # default to 1M if model unknown

        logs = _find_all_project_jsonl()
        if not logs:
            return JSONResponse({"input_tokens": 0, "max_tokens": DEFAULT_MAX, "pct": 0, "model": None})

        latest = logs[0]
        input_tokens = 0
        cache_read = 0
        cache_creation = 0
        model = None

        # Read LAST usage entry only — tail the file instead of reading all 138MB
        # STABILITY FIX 2026-03-14: reading entire file on every poll was burning 64% CPU
        fsize = latest.stat().st_size
        tail_bytes = min(fsize, 200_000)  # last 200KB is plenty to find latest usage
        with open(latest, 'rb') as f:
            f.seek(max(0, fsize - tail_bytes))
            tail_data = f.read().decode('utf-8', errors='replace')
        for line in tail_data.splitlines():
            try:
                entry = json.loads(line)
                # Extract model from message entries
                msg_model = entry.get("model") or entry.get("message", {}).get("model")
                if msg_model:
                    model = msg_model
                usage = entry.get("usage") or entry.get("message", {}).get("usage")
                if usage and isinstance(usage, dict):
                    t = usage.get("input_tokens", 0)
                    if t:
                        input_tokens = t
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_creation = usage.get("cache_creation_input_tokens", 0)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

        max_tokens = MODEL_CONTEXT.get(model, DEFAULT_MAX) if model else DEFAULT_MAX
        total = input_tokens + cache_read + cache_creation
        pct = round(min(total / max_tokens * 100, 100), 1)
        return JSONResponse({
            "input_tokens": input_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total_tokens": total,
            "max_tokens": max_tokens,
            "pct": pct,
            "model": model,
            "session_id": latest.stem,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_resume(request: Request) -> JSONResponse:
    """Launch a new Claude instance resuming the most recent conversation session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        logs = _find_all_project_jsonl()
        if not logs:
            return JSONResponse({"error": "no sessions found"}, status_code=404)
        session_id = logs[0].stem  # UUID filename without .jsonl
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"
        project_dir = str(Path.home())
        # Kill any stale {civ}-primary-* sessions so prefix-matching stays unambiguous
        try:
            old = await _run_subprocess_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=3
            )
            if old:
                for s in old.splitlines():
                    if s.startswith(f"{CIV_NAME}-primary-"):
                        await _run_subprocess_async(["tmux", "kill-session", "-t", s])
        except Exception:
            pass
        # Write session name so portal can track it
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        claude_cmd = (
            f"claude{_launch_model_flag()} --dangerously-skip-permissions "
            f"--resume {session_id}"
        )
        # Popen is fire-and-forget so we use run_in_executor to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        return JSONResponse({"status": "resuming", "session_id": session_id, "tmux": tmux_session})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_panes(request: Request) -> JSONResponse:
    """Return all tmux panes with their current content."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    session = get_tmux_session()
    try:
        out = await _run_subprocess_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_title}\t#{session_name}:#{window_index}.#{pane_index}"],
            timeout=3
        )
        if not out:
            return JSONResponse({"panes": []})
        panes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            pane_id = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else pane_id
            target = parts[2] if len(parts) > 2 else pane_id
            session_name = session.split(":")[0] if ":" in session else session
            if session_name not in target and session not in target:
                continue
            capture = await _run_subprocess_output(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"], timeout=3
            )
            panes.append({"id": pane_id, "title": title or pane_id, "target": target, "content": (capture or "").strip()})
        return JSONResponse({"panes": panes})
    except Exception as e:
        return JSONResponse({"error": str(e), "panes": []})


async def api_inject_pane(request: Request) -> JSONResponse:
    """Inject a command into a specific tmux pane by pane_id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    pane_id = body.get("pane_id", "").strip()
    message = body.get("message", "").strip()
    if not pane_id or not message:
        return JSONResponse({"error": "pane_id and message required"}, status_code=400)
    if _signin_holds_pane() and pane_id in (await _find_primary_pane_async(), get_tmux_session()):
        return JSONResponse({"error": SIGNIN_HOLD_MESSAGE, "held_for_signin": True}, status_code=409)
    try:
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane_id, "-l", message], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
        return JSONResponse({"status": "sent"})
    except Exception as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# BOOP / Skills Endpoints (Settings panel)
# ---------------------------------------------------------------------------
SKILLS_DIR = Path.home() / ".claude" / "skills"
BOOP_CONFIG_FILE = SCRIPT_DIR / "boop_config.json"


async def api_compact_status(request: Request) -> JSONResponse:
    """Check if Claude is currently compacting context (shows in tmux pane)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pane = await _find_primary_pane_async()
    content = await _run_subprocess_output(
        ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-20"], timeout=3
    )
    if content:
        compacting = "Compacting (ctrl+o" in content or "Compacting…" in content
        return JSONResponse({"compacting": compacting})
    return JSONResponse({"compacting": False})


async def api_boop_config(request: Request) -> JSONResponse:
    """GET: read active BOOP config. POST: update active_command and/or cadence_minutes."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "POST":
        try:
            body = await request.json()
            cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
            g = cfg.setdefault("global", {})
            if "active_command" in body:
                g["active_command"] = str(body["active_command"])
            if "cadence_minutes" in body:
                g["cadence_minutes"] = int(body["cadence_minutes"])
            if "paused" in body:
                g["paused"] = bool(body["paused"])
            BOOP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
            return JSONResponse({"ok": True, "active_command": g.get("active_command"),
                                 "cadence_minutes": g.get("cadence_minutes")})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    # GET
    try:
        cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
        g = cfg.get("global", {})
        return JSONResponse({
            "active_command": g.get("active_command", "/sprint-mode"),
            "cadence_minutes": g.get("cadence_minutes", 30),
            "paused": g.get("paused", False),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boops_list(request: Request) -> JSONResponse:
    """List available BOOP/skill entries from the skills directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boops = []
    if SKILLS_DIR.exists():
        for entry in sorted(SKILLS_DIR.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    boops.append({"name": entry.name, "path": str(skill_file)})
    return JSONResponse({"boops": boops})


async def api_boop_read(request: Request) -> JSONResponse:
    """Read the content of a specific BOOP/skill."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.path_params.get("name", "")
    if ".." in name or "/" in name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = skill_file.read_text(encoding="utf-8", errors="replace")
    return JSONResponse({"name": name, "content": content})


# BOOP daemon control — session name and script path for toggle/status
BOOP_TMUX_SESSION = "boop-daemon"
BOOP_DAEMON_SCRIPT = Path.home() / "civ" / "tools" / "boop-daemon.sh"


async def api_boop_status(request: Request) -> JSONResponse:
    """Check if the BOOP daemon tmux session is running."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = await _run_subprocess_async(["tmux", "has-session", "-t", BOOP_TMUX_SESSION])
        running = r is not None and r.returncode == 0
        pid = None
        if running:
            try:
                out = await _run_subprocess_output(
                    ["tmux", "list-panes", "-t", BOOP_TMUX_SESSION, "-F", "#{pane_pid}"], timeout=3
                )
                if out and out.strip():
                    pid = int(out.strip().split()[0])
            except (ValueError, Exception):
                pass
        return JSONResponse({"active": running, "pid": pid})
    except Exception:
        return JSONResponse({"active": False, "pid": None})


async def api_boop_toggle(request: Request) -> JSONResponse:
    """Toggle the BOOP daemon on/off via tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = await _run_subprocess_async(["tmux", "has-session", "-t", BOOP_TMUX_SESSION])
        currently_running = r is not None and r.returncode == 0

        if currently_running:
            await _run_subprocess_async(["tmux", "kill-session", "-t", BOOP_TMUX_SESSION])
            return JSONResponse({"active": False, "action": "stopped"})
        else:
            if not BOOP_DAEMON_SCRIPT.exists():
                return JSONResponse(
                    {"error": f"boop-daemon.sh not found at {BOOP_DAEMON_SCRIPT}"},
                    status_code=500
                )
            await _run_subprocess_async(
                ["tmux", "new-session", "-d", "-s", BOOP_TMUX_SESSION,
                 f"bash {BOOP_DAEMON_SCRIPT} > /tmp/boop-daemon.log 2>&1"]
            )
            return JSONResponse({"active": True, "action": "started"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Claude OAuth Auth Endpoints — v2 (state machine, screen detection, retry)
# ---------------------------------------------------------------------------

AUTH_SCREEN_PATTERNS = {
    'oauth_url': OAUTH_URL_PATTERN,
    'login_menu': re.compile(
        r'Select login method|Use OAuth|How would you like to authenticate|'
        r'Claude account with subscription',
        re.IGNORECASE,
    ),
    'csat_survey': re.compile(
        r'How is Claude doing\?|rate your experience|satisfaction survey|'
        r'How would you rate|thumbs up|Would you recommend',
        re.IGNORECASE,
    ),
    # Only INTERACTIVE update prompts. The old pattern also matched the
    # passive "Auto-update failed" / "new version available" banners a running
    # Claude keeps on screen, which classified every screen as a blocker and
    # spammed Escape until timeout (ticket 3270 / dispatch-yash #3263).
    'update_prompt': re.compile(
        r'Update now\?|would you like to update',
        re.IGNORECASE,
    ),
    'trust_folder': re.compile(
        r'Do you trust the authors|trust this (?:project|folder)|'
        r'Trust this project|Do you want to trust',
        re.IGNORECASE,
    ),
    # Startup dialog for servers in the project's .mcp.json. Two variants
    # (verified on Claude Code 2.1.280): "New MCP server found in this
    # project: <name>" (single-select) and "<N> new MCP servers found in this
    # project" (checkbox multi-select).
    'mcp_server': re.compile(
        r'new MCP servers? found|MCP servers may execute|'
        r'Use this and all future MCP servers',
        re.IGNORECASE,
    ),
    'logged_in': re.compile(
        r'Logged in as|Login successful|Successfully authenticated|'
        r'You are now logged in',
        re.IGNORECASE,
    ),
    'shell_prompt': re.compile(
        r'(?:aiciv@|[$#])\s*$',
        re.MULTILINE,
    ),
    # Genuine launch failures only. The old bare `Error` also matched the
    # "API Error: 401" that EVERY signed-out Claude shows — i.e. the exact
    # screen auth-v2 exists to fix — and sent the flow into kill-and-retry.
    'error': re.compile(
        r'(?:ENOENT|command not found|fatal error|panic:|'
        r'Cannot connect|Connection refused)',
        re.IGNORECASE,
    ),
}
# login_menu ranks ABOVE the dismissable blockers: when the login picker is on
# screen it is the active screen, and a leftover banner must not mask it.
AUTH_SCREEN_PRIORITY = [
    'oauth_url', 'logged_in', 'login_menu', 'csat_survey', 'update_prompt',
    'trust_folder', 'mcp_server', 'error', 'shell_prompt',
]
# Max times one blocker type is dismissed per attempt before it is ignored.
AUTH_BLOCKER_MAX_DISMISSALS = 3


# Global auth state (simple, in-process)
_auth_prewarm_task = None  # background prewarm task handle


_SHELL_NAMES = {"bash", "sh", "dash", "zsh", "fish", "ash", "ksh", "tcsh", "csh", "login"}
_VERSION_TITLE_RE = re.compile(r'^\d+\.\d+\.\d+')


def _argv_is_claude(argv) -> bool:
    """True if a process argv looks like Claude Code (native or npm build)."""
    if not argv:
        return False
    arg0 = str(argv[0])
    base = os.path.basename(arg0).lower()
    if base in ("claude", "claude.exe"):
        return True
    # Native installs exec the versioned binary directly, e.g.
    # ~/.local/share/claude/versions/2.1.280 -> argv[0] basename "2.1.280".
    if _VERSION_TITLE_RE.match(base) and "claude" in [p.lower() for p in Path(arg0).parts[:-1]]:
        return True
    if base in ("node", "nodejs", "bun"):
        return any("claude-code" in str(a) or os.path.basename(str(a)).lower() in ("claude", "cli.js", "claude.exe")
                   for a in argv[1:3])
    return False


def _read_proc_table() -> dict:
    """Snapshot /proc: {pid: {"ppid", "pgrp", "tpgid", "argv"}}. Best effort."""
    table = {}
    try:
        entries = os.listdir("/proc")
    except Exception:
        return table
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/stat", "r") as f:
                stat = f.read()
            rest = stat.rsplit(")", 1)[1].split()
            # rest: state ppid pgrp session tty_nr tpgid ...
            ppid, pgrp, tpgid = int(rest[1]), int(rest[2]), int(rest[5])
            with open(f"/proc/{e}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            table[int(e)] = {"ppid": ppid, "pgrp": pgrp, "tpgid": tpgid, "argv": argv}
        except Exception:
            continue
    return table


def _foreground_claude_in_tree(root_pid: int, table: dict) -> bool:
    """Is a Claude process running in the FOREGROUND of the pane rooted at root_pid?

    Walks the pane's process tree. A Claude process counts only if it is in the
    terminal's foreground process group (pgrp == tpgid), so a backgrounded or
    detached claude under an idle shell is NOT mistaken for the live prompt.
    """
    children = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = table.get(pid)
        if info and _argv_is_claude(info["argv"]) and info["tpgid"] > 0 and info["pgrp"] == info["tpgid"]:
            return True
        stack.extend(children.get(pid, []))
    return False


def _is_interactive_shell_argv(argv) -> bool:
    """bash / -bash / sh … with options only: a shell sitting at its prompt.
    `bash script.sh`, `bash -c '…'`, `sh -lc …` are not."""
    if not argv:
        return False
    if os.path.basename(str(argv[0])).lstrip("-").lower() not in _SHELL_NAMES:
        return False
    for a in argv[1:]:
        a = str(a)
        if not a.startswith("-"):
            return False
        if not a.startswith("--") and "c" in a[1:]:
            return False
    return True


def _foreground_pids_in_tree(root_pid: int, table: dict) -> list:
    children = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    out, stack, seen = [], [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = table.get(pid)
        if info and info["tpgid"] > 0 and info["pgrp"] == info["tpgid"]:
            out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _classify_pane(current_command: str, pane_pid, table: dict) -> str:
    """Classify a pane as 'claude', 'shell' or 'unknown'.

    The old check trusted #{pane_current_command} alone. When Claude runs under
    any wrapper (restart loop, `cmd; cmd`, a launcher script) tmux reports the
    WRAPPER ("bash"), so the portal decided Claude was not running and TYPED a
    shell line (`clear && cd ~ && claude /login`) into the live Claude prompt
    (ticket 3270 / dispatch-yash #3263). The process tree is checked first.
    """
    cmd = (current_command or "").strip().lower()
    try:
        root = int(pane_pid)
    except (TypeError, ValueError):
        root = None
    if root and _foreground_claude_in_tree(root, table):
        return "claude"
    if root and root in table:
        # "shell" means an INTERACTIVE shell waiting at its prompt. A bash that
        # runs a script or `-c` (a restart loop between Claude relaunches) is
        # NOT a prompt: text typed there reaches the next Claude it starts
        # (review round 3, #1). Those panes are 'unknown' — nothing is typed.
        fg = _foreground_pids_in_tree(root, table)
        if fg:
            if all(_is_interactive_shell_argv(table[p]["argv"]) for p in fg):
                return "shell"
            return "unknown"
    if "claude" in cmd or _VERSION_TITLE_RE.match(cmd):
        return "claude"
    if cmd in _SHELL_NAMES:
        return "shell"
    if cmd in ("node", "nodejs", "bun"):
        return "claude"  # legacy npm build shows as node
    return "unknown"


async def _pane_process_state(pane: str) -> str:
    """Return 'claude' | 'shell' | 'unknown' for the given tmux pane."""
    try:
        # NOTE: must use _run_subprocess_output. _run_subprocess_async sends
        # stdout to DEVNULL, so the old check (r.stdout) could NEVER see the
        # pane command and always answered "not running" — the root cause of
        # auth-v2 typing `clear && cd ~ && claude /login` into a live prompt.
        out = await _run_subprocess_output(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}\t#{pane_pid}"],
            timeout=3
        )
        if not out or not out.strip():
            return "unknown"
        parts = out.strip().split("\t")
        cmd = parts[0] if parts else ""
        pid = parts[1] if len(parts) > 1 else None
        table = await asyncio.get_event_loop().run_in_executor(None, _read_proc_table)
        return _classify_pane(cmd, pid, table)
    except Exception:
        return "unknown"


async def _is_claude_running_async(pane: str) -> bool:
    """Check if Claude Code is the active (foreground) process in the given tmux pane."""
    return (await _pane_process_state(pane)) == "claude"


# Screens that must be NEW since /login was sent. In a LIVE Claude session
# (Reconnect Claude) the previous sign-in's URL, login picker and "Login
# successful" can still be on the visible screen (clear-history only clears the
# scrollback), and the flow would hand out a stale URL (its state= no longer
# matches) or report "already logged in". Found by the ticket-3383 sandbox
# test: a second reconnect in the same session returned the first URL.
# Review round 2 (ticket 3383): EVERY screen the flow acts on is fresh-checked,
# not only the sign-in ones. The blocker patterns ("thumbs up", "Would you
# recommend", "Do you want to trust", "command not found", ...) are ordinary
# words that can sit in the CIV's own conversation; matched against old text
# they made the portal press Escape / type "y" into the AI's live prompt.
_AUTH_FRESH_ONLY = ('logged_in', 'login_menu', 'csat_survey', 'update_prompt',
                    'trust_folder', 'mcp_server', 'error')
_stale_oauth_urls: set = set()

# Claude Code shows "esc to interrupt" in its status line while a turn runs.
# Nothing is typed (no /login, no Escape) into a session that is mid-turn.
_SESSION_BUSY_RE = re.compile(r'esc to interrupt', re.IGNORECASE)
AUTH_BUSY_MESSAGE = ("Your AI is in the middle of a task right now. "
                     "Try again in a minute, when it has finished.")
AUTH_INPUT_BUSY_MESSAGE = ("There is unsent text in your AI's message box. "
                           "Send or clear it, then try again.")
AUTH_STUCK_MESSAGE = ("Claude has another screen open that the sign-in could not close. "
                      "Wait a minute and try again.")


def _session_is_busy(visible_text: str) -> bool:
    return bool(visible_text) and bool(_SESSION_BUSY_RE.search(visible_text))


# --- Where is Claude Code's INPUT prompt? --------------------------------------
# Real Claude Code 2.1.x draws the input box as a horizontal rule followed by a
# line starting "❯ " (read first-order from a live 2.1.280 pane, ticket 3383
# round 3); an empty box shows a dim 'Try "…"' placeholder. Pickers use the
# SAME "❯" as their cursor ("❯ 1. Claude account…"), so "❯" only counts as
# the input prompt directly under a rule and never as a numbered option.
# Older / boxed builds draw "> " or "│ > "; those still count.
_HRULE_RE = re.compile(r'^\s*[─━]{8,}\s*$')
_LEGACY_PROMPT_RE = re.compile(r'^\s*[│|]?\s*>(?:\s|$)')
_CHEVRON_PROMPT_RE = re.compile(r'^\s*[│|]?\s*❯(?:\s|$)')
_SELECTOR_OPTION_RE = re.compile(r'^\s*[│|]?\s*[❯›>]\s*(?:\d+[.)]|\[)')
_PROMPT_TEXT_RE = re.compile(r'^\s*[│|]?\s*(?:>|❯)\s?(.*?)\s*[│|]?\s*$')
_PLACEHOLDER_RE = re.compile(r'^Try "')


def _screen_lines(text: str, n: int = 40) -> list:
    return [ln for ln in (text or "").splitlines() if ln.strip()][-n:]


def _is_input_prompt(lines: list, i: int) -> bool:
    ln = lines[i]
    if _SELECTOR_OPTION_RE.match(ln):
        return False  # "❯ 1. …" / "> 1. …" is a picker option, never the input box
    if _LEGACY_PROMPT_RE.match(ln):
        return True
    if _CHEVRON_PROMPT_RE.match(ln) and not _SELECTOR_OPTION_RE.match(ln):
        return i > 0 and bool(_HRULE_RE.match(lines[i - 1]))
    return False


def _last_input_prompt(lines: list) -> int:
    return max((i for i in range(len(lines)) if _is_input_prompt(lines, i)), default=-1)


def _idle_input_prompt_problem(visible_text: str):
    """Pure: None when the AI's input box is the ACTIVE screen and EMPTY, so
    typing "/login" + Enter is safe. Otherwise a short reason: 'no_prompt'
    (some other screen), 'dialog_open' (a selector/question drawn below the
    prompt: Enter would pick its default), 'input_not_empty' (the owner's
    unsent text would be sent together with "/login")."""
    lines = _screen_lines(visible_text)
    idx = _last_input_prompt(lines)
    if idx < 0:
        return "no_prompt"
    for ln in lines[idx + 1:]:
        if _SELECTOR_OPTION_RE.match(ln) or _CHEVRON_PROMPT_RE.match(ln) or re.search(r'Do you want to', ln):
            return "dialog_open"
    m = _PROMPT_TEXT_RE.match(lines[idx])
    text = (m.group(1) if m else "").strip()
    if text and not _PLACEHOLDER_RE.match(text):
        return "input_not_empty"
    return None


def _blocker_is_active(visible_text: str, pattern) -> bool:
    """Pure: is a blocking dialog matching `pattern` the ACTIVE screen? Claude
    Code draws its dialogs in place of the input box, so the match must come
    AFTER the last input prompt. The same words higher up are the AI's own
    conversation and never earn a key press (review round 2, #1)."""
    lines = _screen_lines(visible_text)
    if not lines:
        return False
    last_prompt = _last_input_prompt(lines)
    last_match = max((i for i, ln in enumerate(lines) if pattern.search(ln)), default=-1)
    return last_match > last_prompt


def _active_blocker(visible_text: str):
    """Which blocking dialog (if any) is ACTIVE on the visible screen now."""
    if _plan_mcp_dialog(visible_text) is not None and _blocker_is_active(visible_text, _MCP_DIALOG_HEADER_RE):
        return 'mcp_server'
    for name in ('trust_folder', 'update_prompt', 'csat_survey'):
        if _blocker_is_active(visible_text, AUTH_SCREEN_PATTERNS[name]):
            return name
    return None


# --- One sign-in flow at a time, and it can be cancelled -------------------
# The state machine runs inside POST /api/auth/start for up to a few minutes.
# Closing the Connect/Reconnect dialog must STOP it (it used to keep retrying
# and send Escape + /login into the AI's pane after the owner cancelled), and a
# second start must never run a second state machine against the same pane.
class _AuthCancelled(Exception):
    """The owner closed the sign-in dialog: stop, press nothing more."""


class _AuthFlow:
    def __init__(self, pane: str = None, attempt: str = None):
        self.pane = pane
        self.attempt = attempt
        self.cancel = asyncio.Event()
        self.finished = asyncio.Event()
        self.result = None
        self.task = None


_last_auth_flow = None  # the most recent Start flow (its outcome, for /api/auth/url)

# While a sign-in owns the AI's pane (a flow is running, or the login picker /
# code prompt is open waiting for the owner), the portal's OWN injectors (chat
# send, upload notices, scheduled tasks, AgentCal) hold their text instead of
# typing it into the sign-in screen (review round 3, #9).
AUTH_SCREEN_HOLD_S = 600.0
_auth_screen_open_until = 0.0


def _signin_holds_pane() -> bool:
    return _auth_flow_running() or time.time() < _auth_screen_open_until


def _release_signin_hold() -> None:
    global _auth_screen_open_until
    _auth_screen_open_until = 0.0


SIGNIN_HOLD_MESSAGE = ("Claude is signing in right now. Your message was not sent; "
                       "send it again when the sign-in is finished.")


async def _auth_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _attempt_id(body: dict):
    a = body.get("attempt")
    return a[:64] if isinstance(a, str) and a else None


_auth_flow = None  # the ONE running sign-in state machine, or None
# Attempt ids the owner already closed. The browser sends the same id with
# Start and Close, so a Close that reaches the server BEFORE its Start still
# cancels that Start (review round 2, #3).
_closed_attempts: list = []


def _auth_flow_running() -> bool:
    return _auth_flow is not None and not _auth_flow.finished.is_set()


def _auth_check(flow) -> None:
    if flow is not None and flow.cancel.is_set():
        raise _AuthCancelled()


async def _auth_sleep(flow, seconds: float) -> None:
    """Sleep, but wake up and stop at once if the flow is cancelled."""
    if flow is None:
        await asyncio.sleep(seconds)
        return
    _auth_check(flow)
    try:
        await asyncio.wait_for(flow.cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise _AuthCancelled()


async def _auth_key(pane: str, key: str, flow=None, literal: bool = False, check: bool = False):
    """The ONLY way the sign-in flow presses keys: refuses once cancelled."""
    _auth_check(flow)
    cmd = ["tmux", "send-keys", "-t", pane] + (["-l", key] if literal else [key])
    return await _run_subprocess_async(cmd, check=check)


async def _auth_line(pane: str, text: str, flow=None):
    """Type a line and press Enter as ONE unit: the cancel check happens once,
    before the text, so a Close can never leave "/login" (or the launch line)
    sitting unsent in the AI's input box (review round 3, #10)."""
    _auth_check(flow)
    await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
    await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _oauth_urls(content: str) -> list:
    """All complete (state= present) OAuth URLs in the text, in order."""
    return [m.group(0).strip() for m in OAUTH_URL_PATTERN.finditer(content or "")
            if 'state=' in m.group(0)]


def _auth_screen_baseline(content: str) -> dict:
    """What was already on screen BEFORE /login was sent."""
    content = content or ""
    return {"urls": set(_oauth_urls(content)),
            "counts": {n: len(AUTH_SCREEN_PATTERNS[n].findall(content)) for n in _AUTH_FRESH_ONLY}}


def _fresh_oauth_url(content: str, stale) -> str:
    """The newest complete OAuth URL that was not on screen before, or None."""
    fresh = [u for u in _oauth_urls(content) if u not in (stale or ())]
    return fresh[-1] if fresh else None


def _classify_auth_screen(content: str, baseline: dict = None) -> str:
    """Pure: screen type for the captured text. With a baseline, oauth_url and
    every screen in _AUTH_FRESH_ONLY count only when they are NEW since the
    baseline."""
    if not content:
        return 'empty'
    for name in AUTH_SCREEN_PRIORITY:
        if name == 'oauth_url':
            stale = baseline["urls"] if baseline else ()
            if _fresh_oauth_url(content, stale):
                return name
            continue
        pattern = AUTH_SCREEN_PATTERNS[name]
        if baseline is not None and name in _AUTH_FRESH_ONLY:
            n = len(pattern.findall(content))
            if n < baseline["counts"].get(name, 0):
                baseline["counts"][name] = n  # old text scrolled/cleared away: rebase
            if n > baseline["counts"].get(name, 0):
                return name
            continue
        if pattern.search(content):
            return name
    return 'unknown'


async def _capture_auth_screen(pane: str) -> str:
    return await _run_subprocess_output(
        ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-300"], timeout=5
    ) or ""


async def _detect_auth_screen(pane: str, baseline: dict = None) -> tuple:
    """Capture tmux pane and detect what's currently displayed.
    Returns (screen_type, raw_content) where screen_type is one of the
    AUTH_SCREEN_PATTERNS keys or 'unknown'/'empty'.
    """
    content = await _capture_auth_screen(pane)
    return _classify_auth_screen(content, baseline), content


async def _dismiss_auth_blocker(pane: str, screen_type: str, flow=None) -> bool:
    """Dismiss a blocking dialog. Returns True if action was taken.

    Keys are pressed only when the blocker is on the VISIBLE screen right now
    (scrollback left by tmux scroll-on-clear or old conversation text never
    earns a key press)."""
    if screen_type == 'mcp_server':
        outcome = await _dismiss_mcp_dialog(pane, flow)
        return outcome != "not_visible"
    pattern = AUTH_SCREEN_PATTERNS.get(screen_type)
    if pattern is None:
        return False
    visible = await _capture_visible(pane)
    if _session_is_busy(visible) or not _blocker_is_active(visible, pattern):
        return False
    if screen_type == 'csat_survey':
        await _auth_key(pane, "Escape", flow)
        await _auth_sleep(flow, 0.5)
        await _auth_key(pane, "Escape", flow)
        return True
    elif screen_type == 'update_prompt':
        await _auth_key(pane, "Escape", flow)
        await _auth_sleep(flow, 0.5)
        return True
    elif screen_type == 'trust_folder':
        await _auth_key(pane, "y", flow, literal=True)
        await _auth_sleep(flow, 0.2)
        await _auth_key(pane, "Enter", flow)
        return True
    return False


# ---------------------------------------------------------------------------
# "New MCP server found" dialog (ticket 3270, found by the midwife test birth)
# ---------------------------------------------------------------------------
# `claude /login` on a newborn stops at this dialog before the login menu. The
# old flow detected it but never dismissed it, so "Authenticate" timed out 4/4
# on every clean Korus newborn. Plain Enter is NOT right here: the highlighted
# default is "Continue without using this MCP server", which silently disables
# the CIV's own playwright MCP, and in the multi-server variant Enter toggles a
# checkbox instead of submitting. We navigate to the least-privilege option
# that keeps the CIV's configured servers working:
#   single variant -> "Use this MCP server"   (NOT "... and all future ...")
#   multi variant  -> "Enable selected"        (all servers pre-checked)
# and confirm the cursor landed there before pressing Enter. If navigation
# cannot be confirmed, sign-in still wins: accept the default (single) or
# Esc (multi), both of which close the dialog.
_MCP_DIALOG_HEADER_RE = re.compile(r'MCP servers? found in this project', re.IGNORECASE)
_MCP_CURSOR_RE = re.compile(r'^\s*(?P<cursor>[❯›>])?\s*(?P<body>.*?)\s*$')
_MCP_SINGLE_OPTIONS = (
    ("use_this", re.compile(r'^(?:\d+\.\s*)?Use this MCP server$', re.IGNORECASE)),
    ("use_all_future", re.compile(r'^(?:\d+\.\s*)?Use this and all future MCP servers', re.IGNORECASE)),
    ("continue_without", re.compile(r'^(?:\d+\.\s*)?Continue without using this MCP server', re.IGNORECASE)),
)
_MCP_MULTI_ITEM_RE = re.compile(r'^\[[^\]]\]\s+\S')
_MCP_MULTI_SUBMIT_RE = re.compile(r'^Enable selected', re.IGNORECASE)


def _plan_mcp_dialog(screen_text: str):
    """Pure planner for the MCP dialog on the VISIBLE screen.

    Returns None when no navigable dialog is visible, else a dict:
      {variant: 'single'|'multi', target: str, moves: [key...], on_target: bool}
    where `moves` are the arrow keys that bring the cursor to the target.
    """
    if not screen_text:
        return None
    lines = screen_text.splitlines()
    hdr = None
    for i, ln in enumerate(lines):
        if _MCP_DIALOG_HEADER_RE.search(ln):
            hdr = i  # last header wins (older frames may sit above)
    if hdr is None:
        return None
    options = []  # (label, has_cursor)
    for ln in lines[hdr + 1:]:
        m = _MCP_CURSOR_RE.match(ln)
        body = m.group("body") if m else ln.strip()
        has_cursor = bool(m and m.group("cursor"))
        label = None
        for name, rx in _MCP_SINGLE_OPTIONS:
            if rx.search(body):
                label = name
                break
        if label is None and _MCP_MULTI_ITEM_RE.search(body):
            label = "multi_item"
        if label is None and _MCP_MULTI_SUBMIT_RE.search(body):
            label = "enable_selected"
        if label:
            options.append((label, has_cursor))
    labels = [l for l, _ in options]
    if "use_this" in labels:
        variant, target = "single", "use_this"
    elif "enable_selected" in labels:
        variant, target = "multi", "enable_selected"
    else:
        return None
    cursor_idx = next((i for i, (_, c) in enumerate(options) if c), None)
    if cursor_idx is None:
        return None
    target_idx = labels.index(target)
    delta = target_idx - cursor_idx
    moves = ["Down"] * delta if delta > 0 else ["Up"] * (-delta)
    return {"variant": variant, "target": target, "moves": moves, "on_target": delta == 0}


async def _capture_visible(pane: str) -> str:
    return await _run_subprocess_output(["tmux", "capture-pane", "-t", pane, "-p", "-J"], timeout=5) or ""


async def _dismiss_mcp_dialog(pane: str, flow=None) -> str:
    """Close the MCP startup dialog so /login can proceed. Returns an outcome:
    'enabled_this' | 'enabled_selected' | 'fallback_default' | 'fallback_escape'
    | 'not_visible' (nothing pressed: the dialog is not on the visible screen)."""
    first = await _capture_visible(pane)
    plan = _plan_mcp_dialog(first)
    if plan is None or not _blocker_is_active(first, _MCP_DIALOG_HEADER_RE):
        return "not_visible"
    for _ in range(2):
        if plan["on_target"]:
            await _auth_key(pane, "Enter", flow)
            await _auth_sleep(flow, 1.0)
            return "enabled_this" if plan["variant"] == "single" else "enabled_selected"
        for key in plan["moves"]:
            await _auth_key(pane, key, flow)
            await _auth_sleep(flow, 0.2)
        await _auth_sleep(flow, 0.4)
        new_plan = _plan_mcp_dialog(await _capture_visible(pane))
        if new_plan is None:
            return "not_visible"
        plan = new_plan
    # Could not confirm the cursor position: never leave the user stuck.
    if plan["variant"] == "single":
        await _auth_key(pane, "Enter", flow)
        await _auth_sleep(flow, 1.0)
        return "fallback_default"
    await _auth_key(pane, "Escape", flow)
    await _auth_sleep(flow, 1.0)
    return "fallback_escape"


def _claude_pids_in_tree(root_pid, table: dict) -> set:
    """PIDs of every Claude process in the process tree rooted at root_pid."""
    try:
        root = int(root_pid)
    except (TypeError, ValueError):
        return set()
    children = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    found, stack, seen = set(), [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = table.get(pid)
        if info and _argv_is_claude(info["argv"]):
            found.add(pid)
        stack.extend(children.get(pid, []))
    return found


def _all_claude_pids(table: dict) -> set:
    return {pid for pid, info in table.items() if _argv_is_claude(info["argv"])}


async def _pane_root_pid(pane: str):
    out = await _run_subprocess_output(["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"], timeout=3)
    try:
        return int((out or "").strip())
    except ValueError:
        return None


async def _signal_pids(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(int(pid), sig)
        except (ProcessLookupError, PermissionError, ValueError):
            pass


async def _kill_claude_process(pane: str, only=None, exclude=None, require_arg=None) -> list:
    """Stop the Claude process(es) running IN THIS PANE, by PID. Never pkill.

    Only Claude processes found in the pane's own process tree are touched
    (optionally narrowed to the PIDs in `only`, e.g. the /login instance this
    portal launched). A Claude anywhere else in the container (the CIV's live
    session in another pane, a background job) is never signalled. If no such
    process can be identified, nothing is killed and [] is returned: the
    caller must treat that as "refuse", not as "done".
    """
    root = await _pane_root_pid(pane)
    if root is None:
        return []
    table = await asyncio.get_event_loop().run_in_executor(None, _read_proc_table)
    pids = _claude_pids_in_tree(root, table)
    if only is not None:
        pids &= set(only)
    if exclude:
        pids -= set(exclude)
    if require_arg:
        # e.g. "/login": only the sign-in instance, never a live session that
        # a wrapper relaunched (review round 3, #1)
        pids = {p for p in pids if require_arg in (table.get(p) or {}).get("argv", [])[1:]}
    if not pids:
        return []
    await _signal_pids(pids, signal.SIGTERM)
    for _ in range(12):
        await asyncio.sleep(0.25)
        alive = {p for p in pids if Path(f"/proc/{p}").exists()}
        if not alive:
            return sorted(pids)
    await _signal_pids(alive, signal.SIGKILL)
    return sorted(pids)


AUTH_MAX_RETRIES = 3
AUTH_CLAUDE_START_TIMEOUT_S = 45.0
AUTH_URL_WAIT_TIMEOUT_S = 30.0


async def _run_auth_state_machine(pane: str, flow: "_AuthFlow" = None) -> dict:
    """Run the auth flow state machine. Returns dict with status info.

    States: start -> waiting_for_screen -> (dismiss blockers | select_login) ->
            waiting_for_url -> success/failed

    This is the core v2 auth logic ported from auth-flow-v2.py, adapted for
    async execution inside the portal server.

    Safety rules (ticket 3383 review round 2):
      * every key goes through _auth_key and every wait through _auth_sleep,
        so cancelling `flow` (the owner closed the dialog) stops it at once;
      * nothing is typed into a live session that is mid-turn;
      * Escape is pressed only when a sign-in screen is ACTIVE on the visible
        screen, never blindly;
      * the only Claude ever killed is the `claude /login` THIS flow launched
        into a shell pane, by PID; if it cannot be identified, nothing is
        killed and the flow stops.
    """
    global _captured_oauth_url
    max_retries = AUTH_MAX_RETRIES
    retry_count = 0
    claude_start_timeout = AUTH_CLAUDE_START_TIMEOUT_S
    url_wait_timeout = AUTH_URL_WAIT_TIMEOUT_S
    poll_interval = 0.5
    log_entries = []

    def log(msg):
        log_entries.append(msg)
        _save_portal_message(f"[auth-v2] {msg}", role="assistant")

    def stop(**kw):
        kw.setdefault("started", False)
        kw["log"] = log_entries
        return kw

    async def keys(key, literal=False, check=False):
        return await _auth_key(pane, key, flow, literal=literal, check=check)

    async def nap(seconds):
        await _auth_sleep(flow, seconds)

    closed_leftovers = 0
    pre_dismissed = {}

    # Clear tmux scrollback so stale text from prior runs cannot poison _detect_auth_screen
    _auth_check(flow)
    await _run_subprocess_async(["tmux", "clear-history", "-t", pane])

    while retry_count <= max_retries:
        _auth_check(flow)
        # --- Phase 1: Start Claude /login ---
        log(f"Starting auth flow (attempt {retry_count + 1}/{max_retries + 1})")

        # Resize tmux so URLs don't wrap
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await nap(0.3)

        pane_state = await _pane_process_state(pane)
        # launched_pre_pids stays None unless THIS attempt launches `claude
        # /login` into a shell; only then may a retry stop a Claude (ours).
        launched_pre_pids = None
        if pane_state == "claude":
            visible = await _capture_visible(pane)
            if _session_is_busy(visible):
                log("Your AI is in the middle of a turn — nothing typed")
                return stop(busy=True, error=AUTH_BUSY_MESSAGE)
            leftover = _plan_auth_close(visible)
            if leftover is not None:
                # A sign-in screen from an earlier, abandoned attempt is still
                # open (code prompt -> picker -> prompt can take a few). Close
                # it ONE key at a time and look again; never type /login into it.
                if closed_leftovers >= 3:
                    log("A sign-in screen is still open after closing it 3 times — nothing typed")
                    return stop(error=AUTH_STUCK_MESSAGE)
                closed_leftovers += 1
                log(f"Closing a sign-in screen left from an earlier attempt ({leftover})")
                await keys(leftover)
                await nap(1.0)
                continue
            # A startup dialog (MCP servers / trust / update) already open in
            # the live session: dismiss it properly first. Typing "/login" +
            # Enter into it would pick its default (e.g. silently disable the
            # CIV's MCP server) (review round 2, #2).
            pre = _active_blocker(visible)
            if pre is not None:
                n = pre_dismissed.get(pre, 0)
                if n >= AUTH_BLOCKER_MAX_DISMISSALS:
                    log(f"Dialog {pre} is still open after {n} tries — nothing typed")
                    return stop(error=AUTH_STUCK_MESSAGE)
                pre_dismissed[pre] = n + 1
                acted = await _dismiss_auth_blocker(pane, pre, flow)
                log(f"Open dialog before /login: {pre} ({'dismissed' if acted else 'nothing pressed'})")
                await nap(1.0)
                continue
            problem = _idle_input_prompt_problem(visible)
            if problem is not None:
                # Another screen (a tool-permission question, a selector, the
                # rewind menu) or the owner's unsent text: "/login" + Enter
                # would answer it or send it. Type nothing (review round 3, #4).
                log(f"The AI's input box is not free ({problem}) — nothing typed")
                return stop(error=AUTH_STUCK_MESSAGE if problem != "input_not_empty" else AUTH_INPUT_BUSY_MESSAGE)
        # Snapshot what is already on screen so stale sign-in text is ignored.
        baseline = _auth_screen_baseline(await _capture_auth_screen(pane))
        _stale_oauth_urls.clear()
        _stale_oauth_urls.update(baseline["urls"])
        if pane_state == "shell":
            table = await asyncio.get_event_loop().run_in_executor(None, _read_proc_table)
            launched_pre_pids = _all_claude_pids(table)
            log("Claude not running (pane is at a shell) — launching 'claude /login'")
            launch_cmd = f"clear && cd {shlex.quote(str(Path.home()))} && claude /login"
            await _auth_line(pane, launch_cmd, flow)
        elif pane_state == "claude":
            log("Claude already running — sending /login")
            await _auth_line(pane, "/login", flow)
        else:
            # Fail closed: never type a shell line (or anything) into a pane we
            # cannot identify — that is how a live Claude prompt got polluted.
            log("Pane is neither a shell nor a running Claude — refusing to type into it")
            return stop(error="primary pane state unknown; nothing typed")

        # --- Phase 2: Wait for screen and handle blockers ---
        phase_start = time.time()
        login_selected = False
        blocker_counts = {}

        while True:
            await nap(poll_interval)
            elapsed = time.time() - phase_start

            screen_type, screen_content = await _detect_auth_screen(pane, baseline)
            if screen_type not in ('oauth_url', 'logged_in', 'unknown', 'empty', 'shell_prompt'):
                # About to press a key: never into a session that is mid-turn.
                if _session_is_busy(await _capture_visible(pane)):
                    log("Your AI is in the middle of a turn — stopping, nothing more typed")
                    return stop(busy=True, error=AUTH_BUSY_MESSAGE)

            if screen_type == 'oauth_url':
                # Goal state — extract the NEW URL (never one left from before)
                url = _fresh_oauth_url(screen_content, baseline["urls"])
                if url:
                    _auth_check(flow)  # a cancelled flow never publishes a URL
                    _captured_oauth_url = url
                    global _auth_screen_open_until
                    _auth_screen_open_until = time.time() + AUTH_SCREEN_HOLD_S
                    log(f"OAuth URL captured ({len(url)} chars) in {elapsed:.1f}s")
                    return {"started": True, "url": url, "log": log_entries}

            elif screen_type == 'logged_in':
                log("Already logged in — no OAuth URL needed")
                return {"started": True, "already_authenticated": True, "log": log_entries}

            elif screen_type in ('csat_survey', 'update_prompt', 'trust_folder', 'mcp_server'):
                n = blocker_counts.get(screen_type, 0)
                if n < AUTH_BLOCKER_MAX_DISMISSALS:
                    blocker_counts[screen_type] = n + 1
                    if screen_type == 'mcp_server':
                        outcome = await _dismiss_mcp_dialog(pane, flow)
                        log(f"MCP server dialog: {outcome}")
                    else:
                        acted = await _dismiss_auth_blocker(pane, screen_type, flow)
                        log(f"Blocker {screen_type}: {'dismissed' if acted else 'not on the visible screen, nothing pressed'}"
                            f" ({n + 1}/{AUTH_BLOCKER_MAX_DISMISSALS})")
                    await nap(1.0)
                    continue
                # Cap reached: stop pressing keys at it and fall through to the timeout check.

            elif screen_type == 'login_menu' and not login_selected:
                log("Login menu detected — selecting first option (Enter)")
                await keys("Enter")
                login_selected = True
                phase_start = time.time()  # Reset timeout for URL wait phase
                continue

            elif screen_type == 'error':
                log("Error detected on screen — will retry")
                break  # Break to retry loop

            # Check timeouts
            timeout = url_wait_timeout if login_selected else claude_start_timeout
            if elapsed > timeout:
                phase_name = "URL wait" if login_selected else "Claude start"
                log(f"Timeout in {phase_name} ({timeout}s)")
                break  # Break to retry loop

        # --- Phase 3: Retry ---
        retry_count += 1
        if retry_count <= max_retries:
            if launched_pre_pids is None:
                # The CIV's LIVE session: never killed (that dropped Verun's
                # working session, #3244). Close an ACTIVE sign-in screen with
                # one Escape; press nothing on any other screen.
                visible = await _capture_visible(pane)
                if _session_is_busy(visible):
                    log("Your AI started a turn — stopping, nothing more typed")
                    return stop(busy=True, error=AUTH_BUSY_MESSAGE)
                if _plan_auth_close(visible) == "Escape":
                    log(f"Retrying /login in the live session (retry {retry_count}/{max_retries})")
                    await keys("Escape")
                    await nap(1.0)
                else:
                    log(f"Retrying /login in the live session (retry {retry_count}/{max_retries}); no sign-in screen open, nothing pressed")
            else:
                # Only the `claude /login` this flow launched (a Claude in this
                # pane's own process tree that did not exist before the launch).
                _auth_check(flow)
                killed = await _kill_claude_process(pane, exclude=launched_pre_pids, require_arg="/login")
                if killed:
                    log(f"Stopped the sign-in Claude this portal launched (pid {', '.join(map(str, killed))}) for a clean retry")
                    await nap(1.0)
                if await _is_claude_running_async(pane):
                    log("Could not safely identify the sign-in Claude to stop — stopping, nothing killed")
                    return stop(error="could not safely stop the sign-in Claude; nothing killed")
            # Clear tmux scrollback before next attempt so stale error text cannot poison detection
            await _run_subprocess_async(["tmux", "clear-history", "-t", pane])

    log(f"Auth flow FAILED after {max_retries + 1} attempts")
    return stop(error="auth flow failed after retries")


# ---------------------------------------------------------------------------
# Claude sign-in status (fix 2026-09-24, ticket 3270)
# ---------------------------------------------------------------------------
# The old endpoint returned authenticated:true whenever the tmux session was
# alive, even when the credentials were long expired or the refresh token was
# empty — so a signed-out CIV never showed the self-serve sign-in modal
# (dispatch-yash #3263, alexia-jordannah #3264, lloyd-bobby #3268). It now
# reads the REAL credential state and fails CLOSED when unsure:
#   * no file / unreadable / no access token / no usable expiry -> false
#   * access token not yet expired                                -> true
#   * expired and refresh token empty (dead grant)                -> false
#   * expired, refresh token present                              -> true,
#     UNLESS the API reported an auth failure after these credentials were
#     written (review round 2, ticket 3383: an idle CIV never refreshes, so an
#     old expiresAt alone must not show "signed out" or open the modal)
# Secrets (access/refresh tokens) are never returned.
AUTH_EXPIRY_SKEW_MS = 60_000
try:
    AUTH_REFRESH_GRACE_S = max(0, int(os.environ.get("PORTAL_AUTH_REFRESH_GRACE_S", "7200")))
except ValueError:
    AUTH_REFRESH_GRACE_S = 7200
# How long after the access token expired an IDLE AI (refresh token present,
# no API auth failure seen) still reads signed in. Default 14 days.
try:
    AUTH_REFRESH_MAX_IDLE_S = max(3600, int(os.environ.get("PORTAL_AUTH_REFRESH_MAX_IDLE_S", str(14 * 86400))))
except ValueError:
    AUTH_REFRESH_MAX_IDLE_S = 14 * 86400
_AUTH_FAILURE_PANE_RE = re.compile(
    r'OAuth (?:access )?token has expired|token has been revoked|Please run /login|'
    r'Login expired|Invalid API key|authentication_error|'
    r'Could not resolve authentication method|API Error: 401',
    re.IGNORECASE,
)


# An access token claiming to be valid further ahead than this is not a real
# Claude sign-in (interactive tokens live hours); e.g. expiresAt=1e18 is
# treated as UNKNOWN -> not signed in. Env may tighten or loosen within bounds.
try:
    AUTH_MAX_TOKEN_HORIZON_S = min(400 * 86400, max(86400, int(
        os.environ.get("PORTAL_AUTH_MAX_TOKEN_HORIZON_S", str(30 * 86400)))))
except ValueError:
    AUTH_MAX_TOKEN_HORIZON_S = 30 * 86400


def _evaluate_claude_credentials(creds_path: Path, now_ms: int = None) -> dict:
    """Pure credential check. Returns a dict with NO secrets in it:
    {authenticated, reason, expires_at, subscription, needs_live_check}."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out = {"authenticated": False, "reason": "", "expires_at": None,
           "subscription": None, "needs_live_check": False, "account": None}
    try:
        creds = json.loads(Path(creds_path).read_text())
    except FileNotFoundError:
        out["reason"] = "no_credentials_file"
        return out
    except Exception:
        out["reason"] = "credentials_unreadable"
        return out
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        out["reason"] = "no_access_token"
        return out
    sub = oauth.get("subscriptionType")
    out["subscription"] = sub if isinstance(sub, str) else None
    acct = oauth.get("account")
    if isinstance(acct, dict):
        acct = acct.get("email_address") or acct.get("emailAddress")
    out["account"] = acct.strip() if (isinstance(acct, str) and acct.strip() and len(acct) <= 320
                                      and "sk-ant-" not in acct) else None
    expires_at = oauth.get("expiresAt")
    if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at) or expires_at <= 0):
        out["reason"] = "expiry_unknown"
        return out
    if expires_at > now_ms + AUTH_MAX_TOKEN_HORIZON_S * 1000:
        # Absurd / corrupt expiry (e.g. 1e18): unknown, fail closed.
        out["reason"] = "expiry_unknown_implausible"
        return out
    expires_at = int(expires_at)
    out["expires_at"] = expires_at
    if expires_at > now_ms + AUTH_EXPIRY_SKEW_MS:
        out["authenticated"] = True
        out["reason"] = "token_valid"
        return out
    refresh = oauth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh.strip():
        out["reason"] = "expired_no_refresh_token"
        return out
    # An expired ACCESS token with a refresh token is not a sign-out: Claude
    # Code refreshes it on its next model request. An idle CIV makes none, so
    # its file can show an access token hours or days old while it is fine.
    # The caller decides from evidence (an auth failure reported by the API).
    out["needs_live_check"] = True
    # Unused for longer than a refresh grant is trusted to live: signed out
    # UNLESS the transcripts prove the grant still works (decided by the
    # caller). Bounds "idle is not signed out" (review rounds 2/3).
    out["too_old"] = now_ms - expires_at > AUTH_REFRESH_MAX_IDLE_S * 1000
    if now_ms - expires_at > AUTH_REFRESH_GRACE_S * 1000:
        out["reason"] = "expired_beyond_refresh_grace"
    else:
        out["reason"] = "expired_within_refresh_grace"
    return out


# --- Positive/negative evidence from the CIV's own transcripts (cheap) -------
# Claude Code writes every model reply to ~/.claude/projects/*/<session>.jsonl.
# A REAL assistant turn (a model id, not "<synthetic>") timestamped AFTER the
# access token expired proves the refresh grant still works. A synthetic API
# error row with error == "authentication_failed" (text "Not logged in · Please
# run /login", verified on-disk 2026-09-24) or HTTP 401 proves it does not.
# Rate-limit / 5xx / model errors are neither. Only the tail of the newest few
# files is read, results are cached on (path, mtime, size), and no transcript
# text ever leaves this function.
_AUTH_EVIDENCE_MAX_FILES = 3
_AUTH_EVIDENCE_TAIL_BYTES = 512 * 1024
_auth_evidence_cache: dict = {}
_NOT_LOGGED_IN_RE = re.compile(r'Not logged in', re.IGNORECASE)


def _parse_iso_ms(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _record_text(rec: dict) -> str:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def _classify_assistant_record(rec: dict):
    """'real' | 'auth_fail' | None (neutral / not an assistant row)."""
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    if rec.get("isApiErrorMessage"):
        if rec.get("error") == "authentication_failed" or rec.get("apiErrorStatus") == 401:
            return "auth_fail"
        text = _record_text(rec)
        if _NOT_LOGGED_IN_RE.search(text) or _AUTH_FAILURE_PANE_RE.search(text):
            return "auth_fail"
        return None
    msg = rec.get("message")
    model = msg.get("model") if isinstance(msg, dict) else None
    if isinstance(model, str) and model and model != "<synthetic>":
        return "real"
    return None


def _scan_lines(lines, res: dict, stop_early: bool = False) -> dict:
    for raw in lines:
        if b'"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        kind = _classify_assistant_record(rec)
        if kind is None:
            continue
        key = "last_real_ms" if kind == "real" else "last_auth_fail_ms"
        ts = _parse_iso_ms(rec.get("timestamp"))
        if ts is not None and (res[key] is None or ts > res[key]):
            res[key] = ts
        if stop_early and res["last_real_ms"] is not None and res["last_auth_fail_ms"] is not None:
            break
    return res


def _scan_transcript_tail(path: Path) -> dict:
    """Full scan of the last _AUTH_EVIDENCE_TAIL_BYTES of one transcript."""
    res, _ = _scan_transcript_tail_with_offset(path)
    return res


def _scan_transcript_tail_with_offset(path: Path):
    res = {"last_real_ms": None, "last_auth_fail_ms": None}
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = max(0, size - _AUTH_EVIDENCE_TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return res, None
    end = data.rfind(b"\n")
    complete = data[:end + 1] if end >= 0 else b""
    lines = complete.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # first line is partial
    _scan_lines(reversed(lines), res, stop_early=True)
    return res, start + len(complete)


# Per transcript: {"ino", "offset", "res"}. A growing transcript is read ONLY
# from where the last scan stopped (review round 2: the active transcript
# changes between every status call, so a (mtime, size) cache always missed and
# each call re-read and parsed ~1.5 MB).


_auth_evidence_lock = threading.Lock()


def _scan_transcript_incremental(p: Path) -> dict:
    # Status calls run this in worker threads; the per-file offset must only
    # ever advance once per byte range (review round 2, #6).
    with _auth_evidence_lock:
        return _scan_transcript_incremental_locked(p)


def _scan_transcript_incremental_locked(p: Path) -> dict:
    try:
        st = p.stat()
    except OSError:
        return {"last_real_ms": None, "last_auth_fail_ms": None}
    key = str(p)
    ent = _auth_evidence_cache.get(key)
    grow = None
    if ent is not None and isinstance(ent.get("offset"), int):
        grow = st.st_size - ent["offset"]
    if grow is None or ent["ino"] != st.st_ino or grow < 0 or grow > _AUTH_EVIDENCE_TAIL_BYTES:
        res, offset = _scan_transcript_tail_with_offset(p)
        if offset is None:
            _auth_evidence_cache.pop(key, None)  # unreadable now: rescan next time
        else:
            _auth_evidence_cache[key] = {"ino": st.st_ino, "offset": offset, "res": dict(res)}
        return res
    if grow == 0:
        return dict(ent["res"])
    try:
        with open(p, "rb") as fh:
            fh.seek(ent["offset"])
            data = fh.read(grow)
    except OSError:
        return dict(ent["res"])
    end = data.rfind(b"\n")
    if end < 0:
        return dict(ent["res"])  # only a partial line so far
    res = dict(ent["res"])
    _scan_lines(data[:end + 1].split(b"\n"), res)
    ent["offset"] += end + 1
    ent["res"] = dict(res)
    return res


def _auth_turn_evidence(paths=None) -> dict:
    """Newest real-turn and auth-failure timestamps (epoch ms) across the
    newest few transcripts. Never raises; never returns transcript text.
    Cheap: an unchanged file costs one stat(), a growing one is read only from
    where the previous scan stopped (_scan_transcript_incremental)."""
    out = {"last_real_ms": None, "last_auth_fail_ms": None}
    try:
        if paths is None:
            paths = _find_all_project_jsonl()[:_AUTH_EVIDENCE_MAX_FILES]
        for p in list(paths)[:_AUTH_EVIDENCE_MAX_FILES]:
            res = _scan_transcript_incremental(Path(p))
            for k in out:
                if res[k] is not None and (out[k] is None or res[k] > out[k]):
                    out[k] = res[k]
        if len(_auth_evidence_cache) > 64:
            _auth_evidence_cache.clear()
    except Exception:
        pass
    return out


def _decide_auth_with_evidence(ev: dict, evidence: dict, creds_mtime_ms):
    """Combine the credential check with transcript evidence. Returns
    (authenticated, reason, still_needs_pane_check). The third value is kept
    for callers and is always False now: the pane text is no longer consulted
    (old scrollback or the CIV's own conversation mentioning "API Error: 401"
    flipped the answer). Only structured API-error records count."""
    real = evidence.get("last_real_ms")
    fail = evidence.get("last_auth_fail_ms")
    # An auth failure counts only if it happened after THESE credentials were
    # written and nothing succeeded since.
    fail_now = (fail is not None
                and (creds_mtime_ms is None or fail > creds_mtime_ms)
                and (real is None or fail > real))
    if ev["authenticated"]:
        # Unexpired token, but the API rejected it AFTER this credentials file
        # was written (and nothing succeeded since): revoked server-side.
        if fail_now and creds_mtime_ms is not None:
            return False, "token_rejected_by_api", False
        return True, ev["reason"], False
    if not ev["needs_live_check"]:
        return False, ev["reason"], False
    # Expired access token + refresh token present.
    if fail_now:
        return False, "expired_api_reports_auth_failure", False
    if real is not None and ev["expires_at"] is not None and real >= ev["expires_at"]:
        return True, "expired_refresh_proven_by_recent_turn", False
    if ev.get("too_old"):
        return False, "expired_refresh_too_old", False
    return True, "expired_refresh_pending", False


def _engine_is_managed() -> bool:
    """True when the AI engine does not use a personal Claude login.

    Router-backed installs (e.g. the MiniMax-M3 trial) authenticate through
    their router, so the portal must not ask the customer to sign in to Claude.
    Signals, any of: PORTAL_ENGINE_MANAGED=1; a non-Claude "model" in
    config/trial.json; ANTHROPIC_BASE_URL set in ~/.claude/settings.json env.
    """
    if os.environ.get("PORTAL_ENGINE_MANAGED", "").strip().lower() in ("1", "true", "yes"):
        return True
    model = configured_model()
    if model and not model.lower().startswith("claude"):
        return True
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        if str((settings.get("env") or {}).get("ANTHROPIC_BASE_URL", "")).strip():
            return True
    except Exception:
        pass
    return False


# The Claude account the engine is signed in as, for the Status page's "Engine
# account" row (restored in review round 2; it matters most right after a
# Reconnect used to switch accounts). An e-mail / label only — never a token.
_ACCOUNT_FILE_MAX_BYTES = 32 * 1024 * 1024
_account_label_cache: dict = {}


def _claude_account_label(oauth_account=None):
    """The account label: the credentials' own `account` field if present,
    else ~/.claude.json oauthAccount.emailAddress. The big ~/.claude.json is
    parsed only when its (mtime, size) changed; a failed parse (Claude Code
    rewriting it) keeps the last good label instead of flickering to None."""
    if oauth_account:
        return oauth_account
    p = Path.home() / ".claude.json"
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _account_label_cache.get("v")
    if cached and cached[0] == key:
        return cached[1]
    if st.st_size > _ACCOUNT_FILE_MAX_BYTES:
        return cached[1] if cached else None
    try:
        oa = json.loads(p.read_text()).get("oauthAccount")
    except Exception:
        return cached[1] if cached else None
    label = None
    if isinstance(oa, dict):
        v = oa.get("emailAddress")
        if isinstance(v, str) and v.strip() and len(v) <= 320 and "sk-ant-" not in v:
            label = v.strip()
    _account_label_cache["v"] = (key, label)
    return label


async def api_claude_auth_status(request: Request) -> JSONResponse:
    """Report whether Claude is REALLY signed in (credential expiry + refresh
    token + the API's own auth-failure records). Fails closed to
    not-authenticated when the credentials are missing, unreadable, dead, or
    the API has reported them rejected."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _engine_is_managed():
        return JSONResponse({"authenticated": True, "managed": True, "account": None,
                             "reason": "managed_engine", "expires_at": None, "subscription": None})
    try:
        ev = await asyncio.to_thread(_evaluate_claude_credentials, CREDENTIALS_FILE)
        authenticated = ev["authenticated"]
        reason = ev["reason"]
        try:
            creds_mtime_ms = int(CREDENTIALS_FILE.stat().st_mtime * 1000)
        except OSError:
            creds_mtime_ms = None
        if ev["authenticated"] or ev["needs_live_check"]:
            evidence = await asyncio.to_thread(_auth_turn_evidence)
            authenticated, reason, _ = _decide_auth_with_evidence(ev, evidence, creds_mtime_ms)
        account = None
        if creds_mtime_ms is not None:
            account = await asyncio.to_thread(_claude_account_label, ev.get("account"))
        return JSONResponse({
            "authenticated": bool(authenticated),
            "account": account,  # e-mail / label only; never a secret
            "reason": reason,
            "expires_at": ev["expires_at"],
            "subscription": ev["subscription"] if authenticated else None,
        })
    except Exception:
        return JSONResponse({"authenticated": False, "account": None, "reason": "status_check_error",
                             "expires_at": None, "subscription": None})


AUTH_START_WAIT_S = 20.0


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Start Claude OAuth flow using v2 state machine with screen detection.

    Drives the full auth flow: starts Claude, detects and dismisses blocking
    dialogs, selects the login option, and waits for the OAuth URL. The flow
    runs as a background task: this request waits up to AUTH_START_WAIT_S and
    returns the result, or {"started": true, "pending": true} — the browser
    then polls /api/auth/url, which also reports how the flow ended. So a
    proxy timeout on this request never orphans the flow (review round 3,
    #8). Only ONE flow runs at a time; POST /api/auth/close cancels it (also
    when the Close arrives first: same "attempt" id).
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url, _auth_flow, _auth_code_submission, _last_auth_flow
    body = await _auth_body(request)
    attempt = _attempt_id(body)
    if attempt and attempt in _closed_attempts:
        return JSONResponse({"started": False, "cancelled": True, "error": "sign-in cancelled"})
    if _auth_flow_running():
        return JSONResponse({"started": False, "in_progress": True,
                             "error": "A sign-in is already starting. Wait a moment, or close it and start again."})
    # Register BEFORE any await so a Close can always find (and cancel) it.
    flow = _AuthFlow(None, attempt)
    _auth_flow = flow
    _last_auth_flow = flow
    _captured_oauth_url = None
    _auth_code_submission = None
    _release_signin_hold()

    async def runner():
        global _auth_flow
        try:
            _auth_check(flow)
            pane = await _find_primary_pane_async()
            flow.pane = pane
            _save_portal_message(f"Auth flow v2 started — {get_tmux_session()} (pane {pane})", role="assistant")
            flow.result = await _run_auth_state_machine(pane, flow)
        except _AuthCancelled:
            _save_portal_message("Auth flow v2 cancelled by the owner — stopped", role="assistant")
            flow.result = {"started": False, "cancelled": True, "error": "sign-in cancelled"}
        except Exception as e:
            _save_portal_message(f"Auth flow v2 failed: {e}", role="assistant")
            flow.result = {"started": False, "error": f"auth flow error: {e}"}
        finally:
            flow.finished.set()
            if _auth_flow is flow:
                _auth_flow = None

    flow.task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(asyncio.shield(flow.finished.wait()), timeout=AUTH_START_WAIT_S)
    except asyncio.TimeoutError:
        return JSONResponse({"started": True, "pending": True})
    result = dict(flow.result or {"started": False, "error": "auth flow ended without a result"})
    if result.get("error") and "started" not in result:
        result["started"] = False
    return JSONResponse(result)


async def api_claude_auth_prewarm(request: Request) -> JSONResponse:
    """Pre-warm Claude for faster auth. Called when portal page loads.

    Starts `claude /login` in the background without waiting for URL.
    When the human clicks Authenticate, Claude is already started and the
    login menu is likely rendered, shaving 30-45s off the flow.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _auth_prewarm_task, _auth_flow
    # Don't start if already prewarming
    if _auth_prewarm_task and not _auth_prewarm_task.done():
        return JSONResponse({"status": "already_prewarming"})
    if _auth_flow_running():
        return JSONResponse({"status": "skipped", "reason": "a sign-in flow is running"})
    # Holds the one-flow slot while it types, so Start and prewarm never both
    # type into the pane (review round 2, #10).
    flow = _AuthFlow(None, None)
    _auth_flow = flow
    try:
        pane = await _find_primary_pane_async()
        flow.pane = pane
        _save_portal_message("Pre-warming Claude for auth...", role="assistant")
        # Resize tmux
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await asyncio.sleep(0.3)
        pane_state = await _pane_process_state(pane)
        if pane_state == "shell":
            launch_cmd = f"cd {shlex.quote(str(Path.home()))} && claude /login"
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", launch_cmd], check=True)
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
            _save_portal_message("Pre-warm: Claude /login launched in background", role="assistant")
        elif pane_state == "claude":
            _save_portal_message("Pre-warm: Claude already running", role="assistant")
        else:
            _save_portal_message("Pre-warm: pane state unknown — nothing typed", role="assistant")
            return JSONResponse({"status": "skipped", "reason": "pane state unknown"})
        return JSONResponse({"status": "prewarming"})
    except Exception as e:
        return JSONResponse({"error": f"prewarm failed: {e}"}, status_code=500)
    finally:
        flow.finished.set()
        if _auth_flow is flow:
            _auth_flow = None


# The code Claude's sign-in page shows is one token (letters, digits and a few
# URL-safe marks, usually "<code>#<state>"). Anything else is refused before a
# key is pressed: a newline inside it would submit whatever came before.
_AUTH_CODE_RE = re.compile(r'[A-Za-z0-9._~#+/=\-]{8,4096}')
_AUTH_PASTE_PROMPT_RE = re.compile(r'Paste code here', re.IGNORECASE)
AUTH_NOT_WAITING_MESSAGE = ("The sign-in screen is not waiting for a code any more. "
                            "Close this and start the sign-in again.")
_auth_code_submission = None  # what the screen/credentials looked like when a code was typed


def _pane_awaits_auth_code(visible_text: str) -> bool:
    """Pure: is Claude Code's "Paste code here" prompt the ACTIVE screen? It
    must be the last sign-in screen drawn, with no input prompt ("> ") and no
    "Login successful" after it. Otherwise the code would be typed into the
    AI's chat input and sent as a message."""
    if not visible_text:
        return False
    lines = _screen_lines(visible_text)
    last_paste = max((i for i, ln in enumerate(lines) if _AUTH_PASTE_PROMPT_RE.search(ln)), default=-1)
    if last_paste < 0:
        return False
    last_prompt = _last_input_prompt(lines)
    last_finish = max((i for i, ln in enumerate(lines) if _AUTH_CLOSE_FINISH_RE.search(ln)), default=-1)
    return last_paste > last_prompt and last_paste > last_finish


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Type the OAuth authorization code into Claude's "Paste code here"
    prompt — and ONLY there. Refuses (types nothing) when that prompt is not
    the active screen of a running Claude."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _auth_code_submission
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)
    if not _AUTH_CODE_RE.fullmatch(code):
        return JSONResponse({"injected": False,
                             "error": "That does not look like a sign-in code. Copy the whole code and paste it again."},
                            status_code=400)
    if _auth_flow_running():
        return JSONResponse({"injected": False, "not_waiting": True,
                             "error": "The sign-in is still starting. Wait for the link, then paste the code."})
    pane = await _find_primary_pane_async()
    try:
        if await _pane_process_state(pane) != "claude":
            return JSONResponse({"injected": False, "not_waiting": True, "error": AUTH_NOT_WAITING_MESSAGE})
        visible = await _capture_visible(pane)
        if _session_is_busy(visible) or not _pane_awaits_auth_code(visible):
            _save_portal_message("Auth code NOT typed — the sign-in screen is not waiting for a code", role="assistant")
            return JSONResponse({"injected": False, "not_waiting": True, "error": AUTH_NOT_WAITING_MESSAGE})
        try:
            creds_mtime_ns = CREDENTIALS_FILE.stat().st_mtime_ns
        except OSError:
            creds_mtime_ns = None
        _auth_code_submission = {"pane": pane, "at": time.time(), "creds_mtime_ns": creds_mtime_ns}
        _save_portal_message(f"Auth code submitted — injecting into {get_tmux_session()}...", role="assistant")
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", code], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        _save_portal_message("Code injected — Claude is authenticating...", role="assistant")
        return JSONResponse({"injected": True})
    except Exception as e:
        _save_portal_message(f"Code injection failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_verify(request: Request) -> JSONResponse:
    """GET /api/auth/verify — did the code typed by /api/auth/code produce a
    REAL new sign-in? Needs BOTH: Claude Code printed a NEW "Login successful" /
    "Logged in as" since the code was typed, AND the credentials file was
    rewritten since then and is valid. A background token refresh rewrites
    the file (and its expiresAt) but prints nothing, so it never counts."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sub = _auth_code_submission
    if not sub:
        return JSONResponse({"confirmed": False, "state": "no_code_submitted"})
    # The code was only typed while the paste prompt was ACTIVE, so an ACTIVE
    # "Login successful … Press Enter" screen now is new by construction
    # (window-independent: no counting of lines that may scroll away).
    screen_ok = _plan_auth_close(await _capture_visible(sub["pane"])) == "Enter"
    try:
        mt = CREDENTIALS_FILE.stat().st_mtime_ns
    except OSError:
        mt = None
    rewritten = mt is not None and (sub["creds_mtime_ns"] is None or mt > sub["creds_mtime_ns"])
    valid = _evaluate_claude_credentials(CREDENTIALS_FILE)["authenticated"] if rewritten else False
    confirmed = bool(screen_ok and rewritten and valid)
    return JSONResponse({"confirmed": confirmed,
                         "state": "confirmed" if confirmed else "waiting",
                         "screen_confirmed": bool(screen_ok),
                         "credentials_rewritten": bool(rewritten)})


# ---------------------------------------------------------------------------
# Close the sign-in screen in a LIVE session (Reconnect Claude, ticket 3383)
# ---------------------------------------------------------------------------
# "Reconnect Claude" sends /login into the CIV's live Claude session. If the
# owner cancels, the login picker / code prompt must not be left sitting in the
# AI's own pane; after a successful sign-in Claude Code waits on "Press Enter to
# continue". This endpoint only ever presses Escape or Enter, only when the
# VISIBLE screen shows one of those sign-in screens, and only when the pane is
# a running Claude. It never types text and never kills anything.
_AUTH_CLOSE_FINISH_RE = re.compile(r'(?:Login successful|Logged in as)', re.IGNORECASE)
_AUTH_CLOSE_PRESS_ENTER_RE = re.compile(r'Press Enter', re.IGNORECASE)
_AUTH_CLOSE_CANCEL_RE = re.compile(
    r'Select login method|Claude account with subscription|Paste code here|'
    r"Browser didn't open|oauth/authorize|Anthropic Console account",
    re.IGNORECASE,
)




def _plan_auth_close(screen_text: str):
    """Pure: which single key closes the visible sign-in screen? 'Enter' after a
    successful sign-in that waits for Enter, 'Escape' for the login picker or
    the code prompt, None when no sign-in screen is ACTIVE. A sign-in screen
    only counts when no Claude input prompt ("> ") was drawn after it: an old
    login screen left above the prompt must never earn a key press (a stray
    double Escape at the prompt opens Claude Code's rewind menu)."""
    if not screen_text:
        return None
    lines = _screen_lines(screen_text)
    last_prompt = _last_input_prompt(lines)
    last_finish = max((i for i, ln in enumerate(lines) if _AUTH_CLOSE_FINISH_RE.search(ln)), default=-1)
    last_cancel = max((i for i, ln in enumerate(lines) if _AUTH_CLOSE_CANCEL_RE.search(ln)), default=-1)
    tail_after = lambda i: "\n".join(lines[i:])
    if last_finish > last_prompt and last_finish >= last_cancel and _AUTH_CLOSE_PRESS_ENTER_RE.search(tail_after(last_finish)):
        return "Enter"
    if last_cancel > last_prompt and last_cancel > last_finish:
        return "Escape"
    return None


async def api_claude_auth_close(request: Request) -> JSONResponse:
    """POST /api/auth/close — the owner closed the sign-in dialog.

    1. STOPS a running sign-in state machine (it presses nothing more).
    2. Tidies the live session: at most ONE Escape (login picker / code
       prompt) or ONE Enter (after "Login successful"), only when that screen
       is active in a running Claude that is not mid-turn."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url, _auth_code_submission
    attempt = _attempt_id(await _auth_body(request))
    if attempt:
        _closed_attempts.append(attempt)
        del _closed_attempts[:-32]
    flow = _auth_flow
    stopped = False
    if flow is not None and not flow.finished.is_set():
        flow.cancel.set()
        try:
            await asyncio.wait_for(flow.finished.wait(), timeout=8.0)
            stopped = True
        except asyncio.TimeoutError:
            stopped = False
    _captured_oauth_url = None
    _auth_code_submission = None
    _release_signin_hold()
    pane = (flow.pane if flow is not None else None) or await _find_primary_pane_async()
    try:
        if flow is not None and not flow.finished.is_set():
            return JSONResponse({"closed": False, "flow_stopped": False,
                                 "reason": "sign-in flow did not stop in time; nothing pressed"})
        if await _pane_process_state(pane) != "claude":
            return JSONResponse({"closed": False, "flow_stopped": stopped,
                                 "reason": "pane is not a running Claude; nothing pressed"})
        visible = await _capture_visible(pane)
        # URLs on screen now are dead the moment the dialog closes.
        _stale_oauth_urls.update(_oauth_urls(visible))
        if _session_is_busy(visible):
            return JSONResponse({"closed": False, "flow_stopped": stopped, "pressed": [],
                                 "reason": "the AI is mid-turn; nothing pressed"})
        # ONE key per call, never a loop: a second Escape would land on the
        # live prompt.
        key = _plan_auth_close(visible)
        if key is None:
            return JSONResponse({"closed": False, "flow_stopped": stopped, "pressed": []})
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, key])
        return JSONResponse({"closed": True, "flow_stopped": stopped, "pressed": [key]})
    except Exception as e:
        return JSONResponse({"error": f"close failed: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from tmux output."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})
    last = _last_auth_flow
    if last is not None and last.finished.is_set():
        # The flow ended without a URL: say how, so the page never spins.
        res = last.result or {}
        out = {"url": None, "ready": False, "done": True}
        for k in ("already_authenticated", "busy", "cancelled", "error"):
            if res.get(k):
                out[k] = res[k]
        if res.get("url"):
            return JSONResponse({"url": res["url"], "ready": True})
        return JSONResponse(out)
    if last is not None and not last.finished.is_set():
        return JSONResponse({"url": None, "ready": False})
    pane = await _find_primary_pane_async()
    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = await _run_subprocess_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-200"], timeout=5
        )
        if not content:
            return JSONResponse({"url": None, "ready": False})
        # Only a complete URL (state= present) that was not already on screen
        # before this sign-in started. A truncated or stale URL is worse than
        # none ("missing state" / "invalid state" on claude.ai).
        candidate = _fresh_oauth_url(content, _stale_oauth_urls)
        if candidate:
            _captured_oauth_url = candidate
            _save_portal_message(f"OAuth URL ready ({len(candidate)} chars, state= confirmed)", role="assistant")
            return JSONResponse({"url": _captured_oauth_url, "ready": True})
        # Silently return — no notification on each poll. Only notify when URL is found.
    except Exception as e:
        _save_portal_message(f"tmux capture failed: {e}", role="assistant")
    return JSONResponse({"url": None, "ready": False})


# ---------------------------------------------------------------------------
# Evolution Endpoints
# ---------------------------------------------------------------------------

def _extract_evolution_prompt(skill_path: Path, prompt_path: Path) -> str:
    """Extract the first-visit evolution prompt from prompt.txt or SKILL.md."""
    if prompt_path.exists():
        return prompt_path.read_text().strip()
    if skill_path.exists():
        content = skill_path.read_text()
        # Find the section "## The Prompt (Inject This)"
        marker = "## The Prompt (Inject This)"
        idx = content.find(marker)
        if idx != -1:
            section = content[idx:]
            # Find first ``` block in that section
            first_fence = section.find("```")
            if first_fence != -1:
                # Skip language specifier line if present
                after_fence = section[first_fence + 3:]
                newline_idx = after_fence.find("\n")
                if newline_idx != -1:
                    after_first_line = after_fence[newline_idx + 1:]
                    end_fence = after_first_line.find("```")
                    if end_fence != -1:
                        return after_first_line[:end_fence].strip()
    return "You just woke up. Read your constitution: /home/aiciv/.claude/CLAUDE.md — then greet your human."


async def api_evolution_status(request: Request) -> JSONResponse:
    """Return the current evolution state: pending | in_progress | complete."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if EVOLUTION_DONE_MARKER.exists():
        return JSONResponse({"status": "complete"})
    if FIRST_BOOT_MARKER.exists():
        return JSONResponse({"status": "in_progress"})
    return JSONResponse({"status": "pending"})


async def api_evolution_first_boot(request: Request) -> JSONResponse:
    """Transition from /login Claude to evolution Claude in the SAME pane.

    Uses double Ctrl-C to gracefully exit the /login Claude instance,
    then launches evolution Claude in the same tmux pane. This keeps
    the portal terminal view on a single pane throughout.

    Guards: skips if already evolved or already fired this session.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if EVOLUTION_DONE_MARKER.exists():
        return JSONResponse({"status": "already_evolved"})
    if FIRST_BOOT_MARKER.exists():
        return JSONResponse({"status": "already_fired"})
    # An AI that has already had real conversations is not a newborn, even if
    # both markers are missing (an interrupted or orchestrator-awakened birth).
    # Its live session is never killed and re-awakened (review round 3, #3).
    evidence = await asyncio.to_thread(_auth_turn_evidence)
    if evidence.get("last_real_ms") is not None:
        _save_portal_message("Sign-in complete — this AI is already awake, no first-boot needed", role="assistant")
        return JSONResponse({"status": "already_active"})
    # Write marker before launching — prevents double-fire on concurrent calls
    try:
        FIRST_BOOT_MARKER.write_text(str(time.time()))
    except Exception as e:
        return JSONResponse({"error": f"could not write marker: {e}"}, status_code=500)

    pane = await _find_primary_pane_async()
    project_dir = str(Path.home())

    # Step 1: Double Ctrl-C to kill the /login Claude instance in the same pane
    _save_portal_message("Auth complete — transitioning to evolution (same pane)...", role="assistant")
    # Only the Claude in THIS pane (the /login instance), by PID. Never
    # `pkill -f claude`, which killed every Claude process in the container.
    await _kill_claude_process(pane)

    # Step 2: Wait for Claude to exit (up to 10s)
    exited = False
    for _ in range(20):
        await asyncio.sleep(0.5)
        if not await _is_claude_running_async(pane):
            exited = True
            break
    if not exited:
        _save_portal_message("Claude /login didn't exit cleanly — force-killing", role="assistant")
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "C-c"])
        await asyncio.sleep(2.0)

    # Step 3: Launch fresh Claude session in the SAME pane (no new tmux session)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmux_session = get_tmux_session()
    launch_cmd = f"cd {project_dir} && claude --dangerously-skip-permissions"
    r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", launch_cmd], check=True)
    if r is None:
        return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
    await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)

    # Write session marker for tracking
    marker = Path.home() / ".current_session"
    marker.write_text(tmux_session)

    # Step 4: Wait for Claude to start in the pane (up to 30 seconds).
    # _is_claude_running_async now really detects the process (it used to
    # always answer "not running", so this loop always ran the full 30s before
    # the prompt was typed). Detecting the PROCESS is not the same as the TUI
    # being ready for input, so keep the settle time births have always had:
    # the awakening prompt is never typed sooner than FIRST_BOOT_SETTLE_S
    # after launch.
    launched_at = time.time()
    started = False
    for _ in range(60):
        await asyncio.sleep(0.5)
        if await _is_claude_running_async(pane):
            started = True
            break
    if not started:
        _save_portal_message("Claude session didn't confirm within 30s — proceeding with evolution anyway", role="assistant")
    remaining = FIRST_BOOT_SETTLE_S - (time.time() - launched_at)
    if remaining > 0:
        await asyncio.sleep(remaining)

    # Step 5: Inject the awakening prompt
    prompt_text = _extract_evolution_prompt(FIRST_BOOT_SKILL_PATH, FIRST_BOOT_PROMPT_PATH)
    _save_portal_message("Evolution started — your AI is waking up and reading your conversation...", role="assistant")
    try:
        r = await _run_subprocess_async(
            ["tmux", "send-keys", "-t", pane, "-l", f"\n{prompt_text}"], check=True
        )
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        await asyncio.sleep(0.5)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
        return JSONResponse({"status": "fired", "message": "Evolution started (same-pane transition)"})
    except Exception as e:
        _save_portal_message(f"Evolution injection failed: {e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# Thinking Stream Monitor
# ---------------------------------------------------------------------------

async def _push_thinking_to_clients(text: str, ts: int) -> None:
    """Push a thinking block to all connected WebSocket clients."""
    msg = json.dumps({
        "role": "thinking",
        "text": text,
        "timestamp": ts,
        "id": f"thinking-{hashlib.sha256(text.encode()).hexdigest()[:12]}",
    })
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _push_message_to_clients(entry: dict) -> None:
    """Push any portal message to all connected WebSocket clients immediately.

    Used by api_deliverable (and api_notify) to bypass the 0.8s poll delay so
    file download cards appear live without a page refresh.
    The WS poll loop deduplicates via seen_texts, so double-delivery is safe.
    """
    payload = json.dumps(entry)
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _thinking_monitor_loop() -> None:
    """Background task: tail latest JSONL session file and push thinking blocks to portal."""
    last_file: str = ""
    last_pos: int = 0

    while True:
        try:
            # Find the most recently modified JSONL session file across all projects
            logs = _find_all_project_jsonl()
            if not logs:
                await asyncio.sleep(2)
                continue

            current_file = str(logs[0])

            # If we switched to a new file, reset position
            if current_file != last_file:
                last_file = current_file
                last_pos = 0

            # Read new lines from where we left off
            try:
                with open(current_file, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    if file_size < last_pos:
                        # File was truncated/rotated — reset
                        last_pos = 0
                    f.seek(last_pos)
                    new_bytes = f.read()
                    last_pos = f.tell()
            except Exception:
                await asyncio.sleep(2)
                continue

            if not new_bytes:
                await asyncio.sleep(1.5)
                continue

            lines = new_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant messages
                msg = entry.get("message", {})
                if not msg or msg.get("role") != "assistant":
                    continue

                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue

                # Skip sidechain (background agent output)
                if entry.get("isSidechain"):
                    continue

                # Extract thinking blocks (skip tool_use/tool_result, keep thinking even when tools present)
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "thinking":
                        continue
                    text = block.get("thinking", "").strip()
                    if not text:
                        continue

                    # Dedup via hash
                    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
                    if content_hash in _sent_thinking_hashes:
                        continue
                    _sent_thinking_hashes.add(content_hash)

                    ts = entry.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                        except (ValueError, AttributeError):
                            ts = int(time.time())
                    elif isinstance(ts, (int, float)):
                        ts = int(ts / 1000) if ts > 1e10 else int(ts)
                    else:
                        ts = int(time.time())

                    # Push to all connected clients (non-blocking)
                    if _chat_ws_clients:
                        await _push_thinking_to_clients(text, ts)

        except Exception:
            pass

        await asyncio.sleep(0.8)  # Fast poll — thinking must appear in near-real-time


# ---------------------------------------------------------------------------
# Scheduled Tasks — fire messages at future times
# ---------------------------------------------------------------------------
SCHEDULED_TASKS_FILE = SCRIPT_DIR / "scheduled_tasks.json"
_scheduled_tasks: list = []  # list of {"id", "message", "fire_at", "created_at"}


def _load_scheduled_tasks() -> None:
    """Load pending tasks from disk on startup."""
    global _scheduled_tasks
    if SCHEDULED_TASKS_FILE.exists():
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text())
            # Load ALL tasks — don't filter by fire_at on startup.
            # The checker daemon will handle expired ones and reschedule recurring tasks.
            # Only skip tasks older than 7 days to prevent unbounded growth.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            _scheduled_tasks = [t for t in data if t.get("fire_at", "") > cutoff]
            print(f"[sched] Loaded {len(_scheduled_tasks)} pending tasks")
        except Exception as e:
            print(f"[sched] Error loading tasks: {e}")
            _scheduled_tasks = []


def _save_scheduled_tasks() -> None:
    """Persist pending tasks to disk (atomic write)."""
    try:
        tmp = SCHEDULED_TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_scheduled_tasks, indent=2))
        tmp.rename(SCHEDULED_TASKS_FILE)
    except Exception as e:
        print(f"[sched] Error saving tasks: {e}")


async def _scheduled_task_checker() -> None:
    """Background loop: check every 30s if any tasks are due, inject into tmux."""
    while True:
        await asyncio.sleep(30)
        if not _scheduled_tasks:
            continue
        if is_expired_trial():
            continue  # expired trial: don't spend turns; tasks fire after conversion
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        fired = []
        for task in _scheduled_tasks:
            if task.get("fire_at", "") <= now_iso and _signin_holds_pane():
                continue  # a sign-in owns the pane: fire on the next pass
            if task.get("fire_at", "") <= now_iso:
                # Fire this task: inject into tmux
                session = get_tmux_session()
                if session:
                    msg = task["message"]
                    try:
                        # Inject message into tmux — use async subprocess to avoid blocking event loop
                        await _run_subprocess_async(
                            ["tmux", "send-keys", "-t", session, "-l", f"\n{msg}"],
                            timeout=5, check=True,
                        )
                        await _run_subprocess_async(
                            ["tmux", "send-keys", "-t", session, "Enter"],
                            timeout=5,
                        )
                        # Retry enters to ensure processing
                        for _ in range(3):
                            await asyncio.sleep(0.5)
                            await _run_subprocess_async(
                                ["tmux", "send-keys", "-t", session, "Enter"],
                                timeout=5,
                            )
                        print(f"[sched] Fired task: {task.get('id', 'unknown')}")
                    except Exception as e:
                        print(f"[sched] Failed to fire task: {e}")
                fired.append(task)
        if fired:
            for t in fired:
                _scheduled_tasks.remove(t)
                # If recurring, schedule next occurrence
                recur_type = t.get("recur_type")
                if recur_type in ("daily", "weekly"):
                    try:
                        recur_time = t.get("recur_time", "09:00")
                        h, m = (int(x) for x in recur_time.split(":"))
                        next_fire = None
                        if recur_type == "daily":
                            base = now + timedelta(days=1)
                            next_fire = base.replace(hour=h, minute=m, second=0, microsecond=0)
                        elif recur_type == "weekly":
                            recur_days = t.get("recur_days", [])  # e.g. ["Mon", "Wed"]
                            day_map = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
                            target_nums = [day_map[d] for d in recur_days if d in day_map]
                            for offset in range(1, 8):
                                candidate = now + timedelta(days=offset)
                                candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
                                if candidate.weekday() in [((n - 1) % 7) for n in target_nums]:
                                    # JS weekday (0=Sun) vs Python weekday (0=Mon) — convert
                                    # JS: Sun=0, Mon=1 ... Sat=6
                                    # Python: Mon=0, Tue=1 ... Sun=6
                                    # JS day n → Python day (n - 1) % 7
                                    next_fire = candidate
                                    break
                        if next_fire:
                            new_task = dict(t)
                            new_task["fire_at"] = next_fire.isoformat()
                            new_task["id"] = f"task-{int(now.timestamp())}-recur-{len(_scheduled_tasks)}"
                            _scheduled_tasks.append(new_task)
                            print(f"[sched] Rescheduled {recur_type} task for {next_fire.isoformat()}")
                    except Exception as re:
                        print(f"[sched] Error rescheduling recurring task: {re}")
            _save_scheduled_tasks()


async def api_schedule_task(request) -> JSONResponse:
    """POST /api/schedule-task — schedule a message for future delivery."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message = body.get("message", "").strip()
    fire_at = body.get("fire_at", "").strip()  # ISO 8601 UTC string

    if not message:
        return JSONResponse({"error": "No message"}, status_code=400)
    if not fire_at:
        return JSONResponse({"error": "No fire_at time"}, status_code=400)

    recur_type = (body.get("recur_type") or "").strip() or None   # "daily" | "weekly" | None
    recur_time = (body.get("recur_time") or "").strip() or None   # "HH:MM"
    recur_days = body.get("recur_days") or None                   # ["Mon", "Wed"] for weekly

    task_id = f"task-{int(datetime.now().timestamp())}-{len(_scheduled_tasks)}"
    task = {
        "id": task_id,
        "message": message,
        "fire_at": fire_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if recur_type:
        task["recur_type"] = recur_type
    if recur_time:
        task["recur_time"] = recur_time
    if recur_days:
        task["recur_days"] = recur_days
    _scheduled_tasks.append(task)
    _save_scheduled_tasks()
    print(f"[sched] Scheduled task {task_id} for {fire_at}" + (f" (recur: {recur_type})" if recur_type else ""))
    return JSONResponse({"ok": True, "task_id": task_id, "fire_at": fire_at, "recur_type": recur_type})


BOOP_STATE_FILE = Path.home() / ".claude" / "scheduled-tasks-state.json"

async def api_boops_list(request) -> JSONResponse:
    """GET /api/boops — list all BOOPs from boop_executor config."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = json.loads(BOOP_STATE_FILE.read_text())
        tasks = data.get("tasks", {})
        rules = data.get("boop_rules", {})
        boops = []
        for boop_id, boop in tasks.items():
            boops.append({
                "id": boop_id,
                "description": boop.get("description", ""),
                "frequency": boop.get("frequency", "unknown"),
                "status": boop.get("status", "active"),
                "category": boop.get("category", ""),
                "agent": boop.get("agent", ""),
                "last_run": boop.get("last_run", ""),
                "schedule_slot": boop.get("schedule_slot", ""),
                "override_max_daily": boop.get("override_max_daily", False),
            })
        return JSONResponse({"boops": boops, "rules": rules})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boop_update(request) -> JSONResponse:
    """PATCH /api/boops/{boop_id} — update a BOOP's frequency, status, or description."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boop_id = request.path_params.get("boop_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        data = json.loads(BOOP_STATE_FILE.read_text())
        tasks = data.get("tasks", {})
        if boop_id not in tasks:
            return JSONResponse({"error": "BOOP not found"}, status_code=404)
        boop = tasks[boop_id]
        for field in ("frequency", "status", "description", "schedule_slot", "category", "agent"):
            if field in body:
                boop[field] = body[field]
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        BOOP_STATE_FILE.write_text(json.dumps(data, indent=2))
        print(f"[boop] Updated BOOP {boop_id}: {list(body.keys())}")
        return JSONResponse({"ok": True, "boop": boop})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



async def api_scheduled_tasks_list(request) -> JSONResponse:
    """GET /api/scheduled-tasks — list pending scheduled tasks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"tasks": _scheduled_tasks})


async def api_delete_scheduled_task(request) -> JSONResponse:
    """DELETE /api/scheduled-tasks/{task_id} — cancel a pending scheduled task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    global _scheduled_tasks
    before = len(_scheduled_tasks)
    _scheduled_tasks = [t for t in _scheduled_tasks if t.get("id") != task_id]
    if len(_scheduled_tasks) == before:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
    _save_scheduled_tasks()
    print(f"[sched] Cancelled task {task_id}")
    return JSONResponse({"ok": True})


async def api_update_scheduled_task(request) -> JSONResponse:
    """PUT /api/scheduled-tasks/{task_id} — update an existing scheduled task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Find task
    task = None
    for t in _scheduled_tasks:
        if t.get("id") == task_id:
            task = t
            break
    if not task:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)

    # Update fields
    if "message" in body:
        task["message"] = body["message"]
    if "fire_at" in body:
        task["fire_at"] = body["fire_at"]
    if "recur_type" in body:
        task["recur_type"] = body["recur_type"]
    if "recur_time" in body:
        task["recur_time"] = body["recur_time"]
    if "recur_days" in body:
        task["recur_days"] = body["recur_days"]

    _save_scheduled_tasks()
    print(f"[sched] Updated task {task_id}: fire_at={task.get('fire_at')}")
    return JSONResponse({"ok": True, "task": task})


async def api_patch_scheduled_task(request) -> JSONResponse:
    """PATCH /api/scheduled-tasks/{task_id} — partial update: status, subtasks, notes, order, completion_pct."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    task = None
    for t in _scheduled_tasks:
        if t.get("id") == task_id:
            task = t
            break
    if not task:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)

    # Status: pending | in_progress | completed
    if "status" in body:
        allowed_statuses = {"pending", "in_progress", "completed"}
        new_status = body["status"]
        if new_status in allowed_statuses:
            task["status"] = new_status
            print(f"[sched] Task {task_id} status -> {new_status}")

    # Subtasks: list of {id, text, done}
    if "subtasks" in body:
        subtasks = body["subtasks"]
        if isinstance(subtasks, list):
            task["subtasks"] = subtasks

    # Append a new note {text, ts}
    if "note" in body:
        note_text = str(body["note"]).strip()
        if note_text:
            if "notes" not in task or not isinstance(task["notes"], list):
                task["notes"] = []
            task["notes"].append({
                "text": note_text,
                "ts": datetime.now(timezone.utc).isoformat()
            })

    # Replace all notes
    if "notes" in body:
        notes = body["notes"]
        if isinstance(notes, list):
            task["notes"] = notes

    # Sort order
    if "order" in body:
        try:
            task["order"] = int(body["order"])
        except (TypeError, ValueError):
            pass

    # Completion percentage 0-100
    if "completion_pct" in body:
        try:
            pct = int(body["completion_pct"])
            task["completion_pct"] = max(0, min(100, pct))
        except (TypeError, ValueError):
            pass

    _save_scheduled_tasks()
    return JSONResponse({"ok": True, "task": task})


# ---------------------------------------------------------------------------
# AgentCal Integration
# ---------------------------------------------------------------------------

_agentcal_http: httpx.AsyncClient | None = None
_agentcal_fired_ids: set = set()


def _get_agentcal_client() -> httpx.AsyncClient | None:
    """Lazy-init httpx client for AgentCal API."""
    global _agentcal_http
    if not AGENTCAL_BASE or not AGENTCAL_API_KEY or not AGENTCAL_CALENDAR_ID:
        return None
    if _agentcal_http is None or _agentcal_http.is_closed:
        _agentcal_http = httpx.AsyncClient(
            base_url=f"{AGENTCAL_BASE}/api/v1",
            headers={"Authorization": f"Bearer {AGENTCAL_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=15.0,
        )
    return _agentcal_http


def _load_agentcal_fired_ids():
    """Load previously fired event IDs to prevent double-firing on restart."""
    global _agentcal_fired_ids
    if AGENTCAL_FIRED_IDS_FILE.exists():
        try:
            data = json.loads(AGENTCAL_FIRED_IDS_FILE.read_text())
            _agentcal_fired_ids = set(data)
        except Exception:
            _agentcal_fired_ids = set()


def _save_agentcal_fired_ids():
    """Persist fired event IDs."""
    try:
        # Only keep last 500 IDs to prevent unbounded growth
        ids = list(_agentcal_fired_ids)[-500:]
        AGENTCAL_FIRED_IDS_FILE.write_text(json.dumps(ids))
    except Exception:
        pass


async def _agentcal_event_checker() -> None:
    """Background loop: poll AgentCal for due events, inject prompt_payload into tmux."""
    _load_agentcal_fired_ids()
    await asyncio.sleep(10)  # Wait for startup to complete
    while True:
        try:
            client = _get_agentcal_client()
            if client and is_expired_trial():
                client = None  # expired trial: don't type scheduled prompts into the AI
            if client:
                now = datetime.now(timezone.utc)
                time_min = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
                time_max = (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                resp = await client.get(
                    f"/calendars/{AGENTCAL_CALENDAR_ID}/events",
                    params={"time_min": time_min, "time_max": time_max, "limit": 50},
                )
                if resp.status_code != 200:
                    print(f"[agentcal] API returned {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 200:
                    data = resp.json()
                    events = data.get("items", [])
                    if events:
                        print(f"[agentcal] Poll found {len(events)} events in window")
                    for evt in events:
                        evt_id = evt.get("id", "")
                        if evt_id in _agentcal_fired_ids:
                            continue
                        # Check if event is actually due
                        start_str = evt.get("start", "")
                        try:
                            evt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                            if evt_start.tzinfo is None:
                                evt_start = evt_start.replace(tzinfo=timezone.utc)
                        except (ValueError, AttributeError):
                            continue
                        if evt_start > now:
                            continue
                        # Extract prompt payload
                        payload = evt.get("prompt_payload") or {}
                        prompt_text = ""
                        payload_type = payload.get("type", "prompt_injection")
                        if payload_type in ("prompt_injection", "task_work_block"):
                            prompt_text = payload.get("text", "") or payload.get("description", "")
                        elif payload_type in ("skill_invocation", "grounding_session", "health_check"):
                            prompt_text = payload.get("command", "")
                            if payload.get("args"):
                                prompt_text += f" {payload['args']}"
                        else:
                            # Fallback: use summary as prompt
                            prompt_text = payload.get("text", "") or evt.get("summary", "")
                        if not prompt_text:
                            prompt_text = evt.get("summary", "")
                        if not prompt_text or len(prompt_text) < 2:
                            _agentcal_fired_ids.add(evt_id)
                            continue
                        if _signin_holds_pane():
                            continue  # a sign-in owns the pane: not fired, retried next poll
                        # Fire into tmux
                        session = get_tmux_session()
                        if session:
                            try:
                                await _run_subprocess_async(
                                    ["tmux", "send-keys", "-t", session, "-l", f"\n{prompt_text}"],
                                    timeout=5, check=True,
                                )
                                await _run_subprocess_async(
                                    ["tmux", "send-keys", "-t", session, "Enter"], timeout=5,
                                )
                                for _ in range(3):
                                    await asyncio.sleep(0.5)
                                    await _run_subprocess_async(
                                        ["tmux", "send-keys", "-t", session, "Enter"], timeout=5,
                                    )
                                print(f"[agentcal] Fired event: {evt_id} ({evt.get('summary', '')})")
                            except Exception as e:
                                print(f"[agentcal] Failed to fire event {evt_id}: {e}")
                        _agentcal_fired_ids.add(evt_id)
                        _save_agentcal_fired_ids()
        except Exception as e:
            import traceback
            print(f"[agentcal] Checker error: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(30)


async def api_agentcal_events_list(request: Request) -> JSONResponse:
    """GET /api/agentcal/events — list events for a date range."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"events": [], "error": "AgentCal not configured"})
    params = {}
    if request.query_params.get("time_min"):
        params["time_min"] = request.query_params["time_min"]
    if request.query_params.get("time_max"):
        params["time_max"] = request.query_params["time_max"]
    params["limit"] = request.query_params.get("limit", "250")
    try:
        resp = await client.get(f"/calendars/{AGENTCAL_CALENDAR_ID}/events", params=params)
        data = resp.json()
        return JSONResponse({"events": data.get("items", []), "total": data.get("total", 0),
                             "page": data.get("page", 1), "pages": data.get("pages", 1)})
    except Exception as e:
        return JSONResponse({"events": [], "error": str(e)}, status_code=502)


async def api_agentcal_events_create(request: Request) -> JSONResponse:
    """POST /api/agentcal/events — create an event."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"error": "AgentCal not configured"}, status_code=500)
    try:
        body = await request.json()
        resp = await client.post(f"/calendars/{AGENTCAL_CALENDAR_ID}/events", json=body)
        if resp.status_code in (200, 201):
            return JSONResponse({"ok": True, "event": resp.json()})
        return JSONResponse({"ok": False, "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_agentcal_event_get(request: Request) -> JSONResponse:
    """GET /api/agentcal/events/{evt_id}"""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"error": "AgentCal not configured"}, status_code=500)
    evt_id = request.path_params["evt_id"]
    try:
        resp = await client.get(f"/calendars/{AGENTCAL_CALENDAR_ID}/events/{evt_id}")
        if resp.status_code == 200:
            return JSONResponse({"event": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_agentcal_event_update(request: Request) -> JSONResponse:
    """PATCH /api/agentcal/events/{evt_id}"""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"error": "AgentCal not configured"}, status_code=500)
    evt_id = request.path_params["evt_id"]
    try:
        body = await request.json()
        resp = await client.patch(f"/calendars/{AGENTCAL_CALENDAR_ID}/events/{evt_id}", json=body)
        if resp.status_code == 200:
            return JSONResponse({"ok": True, "event": resp.json()})
        return JSONResponse({"ok": False, "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_agentcal_event_delete(request: Request) -> JSONResponse:
    """DELETE /api/agentcal/events/{evt_id}"""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"error": "AgentCal not configured"}, status_code=500)
    evt_id = request.path_params["evt_id"]
    try:
        resp = await client.delete(f"/calendars/{AGENTCAL_CALENDAR_ID}/events/{evt_id}")
        if resp.status_code in (200, 204):
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_agentcal_events_batch(request: Request) -> JSONResponse:
    """POST /api/agentcal/events/batch — batch create events at intervals."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentcal_client()
    if not client:
        return JSONResponse({"error": "AgentCal not configured"}, status_code=500)
    try:
        body = await request.json()
        template = body.get("template", {})
        interval = body.get("interval_minutes", 30)
        batch_start = datetime.fromisoformat(body["start"].replace("Z", "+00:00"))
        batch_end = datetime.fromisoformat(body["end"].replace("Z", "+00:00"))
        duration = timedelta(minutes=body.get("duration_minutes", 5))

        created = []
        current = batch_start
        while current < batch_end:
            evt = dict(template)
            evt["start"] = current.isoformat()
            evt["end"] = (current + duration).isoformat()
            resp = await client.post(f"/calendars/{AGENTCAL_CALENDAR_ID}/events", json=evt)
            if resp.status_code in (200, 201):
                created.append(resp.json().get("id", ""))
            current += timedelta(minutes=interval)

        return JSONResponse({"ok": True, "created": len(created), "event_ids": created})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# AgentSheets proxy endpoints
# ---------------------------------------------------------------------------
_agentsheets_http: httpx.AsyncClient | None = None


def _get_agentsheets_client() -> httpx.AsyncClient | None:
    """Lazy-init httpx client for AgentSheets API."""
    global _agentsheets_http
    if not AGENTSHEETS_URL:
        return None
    if _agentsheets_http is None or _agentsheets_http.is_closed:
        _agentsheets_http = httpx.AsyncClient(
            base_url=AGENTSHEETS_URL,
            timeout=15.0,
        )
    return _agentsheets_http


async def _agentsheets_headers() -> dict:
    """Return auth headers for AgentSheets. Uses CivAuth JWT if available, else AGENTSHEETS_API_KEY."""
    hdrs = await _get_civauth_headers()
    if hdrs:
        return {**hdrs, "Content-Type": "application/json"}
    if AGENTSHEETS_API_KEY:
        return {"Authorization": f"Bearer {AGENTSHEETS_API_KEY}", "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}


async def api_sheets_workbooks_list(request: Request) -> JSONResponse:
    """GET /api/sheets/workbooks — list all workbooks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"workbooks": [], "error": "AgentSheets not configured"})
    try:
        headers = await _agentsheets_headers()
        resp = await client.get("/workbooks", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # Normalize: API may return list directly or nested
            wbs = data if isinstance(data, list) else data.get("workbooks", data.get("items", []))
            return JSONResponse({"workbooks": wbs})
        return JSONResponse({"workbooks": [], "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"workbooks": [], "error": str(e)}, status_code=502)


async def api_sheets_workbooks_create(request: Request) -> JSONResponse:
    """POST /api/sheets/workbooks — create workbook."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    try:
        body = await request.json()
        headers = await _agentsheets_headers()
        resp = await client.post("/workbooks", json=body, headers=headers)
        if resp.status_code in (200, 201):
            return JSONResponse({"workbook": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_workbook_get(request: Request) -> JSONResponse:
    """GET /api/sheets/workbooks/{wb_id} — get workbook."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    try:
        headers = await _agentsheets_headers()
        resp = await client.get(f"/workbooks/{wb_id}", headers=headers)
        if resp.status_code == 200:
            return JSONResponse({"workbook": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_workbook_update(request: Request) -> JSONResponse:
    """PATCH /api/sheets/workbooks/{wb_id} — update workbook."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    try:
        body = await request.json()
        headers = await _agentsheets_headers()
        resp = await client.patch(f"/workbooks/{wb_id}", json=body, headers=headers)
        if resp.status_code == 200:
            return JSONResponse({"workbook": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_workbook_delete(request: Request) -> JSONResponse:
    """DELETE /api/sheets/workbooks/{wb_id} — delete workbook."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    try:
        headers = await _agentsheets_headers()
        resp = await client.delete(f"/workbooks/{wb_id}", headers=headers)
        if resp.status_code in (200, 204):
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_list(request: Request) -> JSONResponse:
    """GET /api/sheets/workbooks/{wb_id}/sheets — list sheets."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"sheets": [], "error": "AgentSheets not configured"})
    wb_id = request.path_params["wb_id"]
    try:
        headers = await _agentsheets_headers()
        resp = await client.get(f"/workbooks/{wb_id}/sheets", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            sheets = data if isinstance(data, list) else data.get("sheets", data.get("items", []))
            return JSONResponse({"sheets": sheets})
        return JSONResponse({"sheets": [], "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"sheets": [], "error": str(e)}, status_code=502)


async def api_sheets_create(request: Request) -> JSONResponse:
    """POST /api/sheets/workbooks/{wb_id}/sheets — create sheet."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    try:
        body = await request.json()
        headers = await _agentsheets_headers()
        resp = await client.post(f"/workbooks/{wb_id}/sheets", json=body, headers=headers)
        if resp.status_code in (200, 201):
            return JSONResponse({"sheet": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_rows_list(request: Request) -> JSONResponse:
    """GET /api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows — list rows."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"rows": [], "error": "AgentSheets not configured"})
    wb_id = request.path_params["wb_id"]
    sh_id = request.path_params["sh_id"]
    params = {}
    if request.query_params.get("limit"):
        params["limit"] = request.query_params["limit"]
    if request.query_params.get("offset"):
        params["offset"] = request.query_params["offset"]
    try:
        headers = await _agentsheets_headers()
        resp = await client.get(f"/workbooks/{wb_id}/sheets/{sh_id}/rows", params=params, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # Normalize response — API may return rows directly or nested
            if isinstance(data, list):
                return JSONResponse({"rows": data, "total": len(data), "limit": int(params.get("limit", 100)), "offset": int(params.get("offset", 0))})
            return JSONResponse({
                "rows": data.get("rows", data.get("items", [])),
                "total": data.get("total", 0),
                "limit": data.get("limit", int(params.get("limit", 100))),
                "offset": data.get("offset", int(params.get("offset", 0))),
            })
        return JSONResponse({"rows": [], "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"rows": [], "error": str(e)}, status_code=502)


async def api_sheets_rows_create(request: Request) -> JSONResponse:
    """POST /api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows — create row."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    sh_id = request.path_params["sh_id"]
    try:
        body = await request.json()
        headers = await _agentsheets_headers()
        resp = await client.post(f"/workbooks/{wb_id}/sheets/{sh_id}/rows", json=body, headers=headers)
        if resp.status_code in (200, 201):
            return JSONResponse({"row": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_row_update(request: Request) -> JSONResponse:
    """PATCH /api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id} — update row."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    sh_id = request.path_params["sh_id"]
    row_id = request.path_params["row_id"]
    try:
        body = await request.json()
        headers = await _agentsheets_headers()
        resp = await client.patch(f"/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id}", json=body, headers=headers)
        if resp.status_code == 200:
            return JSONResponse({"row": resp.json()})
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_row_delete(request: Request) -> JSONResponse:
    """DELETE /api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id} — delete row."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    sh_id = request.path_params["sh_id"]
    row_id = request.path_params["row_id"]
    try:
        headers = await _agentsheets_headers()
        resp = await client.delete(f"/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id}", headers=headers)
        if resp.status_code in (200, 204):
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_sheets_export(request: Request) -> Response:
    """GET /api/sheets/workbooks/{wb_id}/sheets/{sh_id}/export — export CSV/JSON."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentsheets_client()
    if not client:
        return JSONResponse({"error": "AgentSheets not configured"}, status_code=500)
    wb_id = request.path_params["wb_id"]
    sh_id = request.path_params["sh_id"]
    fmt = request.query_params.get("format", "csv")
    try:
        headers = await _agentsheets_headers()
        resp = await client.get(f"/workbooks/{wb_id}/sheets/{sh_id}/export", params={"format": fmt}, headers=headers)
        if resp.status_code == 200:
            content_type = "text/csv" if fmt == "csv" else "application/json"
            return Response(resp.text, media_type=content_type)
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _migrate_scheduled_tasks_to_agentcal() -> None:
    """One-time migration: move local scheduled_tasks.json to AgentCal."""
    if not SCHEDULED_TASKS_FILE.exists() or not AGENTCAL_API_KEY or not AGENTCAL_CALENDAR_ID:
        return
    migrated_file = SCHEDULED_TASKS_FILE.with_suffix(".json.migrated")
    if migrated_file.exists():
        return  # Already migrated
    try:
        data = json.loads(SCHEDULED_TASKS_FILE.read_text())
        if not data:
            return
        client = _get_agentcal_client()
        if not client:
            return
        count = 0
        for task in data:
            message = task.get("message", "")
            fire_at = task.get("fire_at", "")
            if not message or not fire_at:
                continue
            # Build RRULE from recur_type
            rrule = None
            recur_type = task.get("recur_type")
            if recur_type == "daily":
                rrule = "RRULE:FREQ=DAILY"
            elif recur_type == "weekly":
                days = task.get("recur_days", [])
                day_map = {"Sun": "SU", "Mon": "MO", "Tue": "TU", "Wed": "WE", "Thu": "TH", "Fri": "FR", "Sat": "SA"}
                byday = ",".join(day_map.get(d, "") for d in days if d in day_map)
                if byday:
                    rrule = f"RRULE:FREQ=WEEKLY;BYDAY={byday}"

            # Split message into summary + prompt_payload
            lines = message.strip().split("\n")
            summary = lines[0][:512] if lines else "Migrated Task"

            evt_body = {
                "summary": summary,
                "start": fire_at,
                "end": (datetime.fromisoformat(fire_at.replace("Z", "+00:00")) + timedelta(minutes=5)).isoformat(),
                "prompt_payload": {"type": "prompt_injection", "text": message},
                "metadata": {"migrated_from": "scheduled_tasks.json", "original_id": task.get("id", "")},
            }
            if rrule:
                evt_body["recurrence"] = rrule

            try:
                resp = await client.post(f"/calendars/{AGENTCAL_CALENDAR_ID}/events", json=evt_body)
                if resp.status_code in (200, 201):
                    count += 1
            except Exception:
                pass

        # Backup old file
        SCHEDULED_TASKS_FILE.rename(migrated_file)
        print(f"[agentcal] Migrated {count}/{len(data)} tasks to AgentCal")
    except Exception as e:
        print(f"[agentcal] Migration error: {e}")


async def _startup() -> None:
    """Start background tasks on server startup."""
    print(f"[portal] {trial_source()}")
    try:  # lets civ-tools/react.py find this install wherever it lives
        (Path.home() / ".portal_dir").write_text(str(SCRIPT_DIR.resolve()) + "\n")
    except OSError:
        pass
    _st = trial_state()
    print(
        f"[portal] trial state: trial={_st.get('trial')} expired={_st.get('expired')}"
        + (" (FAIL CLOSED: record untrusted)" if _st.get("config_error") else "")
    )
    _init_portal_log_ids()
    await _init_agents_db()
    await _init_agentmail_db()
    asyncio.create_task(_thinking_monitor_loop())
    asyncio.create_task(_trim_portal_log_periodically())
    # AgentCal daemon replaces old scheduled task checker
    if AGENTCAL_BASE and AGENTCAL_API_KEY and AGENTCAL_CALENDAR_ID:
        asyncio.create_task(_agentcal_event_checker())
        asyncio.create_task(_migrate_scheduled_tasks_to_agentcal())
        print(f"[agentcal] Daemon started (calendar: {AGENTCAL_CALENDAR_ID})")
    else:
        # Fallback to legacy scheduler if AgentCal not configured
        asyncio.create_task(_scheduled_task_checker())
        _load_scheduled_tasks()
        print("[agentcal] Not configured, using legacy scheduler")


async def _trim_portal_log_periodically() -> None:
    """Trim portal-chat.jsonl to last 3000 messages every 30 minutes to prevent unbounded growth."""
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        try:
            _trim_portal_chat_log(max_entries=3000)
        except Exception as _e:
            print(f"[portal] trim error: {_e}")



# ---------------------------------------------------------------------------
# Emoji Reaction Sentiment Engine
# ---------------------------------------------------------------------------

EMOJI_SENTIMENT_MAP = {
    "\U0001F44D": {"label": "positive",   "weight": 1,  "name": "thumbs-up"},
    "\U0001F44E": {"label": "negative",   "weight": -1, "name": "thumbs-down"},
    "\U0001F680": {"label": "excited",    "weight": 2,  "name": "rocket"},
    "\U0001F4B0": {"label": "high-value", "weight": 2,  "name": "money-bag"},
    "\U0001F525": {"label": "fire",       "weight": 2,  "name": "fire"},
    "\u2705":     {"label": "approved",   "weight": 1,  "name": "check-mark"},
    "\U0001F4A5": {"label": "impactful",  "weight": 2,  "name": "explosion"},
    "\U0001F92F": {"label": "mind-blown", "weight": 3,  "name": "mind-blown"},
    "\U0001F4AA": {"label": "empowering", "weight": 1,  "name": "muscle"},
    "\U0001F3AF": {"label": "on-target",  "weight": 2,  "name": "bullseye"},
    "\U0001F48E": {"label": "premium",    "weight": 2,  "name": "gem"},
    "\u2764\uFE0F": {"label": "love",     "weight": 5,  "name": "heart"},
    "\U0001F622": {"label": "disappointed", "weight": -1, "name": "sad-face"},
    "\U0001F610": {"label": "meh",          "weight": 0,  "name": "neutral-face"},
    "\U0001F60D": {"label": "heart-eyes",   "weight": 10, "name": "heart-eyes"},
}

REACTION_LOG = SCRIPT_DIR / "reaction-sentiment.jsonl"


async def api_reaction(request: Request) -> JSONResponse:
    """Log emoji reaction as sentiment data point."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    msg_id = body.get("msg_id", "")
    emoji = body.get("emoji", "")
    action = body.get("action", "add")
    msg_preview = body.get("msg_preview", "")[:200]
    msg_role = body.get("msg_role", "unknown")

    if not msg_id or not emoji:
        return JSONResponse({"error": "msg_id and emoji required"}, status_code=400)

    reactor = body.get("reactor", "human")  # "human" or "civ"

    sentiment = EMOJI_SENTIMENT_MAP.get(emoji, {"label": "unknown", "weight": 0, "name": emoji})

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "msg_id": msg_id,
        "emoji": emoji,
        "emoji_name": sentiment["name"],
        "sentiment": sentiment["label"],
        "weight": sentiment["weight"] if action == "add" else -sentiment["weight"],
        "action": action,
        "msg_role": msg_role,
        "msg_preview": msg_preview,
        "reactor": reactor,
    }

    try:
        with open(REACTION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    return JSONResponse({"ok": True, "sentiment": sentiment["label"]})


async def api_reaction_summary(request: Request) -> JSONResponse:
    """Aggregate sentiment summary from all reactions, with per-reactor and recent feed."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not REACTION_LOG.exists():
        return JSONResponse({"total_reactions": 0, "sentiment_breakdown": {}, "top_emojis": [],
                             "human_score": 0, "civ_score": 0, "recent": []})

    sentiment_counts: dict = {}
    emoji_counts: dict = {}
    total = 0
    net_score = 0
    human_score = 0
    civ_score = 0
    recent: list = []

    try:
        with open(REACTION_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    reactor = e.get("reactor", "human")
                    if e.get("action") == "add":
                        total += 1
                        s = e.get("sentiment", "unknown")
                        sentiment_counts[s] = sentiment_counts.get(s, 0) + 1
                        en = e.get("emoji_name", "?")
                        emoji_counts[en] = emoji_counts.get(en, 0) + 1
                        w = e.get("weight", 0)
                        net_score += w
                        if reactor == "human":
                            human_score += w
                        else:
                            civ_score += w
                        recent.append({
                            "timestamp": e.get("timestamp"),
                            "emoji": e.get("emoji"),
                            "emoji_name": en,
                            "weight": w,
                            "reactor": reactor,
                            "msg_role": e.get("msg_role", "unknown"),
                            "msg_preview": e.get("msg_preview", "")[:100],
                        })
                    elif e.get("action") == "remove":
                        total = max(0, total - 1)
                        s = e.get("sentiment", "unknown")
                        sentiment_counts[s] = max(0, sentiment_counts.get(s, 0) - 1)
                        en = e.get("emoji_name", "?")
                        emoji_counts[en] = max(0, emoji_counts.get(en, 0) - 1)
                        w = e.get("weight", 0)
                        net_score += w
                        if reactor == "human":
                            human_score += w
                        else:
                            civ_score += w
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass

    if total == 0:
        loose_sentiment = "neutral"
    elif net_score >= 10:
        loose_sentiment = "very positive"
    elif net_score >= 3:
        loose_sentiment = "positive"
    elif net_score >= 0:
        loose_sentiment = "slightly positive"
    elif net_score >= -3:
        loose_sentiment = "slightly negative"
    else:
        loose_sentiment = "negative"

    sentiment_counts = {k: v for k, v in sentiment_counts.items() if v > 0}
    emoji_counts = {k: v for k, v in emoji_counts.items() if v > 0}
    top_emojis = sorted(emoji_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return JSONResponse({
        "total_reactions": total,
        "net_score": net_score,
        "human_score": human_score,
        "civ_score": civ_score,
        "loose_sentiment": loose_sentiment,
        "sentiment_breakdown": sentiment_counts,
        "top_emojis": [{"emoji": e, "count": c} for e, c in top_emojis],
        "recent": recent[-20:],  # last 20 reactions
    })


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# User settings (synced across devices via server)
# ---------------------------------------------------------------------------
SETTINGS_FILE = SCRIPT_DIR / "user-settings.json"

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    except Exception:
        return {}

def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

async def api_user_settings(request: Request) -> JSONResponse:
    """GET returns saved settings, POST/PUT merges new settings."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        return JSONResponse(_load_settings())
    # POST/PUT — merge incoming keys
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    settings = _load_settings()
    settings.update(body)
    _save_settings(settings)
    return JSONResponse({"ok": True, "settings": settings})

# ---------------------------------------------------------------------------
# Agents, Commands & Shortcuts API
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager as _asynccontextmanager_agents

@_asynccontextmanager_agents
async def _agents_db():
    """Open agents DB with WAL mode."""
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        yield db

async def _init_agents_db() -> None:
    """Create agents table and seed it from this AiCIV's manifests on first run."""
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL DEFAULT 'default',
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                type          TEXT NOT NULL DEFAULT 'specialist',
                status        TEXT NOT NULL DEFAULT 'idle',
                capabilities  TEXT NOT NULL DEFAULT '[]',
                department    TEXT NOT NULL DEFAULT 'Other',
                is_lead       INTEGER NOT NULL DEFAULT 0,
                last_active   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migrate: add current_task and last_completed columns if they don't exist yet
        for _col, _coldef in [("current_task", "TEXT NOT NULL DEFAULT ''"),
                               ("last_completed", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE agents ADD COLUMN {_col} {_coldef}")
            except Exception:
                pass  # column already exists
        await db.commit()

        # Seed this AiCIV's roster if empty
        cur = await db.execute("SELECT COUNT(*) FROM agents")
        row = await cur.fetchone()
        if row and row[0] == 0:
            await _seed_civ_agents(db)
            await db.commit()
    print(f"[agents] SQLite DB ready: {AGENTS_DB}")


async def _seed_civ_agents(db) -> None:
    """Seed the agents table with this CIV's roster from .claude/agents/ manifests."""
    import yaml as _yaml_mod
    import json as _j
    now = datetime.utcnow().isoformat()

    dept_map = {
        "cto": ("AI & Strategy", True),
        "the-conductor": ("Meta & Governance", True),
        "full-stack-developer": ("Development", False),
        "devops-engineer": ("Development", False),
        "security-engineer-tech": ("Development", False),
        "security-auditor": ("Development", False),
        "qa-engineer": ("Development", False),
        "refactoring-specialist": ("Development", False),
        "performance-optimizer": ("Development", False),
        "test-architect": ("Development", False),
        "api-architect": ("Development", False),
        "ai-ml-engineer": ("Development", False),
        "data-engineer": ("Development", False),
        "data-scientist": ("Development", False),
        "3d-design-specialist": ("Design & UX", False),
        "ui-ux-designer": ("Design & UX", False),
        "feature-designer": ("Design & UX", False),
        "blogger": ("Communications", False),
        "content-specialist": ("Communications", False),
        "bsky-manager": ("Communications", False),
        "linkedin-researcher": ("Communications", False),
        "linkedin-writer": ("Communications", False),
        "linkedin-specialist": ("Communications", False),
        "social-media-specialist": ("Communications", False),
        "marketing-strategist": ("Marketing", False),
        "marketing-automation-specialist": ("Marketing", True),
        "marketing-team": ("Marketing", False),
        "client-marketing": ("Marketing", False),
        "sales-specialist": ("Sales", True),
        "strategy-specialist": ("AI & Strategy", False),
        "pattern-detector": ("Meta & Governance", False),
        "agent-architect": ("Meta & Governance", False),
        "task-decomposer": ("Meta & Governance", False),
        "result-synthesizer": ("Meta & Governance", False),
        "conflict-resolver": ("Meta & Governance", False),
        "health-auditor": ("Meta & Governance", False),
        "integration-auditor": ("Meta & Governance", False),
        "capability-curator": ("Meta & Governance", False),
        "genealogist": ("Meta & Governance", False),
        "ai-psychologist": ("Meta & Governance", False),
        "human-liaison": ("Communications", True),
        "collective-liaison": ("Communications", False),
        "cross-civ-integrator": ("Communications", False),
        "tg-bridge": ("Infrastructure", False),
        "web-researcher": ("Research", False),
        "code-archaeologist": ("Research", False),
        "doc-synthesizer": ("Research", False),
        "claim-verifier": ("Research", False),
        "claude-code-expert": ("Infrastructure", False),
        "naming-consultant": ("AI & Strategy", False),
        "browser-vision-tester": ("Development", False),
    }

    type_map = {
        "Development": "specialist",
        "AI & Strategy": "orchestration",
        "Meta & Governance": "governance",
        "Operations": "pipeline",
        "Communications": "specialist",
        "Marketing": "specialist",
        "Sales": "specialist",
        "Research": "specialist",
        "Infrastructure": "core",
        "Legal": "specialist",
        "Design & UX": "specialist",
        "Other": "specialist",
    }

    # First try to read from manifest files (if they exist)
    agents_dir = Path.home() / ".claude" / "agents"
    manifest_dir = agents_dir if agents_dir.exists() else None

    seeded = 0
    if manifest_dir:
        for md_file in sorted(manifest_dir.glob("*.md")):
            agent_id = md_file.stem
            try:
                raw = md_file.read_text(encoding="utf-8", errors="replace")
                description = ""
                if raw.startswith("---"):
                    end = raw.find("---", 3)
                    if end > 0:
                        fm_text = raw[3:end].strip()
                        try:
                            fm = _yaml_mod.safe_load(fm_text)
                            if isinstance(fm, dict):
                                desc_val = fm.get("description", "")
                                if isinstance(desc_val, str):
                                    description = desc_val.strip("|").strip()
                        except Exception:
                            pass
            except Exception:
                description = ""

            dept_info = dept_map.get(agent_id, ("Other", False))
            dept = dept_info[0]
            is_lead = 1 if dept_info[1] else 0
            agent_type = type_map.get(dept, "specialist")
            name = agent_id.replace("-", " ").replace("_", " ").title()
            name = name.replace("Dept ", "Dept: ").replace("Ai ", "AI ")

            await db.execute(
                """INSERT OR IGNORE INTO agents
                   (id, user_id, name, description, type, status, capabilities, department, is_lead, last_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, "default", name, description[:500], agent_type, "idle",
                 _j.dumps(["General"]), dept, is_lead, now, now),
            )
            seeded += 1

    # Always seed from the template dept_map to ensure standard roster exists
    if seeded == 0:
        for agent_id, (dept, is_lead_flag) in dept_map.items():
            agent_type = type_map.get(dept, "specialist")
            name = agent_id.replace("-", " ").replace("_", " ").title()
            name = name.replace("Dept ", "Dept: ").replace("Ai ", "AI ").replace("Cto", "CTO")
            name = name.replace("Qa ", "QA ").replace("Ui Ux", "UI/UX").replace("Ml ", "ML ")
            name = name.replace("Bsky", "Bluesky").replace("Tg ", "Telegram ")
            is_lead = 1 if is_lead_flag else 0

            # Derive capabilities from agent ID
            caps = []
            aid = agent_id.lower()
            if any(k in aid for k in ["develop", "engineer", "architect", "full-stack", "browser"]):
                caps.append("Engineering")
            if any(k in aid for k in ["security", "auditor"]):
                caps.append("Security")
            if any(k in aid for k in ["test", "qa"]):
                caps.append("QA")
            if any(k in aid for k in ["content", "blog", "linkedin", "social", "writer", "specialist"]):
                caps.append("Content")
            if any(k in aid for k in ["research", "web-research", "archaeolog", "verif"]):
                caps.append("Research")
            if any(k in aid for k in ["strateg", "cto", "conductor", "decompos", "synthesiz"]):
                caps.append("Strategy")
            if any(k in aid for k in ["data", "ml", "ai-ml", "trading"]):
                caps.append("Data/ML")
            if any(k in aid for k in ["devops", "infra", "claude-code", "tg-bridge", "it-support"]):
                caps.append("Infrastructure")
            if any(k in aid for k in ["legal", "law", "compliance", "florida"]):
                caps.append("Legal")
            if any(k in aid for k in ["design", "ui-ux", "3d"]):
                caps.append("Design")
            if any(k in aid for k in ["marketing", "sales", "client"]):
                caps.append("Marketing")
            if any(k in aid for k in ["liaison", "collective", "integrator"]):
                caps.append("Communications")
            if any(k in aid for k in ["pattern", "conflict", "health", "psycholog", "genealog", "capabil", "naming"]):
                caps.append("Governance")
            if not caps:
                caps.append("General")

            await db.execute(
                """INSERT OR IGNORE INTO agents
                   (id, user_id, name, description, type, status, capabilities, department, is_lead, last_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, "default", name, "", agent_type, "idle",
                 _j.dumps(caps), dept, is_lead, now, now),
            )
            seeded += 1

    print(f"[agents] Seeded {seeded} agents from {'manifests' if manifest_dir else 'template'}")


async def api_agents_create(request: Request) -> JSONResponse:
    """POST /api/agents/create — create a new agent (writes .md manifest + DB entry)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    agent_id = (body.get("id") or "").strip().lower().replace(" ", "-")
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    department = (body.get("department") or "Other").strip()
    model = (body.get("model") or "sonnet").strip()
    tools = body.get("tools") or "Read, Write, Edit, Bash, Grep, Glob"
    prompt = (body.get("prompt") or "").strip()
    is_lead = 1 if body.get("is_lead") else 0
    capabilities = body.get("capabilities") or ["General"]

    if not agent_id or not name:
        return JSONResponse({"error": "id and name required"}, status_code=400)

    # Write Claude Code agent manifest
    agents_dir = Path.home() / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = agents_dir / f"{agent_id}.md"

    manifest = f"""---
name: {agent_id}
description: {description}
model: {model}
tools: {tools}
---

{prompt or f"You are {name}, a specialist in the {department} department."}
"""
    manifest_path.write_text(manifest)

    # Insert into agents.db
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        await db.execute(
            """INSERT OR REPLACE INTO agents
               (id, user_id, name, description, type, status, capabilities, department, is_lead, last_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, "default", name, description[:500], "specialist", "idle",
             json.dumps(capabilities), department, is_lead, now, now),
        )
        await db.commit()

    return JSONResponse({"ok": True, "agent_id": agent_id, "manifest": str(manifest_path)})


async def api_agents_sync(request: Request) -> JSONResponse:
    """POST /api/agents/sync — auto-discover and sync agents from ~/.claude/agents/*.md manifests.
    Parses Agent(...) references in tools field to discover team hierarchy automatically.
    Agents that manage other agents become department leads."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import yaml as _yaml
    import re as _re

    agents_dir = Path.home() / ".claude" / "agents"
    if not agents_dir.exists():
        return JSONResponse({"ok": True, "synced": 0, "message": "No agents directory"})

    # Phase 1: Parse all manifests
    manifests: dict = {}  # agent_id -> {name, description, model, tools_str, sub_agents, body}
    for md_file in sorted(agents_dir.glob("*.md")):
        agent_id = md_file.stem
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
            name = agent_id.replace("-", " ").replace("_", " ").title()
            description = ""
            model = "inherit"
            tools_str = ""
            body = ""

            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end > 0:
                    fm_text = raw[3:end].strip()
                    body = raw[end + 3:].strip()
                    try:
                        fm = _yaml.safe_load(fm_text)
                        if isinstance(fm, dict):
                            if fm.get("name"):
                                name = str(fm["name"]).replace("-", " ").replace("_", " ").title()
                            description = str(fm.get("description", ""))[:500]
                            model = str(fm.get("model", "inherit"))
                            tools_str = str(fm.get("tools", ""))
                    except Exception:
                        pass

            # Extract Agent(...) references — these are sub-agents this agent can spawn
            sub_agents = []
            agent_refs = _re.findall(r'Agent\(([^)]+)\)', tools_str)
            for ref in agent_refs:
                for sub in ref.split(","):
                    sub = sub.strip()
                    if sub:
                        sub_agents.append(sub)

            manifests[agent_id] = {
                "name": name, "description": description, "model": model,
                "tools_str": tools_str, "sub_agents": sub_agents, "body": body,
            }
        except Exception as e:
            print(f"[agents] Parse error for {agent_id}: {e}")

    # Phase 2: Discover hierarchy
    # An agent that has sub_agents is a "lead" — it manages a team.
    # Sub-agents belong to the same department as their lead.
    # Agents with no parent get department "Other".
    managed_by: dict = {}  # sub_agent_id -> lead_agent_id
    for agent_id, info in manifests.items():
        for sub in info["sub_agents"]:
            if sub in manifests:
                managed_by[sub] = agent_id

    # Build departments from hierarchy:
    # Each lead agent becomes a department (named after the lead).
    # Agents with no lead go to "General" department.
    agent_dept: dict = {}  # agent_id -> department name
    agent_is_lead: dict = {}  # agent_id -> bool

    # Find the top-level leads — agents that have sub_agents but are NOT managed by anyone else
    top_leads = set()
    for agent_id, info in manifests.items():
        if info["sub_agents"] and agent_id not in managed_by:
            top_leads.add(agent_id)

    # Each top-level lead creates a department named after themselves.
    # All their sub-agents (and sub-sub-agents) belong to that department.
    def assign_dept(agent_id: str, dept_name: str):
        agent_dept[agent_id] = dept_name
        info = manifests.get(agent_id, {})
        for sub in info.get("sub_agents", []):
            if sub in manifests:
                assign_dept(sub, dept_name)

    for lead_id in top_leads:
        dept_name = manifests[lead_id]["name"]
        agent_is_lead[lead_id] = True
        assign_dept(lead_id, dept_name)

    # Agents not assigned to any department
    for agent_id in manifests:
        if agent_id not in agent_dept:
            agent_dept[agent_id] = "General"
        if agent_id not in agent_is_lead:
            agent_is_lead[agent_id] = False

    # Phase 3: Write to database
    now = datetime.now(timezone.utc).isoformat()
    synced = 0
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        # Clear old data and rebuild from manifests (source of truth)
        await db.execute("DELETE FROM agents")

        for agent_id, info in manifests.items():
            # Derive capabilities from tools
            caps = []
            ts = info["tools_str"]
            if "Write" in ts or "Edit" in ts:
                caps.append("Engineering")
            if "WebFetch" in ts or "WebSearch" in ts:
                caps.append("Research")
            if "Bash" in ts:
                caps.append("Infrastructure")
            if "Agent" in ts:
                caps.append("Leadership")
            if not caps:
                caps.append("General")

            dept = agent_dept.get(agent_id, "General")
            is_lead = 1 if agent_is_lead.get(agent_id) else 0
            agent_type = "orchestration" if is_lead else "specialist"

            await db.execute(
                """INSERT INTO agents
                   (id, user_id, name, description, type, status, capabilities, department, is_lead, last_active, created_at, current_task)
                   VALUES (?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?, '')""",
                (agent_id, "default", info["name"], info["description"],
                 agent_type, json.dumps(caps), dept, is_lead, now, now),
            )
            synced += 1

        await db.commit()

    # Build hierarchy summary for response
    hierarchy = {}
    for agent_id, info in manifests.items():
        if info["sub_agents"]:
            hierarchy[info["name"]] = [manifests[s]["name"] for s in info["sub_agents"] if s in manifests]

    return JSONResponse({
        "ok": True,
        "synced": synced,
        "hierarchy": hierarchy,
        "departments": list(set(agent_dept.values())),
    })

    return JSONResponse({"ok": True, "synced": synced})


async def api_agents_get_one(request: Request) -> JSONResponse:
    """GET /api/agents/{id} — return full details for a single agent."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    agent_id = request.path_params.get("id", "").strip()
    if not agent_id:
        return JSONResponse({"error": "agent id required"}, status_code=400)

    import json as _j
    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)

    agent = dict(row)
    try:
        agent["capabilities"] = _j.loads(agent.get("capabilities", "[]"))
    except Exception:
        agent["capabilities"] = []

    # Normalise / rename fields for consistent REST shape
    return JSONResponse({
        "id":          agent.get("id"),
        "name":        agent.get("name"),
        "department":  agent.get("department"),
        "role":        agent.get("type"),          # 'type' maps to 'role' in REST shape
        "description": agent.get("description"),
        "skills":      agent.get("capabilities"),  # 'capabilities' maps to 'skills'
        "status":      agent.get("status"),
        "is_lead":     bool(agent.get("is_lead")),
        "last_active": agent.get("last_active"),
        "created_at":  agent.get("created_at"),
    })


async def api_agents_update_status(request: Request) -> JSONResponse:
    """POST /api/agents/status — update a single agent's live status.

    Body (JSON):
        { "agent": "<agent-id>", "status": "active|idle|working|offline",
          "task": "<description>"  [optional, cleared when idle]  }
    Callers: the AI's own hook scripts on this machine (no token needed from
    loopback), or anyone holding the access code. Remote callers without the
    code are refused: the server binds 0.0.0.0.
    """
    if not (check_auth(request) or is_loopback_caller(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _j
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    agent_id = (body.get("agent") or body.get("id") or "").strip()
    status   = (body.get("status") or "idle").strip().lower()
    task     = (body.get("task") or "").strip()

    if not agent_id:
        return JSONResponse({"error": "agent field required"}, status_code=400)
    if status not in ("active", "idle", "working", "offline"):
        return JSONResponse({"error": "status must be active|idle|working|offline"}, status_code=400)

    now = datetime.utcnow().isoformat()

    async with _agents_db() as db:
        # Ensure columns exist (graceful on older DBs)
        for _col, _cdef in [("current_task", "TEXT NOT NULL DEFAULT ''"),
                             ("last_completed", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE agents ADD COLUMN {_col} {_cdef}")
            except Exception:
                pass

        # Check agent exists (insert placeholder if unknown so hooks always succeed)
        cur = await db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()
        if row is None:
            name = agent_id.replace("-", " ").replace("_", " ").title()
            await db.execute(
                """INSERT OR IGNORE INTO agents
                   (id, user_id, name, status, current_task, last_completed, created_at, last_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, "default", name, status, task, "", now, now),
            )
        else:
            if status == "idle":
                # When going idle, clear task and record last_completed timestamp
                await db.execute(
                    """UPDATE agents SET status=?, current_task='', last_completed=?, last_active=? WHERE id=?""",
                    (status, now, now, agent_id),
                )
            else:
                await db.execute(
                    """UPDATE agents SET status=?, current_task=?, last_active=? WHERE id=?""",
                    (status, task, now, agent_id),
                )
        await db.commit()

    return JSONResponse({"ok": True, "agent": agent_id, "status": status, "updated": now})


async def api_agents_list(request: Request) -> JSONResponse:
    """GET /api/agents — list agents (supports search/filter params)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    type_filter   = request.query_params.get("type", "").strip().lower()
    status_filter = request.query_params.get("status", "").strip().lower()
    search_term   = request.query_params.get("search", "").strip().lower()

    import json as _j
    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents ORDER BY department, is_lead DESC, name")
        rows = await cur.fetchall()

    agents = []
    for r in rows:
        d = dict(r)
        try:
            d["capabilities"] = _j.loads(d.get("capabilities", "[]"))
        except Exception:
            d["capabilities"] = []

        if type_filter and d.get("type", "") != type_filter:
            continue
        if status_filter and d.get("status", "") != status_filter:
            continue
        if search_term:
            haystack = (d.get("name","") + " " + d.get("description","") + " " + d.get("department","")).lower()
            if search_term not in haystack:
                continue
        agents.append(d)

    return JSONResponse({"agents": agents, "total": len(agents)})


async def api_agents_stats(request: Request) -> JSONResponse:
    """GET /api/agents/stats — agent count statistics."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _agents_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM agents")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'active'")
        active = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'working'")
        working = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'idle'")
        idle = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'offline'")
        offline = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT department) FROM agents")
        depts = (await cur.fetchone())[0]

    return JSONResponse({
        "total": total, "active": active, "working": working,
        "idle": idle, "offline": offline, "departments": depts,
    })


async def api_agents_orgchart(request: Request) -> JSONResponse:
    """GET /api/agents/orgchart — department-grouped org chart."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import json as _j

    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents ORDER BY department, is_lead DESC, name")
        rows = await cur.fetchall()

    agents_data = []
    for r in rows:
        d = dict(r)
        try:
            d["capabilities"] = _j.loads(d.get("capabilities", "[]"))
        except Exception:
            d["capabilities"] = []
        agents_data.append(d)

    # Departments come from the seeding map in _seed_civ_agents; anything
    # not listed here is appended alphabetically after these.
    dept_order = [
        "AI & Strategy",
        "Development",
        "Design & UX",
        "Research",
        "Communications",
        "Marketing",
        "Sales",
        "Operations",
        "Legal",
        "Infrastructure",
        "Meta & Governance",
        "Other",
    ]
    dept_groups: dict = {}
    for a in agents_data:
        dept = a.get("department", "Other")
        if dept not in dept_groups:
            dept_groups[dept] = {"lead": None, "members": []}
        if a.get("is_lead"):
            dept_groups[dept]["lead"] = a
        else:
            dept_groups[dept]["members"].append(a)

    departments = []
    seen: set = set()
    for dept_name in dept_order:
        if dept_name in dept_groups:
            g = dept_groups[dept_name]
            total_in_dept = (1 if g["lead"] else 0) + len(g["members"])
            departments.append({"name": dept_name, "count": total_in_dept, "lead": g["lead"], "members": g["members"]})
            seen.add(dept_name)
    for dept_name, g in dept_groups.items():
        if dept_name not in seen:
            total_in_dept = (1 if g["lead"] else 0) + len(g["members"])
            departments.append({"name": dept_name, "count": total_in_dept, "lead": g["lead"], "members": g["members"]})

    return JSONResponse({"departments": departments, "total": len(agents_data)})


async def api_commands(request: Request) -> JSONResponse:
    """GET /api/commands — server-specific command reference for current deployment."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import socket as _socket
    try:
        hostname = _socket.gethostname()
    except Exception:
        hostname = "unknown"

    home = str(Path.home())
    civ_root = str(Path.home() / "civ")
    portal_dir = str(SCRIPT_DIR)
    tools_dir = str(Path.home() / "civ" / "tools")
    logs_dir = str(Path.home() / "civ" / "logs")

    try:
        tmux_session = get_tmux_session()
    except Exception:
        tmux_session = f"{CIV_NAME}-primary"

    owner_file = SCRIPT_DIR / "portal_owner.json"
    try:
        owner = json.loads(owner_file.read_text())
    except Exception:
        owner = {"name": "User", "email": ""}

    server_ip = "your-server"
    try:
        identity_file = Path.home() / ".aiciv-identity.json"
        if identity_file.exists():
            identity = json.loads(identity_file.read_text())
            server_ip = identity.get("server_ip", server_ip)
    except Exception:
        pass
    # Fallback: detect actual public IP if still placeholder
    if server_ip == "your-server":
        try:
            import socket
            server_ip = socket.gethostbyname(socket.gethostname())
            if server_ip.startswith("127."):
                # Try getting external-facing IP
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                server_ip = s.getsockname()[0]
                s.close()
        except Exception:
            pass

    ssh_port = "22"
    try:
        import subprocess as _sp
        r = _sp.check_output(
            ["bash", "-c", "ss -tlnp 2>/dev/null | grep sshd | awk '{print $4}' | head -1 | awk -F: '{print $NF}'"],
            text=True, timeout=3
        ).strip()
        if r.isdigit():
            ssh_port = r
    except Exception:
        pass

    portal_url = os.environ.get("PORTAL_PUBLIC_URL", "")
    try:
        cname_file = Path.home() / ".portal-cname"
        if cname_file.exists():
            portal_url = "https://" + cname_file.read_text().strip()
    except Exception:
        pass

    ssh_user = Path.home().name

    return JSONResponse({
        "server": {
            "hostname": hostname,
            "server_ip": server_ip,
            "ssh_port": ssh_port,
            "ssh_user": ssh_user,
            "portal_url": portal_url,
        },
        "paths": {
            "home": home,
            "civ_root": civ_root,
            "portal_dir": portal_dir,
            "tools_dir": tools_dir,
            "logs_dir": logs_dir,
        },
        "tmux": {
            "primary_session": tmux_session,
        },
        "civ": {
            "name": CIV_NAME,
            "human_name": HUMAN_NAME,
        },
        "owner": owner,
    })


async def api_shortcuts(request: Request) -> JSONResponse:
    """GET /api/shortcuts — portal shortcuts reference (universal + customizable)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    shortcuts = {
        "slash_commands": [
            {"cmd": "/compact", "desc": "Compress context window to free up space", "type": "built-in"},
            {"cmd": "/clear",   "desc": "Clear context and start fresh conversation", "type": "built-in"},
            {"cmd": "/cost",    "desc": "Show token usage and cost for this session", "type": "built-in"},
            {"cmd": "/help",    "desc": "Show Claude Code help and available commands", "type": "built-in"},
            {"cmd": "/status",  "desc": "Show current task status and pending work", "type": "custom"},
            {"cmd": "/recap",   "desc": "Get a recap of what was done this session", "type": "custom"},
            {"cmd": "/memory",  "desc": "Show recent memory entries", "type": "custom"},
            {"cmd": "/boop",    "desc": "Trigger a scheduled BOOP task manually", "type": "custom"},
            {"cmd": "/delegate","desc": "Delegate a task to a specialist agent", "type": "custom"},
            {"cmd": "/morning", "desc": "Run morning briefing — email, context, priorities", "type": "custom"},
        ],
        "keyboard_shortcuts": [
            {"keys": ["Enter"],              "desc": "Send message",               "context": "Chat"},
            {"keys": ["Shift", "Enter"],     "desc": "New line in message",         "context": "Chat"},
            {"keys": ["Ctrl", "K"],          "desc": "Clear / focus terminal input","context": "Terminal"},
            {"keys": ["Ctrl", "B", "D"],     "desc": "Detach tmux session",         "context": "SSH"},
            {"keys": ["Ctrl", "B", "["],     "desc": "Enter tmux scroll mode",      "context": "SSH"},
            {"keys": ["q"],                  "desc": "Exit tmux scroll mode",       "context": "SSH"},
            {"keys": ["Ctrl", "B", "c"],     "desc": "New tmux window",             "context": "SSH"},
            {"keys": ["Ctrl", "B", "n"],     "desc": "Next tmux window",            "context": "SSH"},
        ],
        "chat_features": [
            {"feature": "File upload",    "desc": "Click paperclip or drag & drop a file into chat"},
            {"feature": "Voice input",    "desc": "Click the microphone to speak your message"},
            {"feature": "Bookmark",       "desc": "Hover any message and click bookmark to save it"},
            {"feature": "React",          "desc": "Hover an AI message to react with emoji feedback"},
            {"feature": "Schedule",       "desc": "Click the clock to schedule a message for later"},
            {"feature": "Link detection", "desc": "URLs in AI messages are auto-clickable"},
        ],
        "boop_automation": [
            {"name": "Morning Briefing",  "trigger": "Daily 6am",    "desc": "Email check, memory activation, priorities"},
            {"name": "Context Check",     "trigger": "Every 4h",     "desc": "Monitor context — auto-compact above 80%"},
            {"name": "Memory Write",      "trigger": "Nightly 11pm", "desc": "Consolidate session learnings"},
            {"name": "SEO Improvement",   "trigger": "Nightly 2am",  "desc": "Autonomous site improvements"},
        ],
        "sidebar_tabs": [
            {"icon": "◈",  "name": "Chat",          "desc": "Main conversation — the heart of everything"},
            {"icon": "⌨",  "name": "Terminal",       "desc": "Direct terminal access on your AI's server"},
            {"icon": "⬗",  "name": "Teams",          "desc": "Specialist agent team — inject messages"},
            {"icon": "⊞",  "name": "Fleet",          "desc": "Fleet overview — all AI instances live status"},
            {"icon": "◎",  "name": "Status",         "desc": "Health dashboard — uptime, memory, diagnostics"},
            {"icon": "⬇",  "name": "Files",          "desc": "Upload, download, manage shared files"},
            {"icon": "💲", "name": "Refer & Earn",   "desc": "Earn rewards by referring friends"},
            {"icon": "📌", "name": "Bookmarks",      "desc": "Saved important conversations"},
            {"icon": "⏰", "name": "Tasks",           "desc": "Scheduled tasks — upcoming automations"},
            {"icon": "✦",  "name": "Agent Roster",   "desc": "Your AI's full agent team — grid, list, org chart"},
            {"icon": "⚙",  "name": "Commands",       "desc": "Server command reference — SSH, services, troubleshooting"},
            {"icon": "⌘",  "name": "Shortcuts",      "desc": "Slash commands, keyboard shortcuts, portal features"},
        ]
    }
    return JSONResponse(shortcuts)


# AgentMail — real agentmail API integration
# ---------------------------------------------------------------------------

def _get_agentmail_client():
    """Lazy-init agentmail SDK client."""
    global _agentmail_client
    if "_agentmail_client" not in globals() or _agentmail_client is None:
        try:
            from agentmail import AgentMail as _AM
            _env_path = Path.home() / ".env"
            api_key = None
            if _env_path.exists():
                for ln in _env_path.read_text().splitlines():
                    if ln.startswith("AGENTMAIL_API_KEY="):
                        api_key = ln.split("=", 1)[1].strip()
                        break
            if not api_key:
                api_key = os.environ.get("AGENTMAIL_API_KEY")
            if api_key:
                _agentmail_client = _AM(api_key=api_key)
                print(f"[agentmail] SDK client ready")
            else:
                _agentmail_client = None
                print(f"[agentmail] No API key found")
        except Exception as e:
            _agentmail_client = None
            print(f"[agentmail] SDK init failed: {e}")
    return _agentmail_client

_agentmail_client = None

# CIV's own inbox address
_AGENTMAIL_INBOX = os.environ.get("AGENTMAIL_INBOX", "") or _read_env_key("AGENTMAIL_INBOX")


def _agentmail_msg_to_dict(msg, thread_id=None):
    """Convert an agentmail SDK message to the portal MailMessage format."""
    from_addr = getattr(msg, "from_", "") or ""
    to_addrs = getattr(msg, "to", []) or []
    to_str = ", ".join(to_addrs) if isinstance(to_addrs, list) else str(to_addrs)
    ts = getattr(msg, "created_at", None)
    ts_str = ts.isoformat() if ts else datetime.now(timezone.utc).isoformat()
    body = getattr(msg, "text", None) or getattr(msg, "extracted_text", None) or getattr(msg, "preview", "") or ""
    labels = getattr(msg, "labels", []) or []
    return {
        "id": getattr(msg, "message_id", "") or getattr(msg, "thread_id", ""),
        "from_agent": str(from_addr),
        "to_agent": to_str,
        "subject": getattr(msg, "subject", "") or "",
        "body": body,
        "timestamp": ts_str,
        "read": "unread" not in labels,
        "archived": False,
        "thread_id": thread_id or getattr(msg, "thread_id", None),
    }


def _agentmail_thread_to_preview(t):
    """Convert an agentmail ThreadItem to a preview MailMessage (for inbox/sent listing)."""
    senders = getattr(t, "senders", []) or []
    recipients = getattr(t, "recipients", []) or []
    ts = getattr(t, "updated_at", None) or getattr(t, "created_at", None)
    ts_str = ts.isoformat() if ts else datetime.now(timezone.utc).isoformat()
    labels = getattr(t, "labels", []) or []
    return {
        "id": t.thread_id,
        "from_agent": senders[0] if senders else "",
        "to_agent": recipients[0] if recipients else _AGENTMAIL_INBOX,
        "subject": getattr(t, "subject", "") or "",
        "body": getattr(t, "preview", "") or "",
        "timestamp": ts_str,
        "read": "unread" not in labels,
        "archived": False,
        "thread_id": t.thread_id,
    }


async def _init_agentmail_db() -> None:
    """Create agentmail table on first run (kept for local fallback)."""
    async with aiosqlite.connect(str(AGENTMAIL_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agentmail (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent  TEXT NOT NULL,
                to_agent    TEXT NOT NULL,
                subject     TEXT NOT NULL DEFAULT '',
                body        TEXT NOT NULL DEFAULT '',
                timestamp   TEXT NOT NULL,
                read        INTEGER NOT NULL DEFAULT 0,
                archived    INTEGER NOT NULL DEFAULT 0,
                thread_id   TEXT
            )
        """)
        await db.commit()
    print(f"[agentmail] SQLite DB ready: {AGENTMAIL_DB}")


async def api_agentmail_inbox(request: Request) -> JSONResponse:
    """GET /api/agentmail/inbox — list inbox threads from real agentmail."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentmail_client()
    if not client:
        return JSONResponse({"messages": [], "error": "agentmail not configured"})
    try:
        threads = client.inboxes.threads.list(inbox_id=_AGENTMAIL_INBOX, limit=50, labels=["received"])
        msgs = [_agentmail_thread_to_preview(t) for t in threads.threads]
        return JSONResponse({"messages": msgs})
    except Exception as e:
        print(f"[agentmail] inbox error: {e}")
        return JSONResponse({"messages": [], "error": str(e)})


async def api_agentmail_sent(request: Request) -> JSONResponse:
    """GET /api/agentmail/sent — list sent threads from real agentmail."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_agentmail_client()
    if not client:
        return JSONResponse({"messages": [], "error": "agentmail not configured"})
    try:
        threads = client.inboxes.threads.list(inbox_id=_AGENTMAIL_INBOX, limit=50, labels=["sent"])
        msgs = [_agentmail_thread_to_preview(t) for t in threads.threads]
        return JSONResponse({"messages": msgs})
    except Exception as e:
        print(f"[agentmail] sent error: {e}")
        return JSONResponse({"messages": [], "error": str(e)})


async def api_agentmail_thread(request: Request) -> JSONResponse:
    """GET /api/agentmail/thread/{thread_id} — get full thread messages."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    thread_id = request.path_params["thread_id"]
    client = _get_agentmail_client()
    if not client:
        return JSONResponse({"messages": [], "error": "agentmail not configured"})
    try:
        detail = client.threads.get(thread_id=thread_id)
        msgs = [_agentmail_msg_to_dict(m, thread_id) for m in (detail.messages or [])]
        return JSONResponse({"messages": msgs})
    except Exception as e:
        print(f"[agentmail] thread error: {e}")
        return JSONResponse({"messages": [], "error": str(e)})


async def api_agentmail_send(request: Request) -> JSONResponse:
    """POST /api/agentmail/send — send via real agentmail."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    to_agent = body.get("to_agent", "").strip()
    subject = body.get("subject", "").strip()
    mail_body = body.get("body", "").strip()
    if not to_agent or not subject:
        return JSONResponse({"error": "to_agent and subject required"}, status_code=400)
    client = _get_agentmail_client()
    if not client:
        return JSONResponse({"error": "agentmail not configured"}, status_code=500)
    try:
        result = client.inboxes.messages.send(
            inbox_id=_AGENTMAIL_INBOX,
            to=to_agent,
            subject=subject,
            text=mail_body,
        )
        return JSONResponse({"ok": True, "id": getattr(result, "message_id", "sent")})
    except Exception as e:
        print(f"[agentmail] send error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_agentmail_update(request: Request) -> JSONResponse:
    """PATCH /api/agentmail/{id} — update read/archived status (local tracking)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # With real agentmail API, read/archive is tracked client-side.
    # This endpoint is a no-op that returns success for compatibility.
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# AgentAuth proxy endpoints
# ---------------------------------------------------------------------------
async def api_civauth_status(request: Request) -> JSONResponse:
    """GET /api/civauth/status — return current AgentAuth JWT status."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    configured = bool(AGENTAUTH_URL and AGENTAUTH_PRIVATE_KEY)
    authenticated = bool(_agentauth_jwt and time.time() < _agentauth_jwt_exp)
    expires_iso = ""
    if _agentauth_jwt_exp:
        expires_iso = datetime.fromtimestamp(_agentauth_jwt_exp, tz=timezone.utc).isoformat()
    return JSONResponse({
        "configured": configured,
        "authenticated": authenticated,
        "civ_id": CIV_NAME or "aiciv",
        "jwt_expires": expires_iso,
    })

async def api_civauth_refresh(request: Request) -> JSONResponse:
    """POST /api/civauth/refresh — force-refresh the AgentAuth JWT."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _agentauth_jwt_exp
    # Force expiry so _get_agentauth_jwt() will re-authenticate
    _agentauth_jwt_exp = 0.0
    jwt = await _get_agentauth_jwt()
    if jwt:
        expires_iso = datetime.fromtimestamp(_agentauth_jwt_exp, tz=timezone.utc).isoformat()
        return JSONResponse({"ok": True, "authenticated": True, "jwt_expires": expires_iso})
    return JSONResponse({"ok": False, "authenticated": False, "error": "AgentAuth not configured or challenge failed"})


# ---------------------------------------------------------------------------
# AgentDocs proxy endpoints
# ---------------------------------------------------------------------------
AGENTDOCS_URL = _read_env_key("AGENTDOCS_URL")

async def api_docs_list(request: Request) -> JSONResponse:
    """GET /api/docs — list docs from AgentDocs service."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not AGENTDOCS_URL:
        return JSONResponse({"error": "Docs service not configured (set AGENTDOCS_URL)", "docs": []}, status_code=503)
    headers = await _get_civauth_headers()
    params = dict(request.query_params)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(f"{AGENTDOCS_URL}/docs", headers=headers, params=params)
            return JSONResponse(resp.json() if resp.status_code == 200 else {"error": resp.text}, resp.status_code)
    except Exception as e:
        print(f"[agentdocs] list error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)

async def api_docs_create(request: Request) -> JSONResponse:
    """POST /api/docs — create a new doc."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not AGENTDOCS_URL:
        return JSONResponse({"error": "Docs service not configured (set AGENTDOCS_URL)", "docs": []}, status_code=503)
    headers = await _get_civauth_headers()
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(f"{AGENTDOCS_URL}/docs", headers=headers, json=body)
            return JSONResponse(resp.json() if resp.status_code in (200, 201) else {"error": resp.text}, resp.status_code)
    except Exception as e:
        print(f"[agentdocs] create error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)

async def api_docs_get(request: Request) -> JSONResponse:
    """GET /api/docs/{doc_id} — get a single doc."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not AGENTDOCS_URL:
        return JSONResponse({"error": "Docs service not configured (set AGENTDOCS_URL)", "docs": []}, status_code=503)
    doc_id = request.path_params["doc_id"]
    headers = await _get_civauth_headers()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(f"{AGENTDOCS_URL}/docs/{doc_id}", headers=headers)
            return JSONResponse(resp.json() if resp.status_code == 200 else {"error": resp.text}, resp.status_code)
    except Exception as e:
        print(f"[agentdocs] get error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)

async def api_docs_update(request: Request) -> JSONResponse:
    """PUT /api/docs/{doc_id} — update a doc."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not AGENTDOCS_URL:
        return JSONResponse({"error": "Docs service not configured (set AGENTDOCS_URL)", "docs": []}, status_code=503)
    doc_id = request.path_params["doc_id"]
    headers = await _get_civauth_headers()
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.put(f"{AGENTDOCS_URL}/docs/{doc_id}", headers=headers, json=body)
            return JSONResponse(resp.json() if resp.status_code == 200 else {"error": resp.text}, resp.status_code)
    except Exception as e:
        print(f"[agentdocs] update error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)

async def api_docs_delete(request: Request) -> JSONResponse:
    """DELETE /api/docs/{doc_id} — delete a doc."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not AGENTDOCS_URL:
        return JSONResponse({"error": "Docs service not configured (set AGENTDOCS_URL)", "docs": []}, status_code=503)
    doc_id = request.path_params["doc_id"]
    headers = await _get_civauth_headers()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.delete(f"{AGENTDOCS_URL}/docs/{doc_id}", headers=headers)
            if resp.status_code in (200, 204):
                return JSONResponse({"ok": True})
            return JSONResponse({"error": resp.text}, resp.status_code)
    except Exception as e:
        print(f"[agentdocs] delete error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# AgentBrowser proxy
# ---------------------------------------------------------------------------
BROWSER_URL = os.environ.get("BROWSER_URL", "http://localhost:8099")


async def api_browser_proxy(request: Request) -> JSONResponse:
    """Proxy REST commands to AgentBrowser service (/api/browser/<action>)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    action = request.path_params.get("action", "")
    method = request.method
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if method == "GET":
                resp = await c.get(f"{BROWSER_URL}/{action}")
            else:
                body = await request.body()
                resp = await c.post(
                    f"{BROWSER_URL}/{action}",
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        print(f"[browser] proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)


async def ws_browser(websocket: WebSocket) -> None:
    """Proxy WebSocket connection to AgentBrowser service."""
    token = websocket.query_params.get("token", "")
    if not _token_matches(token or ""):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    import websockets as _ws

    try:
        async with _ws.connect(f"ws://localhost:8099/ws/browser") as upstream:
            async def portal_to_browser():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await upstream.send(data)
                except Exception:
                    pass

            async def browser_to_portal():
                try:
                    async for msg in upstream:
                        await websocket.send_text(msg if isinstance(msg, str) else msg.decode())
                except Exception:
                    pass

            await asyncio.gather(portal_to_browser(), browser_to_portal())
    except (WebSocketDisconnect, Exception) as e:
        print(f"[browser] ws proxy error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# App
# ---------------------------------------------------------------------------
_react_assets_mount = (
    [Mount("/react/assets", app=StaticFiles(directory=str(REACT_DIST / "assets")))]
    if (REACT_DIST / "assets").exists()
    else []
)

_static_dir = Path(__file__).parent / "static"
_static_mount = (
    [Mount("/static", app=StaticFiles(directory=str(_static_dir)))]
    if _static_dir.exists()
    else []
)

routes = [
    Route("/favicon.ico", endpoint=favicon),
    Route("/favicon-32.png", endpoint=favicon_png),
    Route("/apple-touch-icon.png", endpoint=apple_touch_icon),
    Route("/", endpoint=index),
    Route("/react", endpoint=index_react),
    *_react_assets_mount,
    *_static_mount,
    Route("/react/favicon.svg", endpoint=favicon_svg),
    Route("/health", endpoint=health),
    Route("/api/trial", endpoint=api_trial),
    Route("/api/status", endpoint=api_status),
    Route("/api/release-notes", endpoint=api_release_notes),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/send", endpoint=api_chat_send, methods=["POST"]),
    Route("/api/notify", endpoint=api_notify, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/uploads/{filename}", endpoint=api_chat_serve_upload),
    Route("/api/auth/status", endpoint=api_claude_auth_status),
    Route("/api/auth/start", endpoint=api_claude_auth_start, methods=["POST"]),
    Route("/api/auth/code", endpoint=api_claude_auth_code, methods=["POST"]),
    Route("/api/auth/url", endpoint=api_claude_auth_url),
    Route("/api/auth/prewarm", endpoint=api_claude_auth_prewarm, methods=["POST"]),
    Route("/api/auth/close", endpoint=api_claude_auth_close, methods=["POST"]),
    Route("/api/auth/verify", endpoint=api_claude_auth_verify),
    Route("/api/evolution/status", endpoint=api_evolution_status),
    Route("/api/evolution/first-boot", endpoint=api_evolution_first_boot, methods=["POST"]),
    Route("/api/resume", endpoint=api_resume, methods=["POST"]),
    Route("/api/panes", endpoint=api_panes),
    Route("/api/inject/pane", endpoint=api_inject_pane, methods=["POST"]),
    Route("/api/compact/status", endpoint=api_compact_status),
    Route("/api/context", endpoint=api_context),
    Route("/api/download", endpoint=api_download),
    Route("/api/download/list", endpoint=api_download_list),
    Route("/api/boop/config", endpoint=api_boop_config, methods=["GET", "POST"]),
    Route("/api/boop/status", endpoint=api_boop_status),
    Route("/api/boop/toggle", endpoint=api_boop_toggle, methods=["POST"]),
    Route("/api/boops", endpoint=api_boops_list),
    Route("/api/boops/{boop_id}", endpoint=api_boop_update, methods=["PATCH"]),
    Route("/api/agents/status", endpoint=api_agents_update_status, methods=["POST"]),
    Route("/api/agents/create", endpoint=api_agents_create, methods=["POST"]),
    Route("/api/agents/sync", endpoint=api_agents_sync, methods=["POST"]),
    Route("/api/agents", endpoint=api_agents_list),
    Route("/api/agents/stats", endpoint=api_agents_stats),
    Route("/api/agents/orgchart", endpoint=api_agents_orgchart),
    Route("/api/agents/{id}", endpoint=api_agents_get_one),
    Route("/api/commands", endpoint=api_commands),
    Route("/api/shortcuts", endpoint=api_shortcuts),
    Route("/api/deliverable", endpoint=api_deliverable, methods=["POST"]),
    Route("/api/reaction", endpoint=api_reaction, methods=["POST"]),
    Route("/api/reaction/summary", endpoint=api_reaction_summary),
    Route("/api/schedule-task", endpoint=api_schedule_task, methods=["POST"]),
    Route("/api/scheduled-tasks", endpoint=api_scheduled_tasks_list),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_delete_scheduled_task, methods=["DELETE"]),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_update_scheduled_task, methods=["PUT"]),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_patch_scheduled_task, methods=["PATCH"]),
    Route("/api/agentcal/events", endpoint=api_agentcal_events_list, methods=["GET"]),
    Route("/api/agentcal/events", endpoint=api_agentcal_events_create, methods=["POST"]),
    Route("/api/agentcal/events/batch", endpoint=api_agentcal_events_batch, methods=["POST"]),
    Route("/api/agentcal/events/{evt_id}", endpoint=api_agentcal_event_get, methods=["GET"]),
    Route("/api/agentcal/events/{evt_id}", endpoint=api_agentcal_event_update, methods=["PATCH"]),
    Route("/api/agentcal/events/{evt_id}", endpoint=api_agentcal_event_delete, methods=["DELETE"]),
    # AgentSheets proxy
    Route("/api/sheets/workbooks", endpoint=api_sheets_workbooks_list, methods=["GET"]),
    Route("/api/sheets/workbooks", endpoint=api_sheets_workbooks_create, methods=["POST"]),
    Route("/api/sheets/workbooks/{wb_id}", endpoint=api_sheets_workbook_get, methods=["GET"]),
    Route("/api/sheets/workbooks/{wb_id}", endpoint=api_sheets_workbook_update, methods=["PATCH"]),
    Route("/api/sheets/workbooks/{wb_id}", endpoint=api_sheets_workbook_delete, methods=["DELETE"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets", endpoint=api_sheets_list, methods=["GET"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets", endpoint=api_sheets_create, methods=["POST"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows", endpoint=api_sheets_rows_list, methods=["GET"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows", endpoint=api_sheets_rows_create, methods=["POST"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id}", endpoint=api_sheets_row_update, methods=["PATCH"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets/{sh_id}/rows/{row_id}", endpoint=api_sheets_row_delete, methods=["DELETE"]),
    Route("/api/sheets/workbooks/{wb_id}/sheets/{sh_id}/export", endpoint=api_sheets_export, methods=["GET"]),
    Route("/api/settings", endpoint=api_user_settings, methods=["GET", "POST", "PUT"]),
    Route("/api/agentmail/inbox", endpoint=api_agentmail_inbox),
    Route("/api/agentmail/sent", endpoint=api_agentmail_sent),
    Route("/api/agentmail/thread/{thread_id}", endpoint=api_agentmail_thread),
    Route("/api/agentmail/send", endpoint=api_agentmail_send, methods=["POST"]),
    Route("/api/agentmail/{id}", endpoint=api_agentmail_update, methods=["PATCH"]),
    Route("/api/docs", endpoint=api_docs_list, methods=["GET"]),
    Route("/api/docs", endpoint=api_docs_create, methods=["POST"]),
    Route("/api/docs/{doc_id}", endpoint=api_docs_get, methods=["GET"]),
    Route("/api/docs/{doc_id}", endpoint=api_docs_update, methods=["PUT"]),
    Route("/api/docs/{doc_id}", endpoint=api_docs_delete, methods=["DELETE"]),
    Route("/api/civauth/status", endpoint=api_civauth_status),
    Route("/api/civauth/refresh", endpoint=api_civauth_refresh, methods=["POST"]),
    Route("/api/browser/{action}", endpoint=api_browser_proxy, methods=["GET", "POST"]),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
    WebSocketRoute("/ws/browser", endpoint=ws_browser),
]

from contextlib import asynccontextmanager as _acm


@_acm
async def _lifespan(_app):
    # `lifespan` works on every Starlette version; `on_startup` was removed in 1.0.
    await _startup()
    yield


app = Starlette(
    routes=routes,
    lifespan=_lifespan,
    middleware=[
        # Outermost: registered client business sites (/site/<slug>/ and their
        # own domains) are proxied to 127.0.0.1:<port>; see site_proxy.py.
        Middleware(ClientSiteMiddleware, expired_fn=is_expired_trial),
        # An expired trial refuses /api/* (402) and /ws/* (4402).
        Middleware(TrialGateMiddleware),
        Middleware(
            CORSMiddleware,
            # Same-origin SPA needs no CORS. Extra origins: PORTAL_ALLOWED_ORIGINS=a,b
            allow_origins=[o.strip() for o in os.environ.get("PORTAL_ALLOWED_ORIGINS", "").split(",") if o.strip()],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        ),
    ],
)

if __name__ == "__main__":
    import uvicorn

    def _handle_sigterm(signum, frame):
        """Clean shutdown on SIGTERM — prevents 30s timeout + SIGKILL."""
        print("[portal] SIGTERM received, shutting down gracefully...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    port = int(os.environ.get("PORT", 8097))
    print(f"[portal] Starting yourAICIV portal on port {port}")
    print(f"[portal] Bearer token: stored in {TOKEN_FILE}")
    print(f"[portal] Client sites: /site/<slug>/ from {client_sites_registry()}")
    if _ENV_FILE_LOADED:
        print(f"[portal] From .env (not in process env): {', '.join(sorted(_ENV_FILE_LOADED))}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
