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
import os
import re
import secrets
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
        r'Select login method|Use OAuth|How would you like to authenticate',
        re.IGNORECASE,
    ),
    'csat_survey': re.compile(
        r'How is Claude doing\?|rate your experience|satisfaction survey|'
        r'How would you rate|thumbs up|Would you recommend',
        re.IGNORECASE,
    ),
    'update_prompt': re.compile(
        r'Auto-update|update available|Update now\?|new version|'
        r'would you like to update|upgrade available',
        re.IGNORECASE,
    ),
    'trust_folder': re.compile(
        r'Do you trust the authors|trust this (?:project|folder)|'
        r'Trust this project|Do you want to trust',
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
    'error': re.compile(
        r'(?:Error|ENOENT|crash|fatal|SIGTERM|SIGKILL|panic|'
        r'Cannot connect|Connection refused)',
        re.IGNORECASE,
    ),
}
AUTH_SCREEN_PRIORITY = [
    'oauth_url', 'logged_in', 'csat_survey', 'update_prompt',
    'trust_folder', 'login_menu', 'error', 'shell_prompt',
]


# Global auth state (simple, in-process)
_auth_prewarm_task = None  # background prewarm task handle


async def _is_claude_running_async(pane: str) -> bool:
    """Check if Claude Code is the active process in the given tmux pane."""
    try:
        r = await _run_subprocess_async(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}"],
            timeout=3
        )
        if r and r.stdout:
            cmd = r.stdout.strip().lower()
            return "claude" in cmd or "node" in cmd
    except Exception:
        pass
    return False


async def _detect_auth_screen(pane: str) -> tuple:
    """Capture tmux pane and detect what's currently displayed.
    Returns (screen_type, raw_content) where screen_type is one of the
    AUTH_SCREEN_PATTERNS keys or 'unknown'/'empty'.
    """
    content = await _run_subprocess_output(
        ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-300"], timeout=5
    )
    if not content:
        return 'empty', ''
    for name in AUTH_SCREEN_PRIORITY:
        pattern = AUTH_SCREEN_PATTERNS[name]
        match = pattern.search(content)
        if match:
            if name == 'oauth_url':
                url = match.group(0).strip()
                if 'state=' not in url:
                    continue  # Truncated URL, keep looking
            return name, content
    return 'unknown', content


async def _dismiss_auth_blocker(pane: str, screen_type: str) -> bool:
    """Dismiss a blocking dialog. Returns True if action was taken."""
    if screen_type == 'csat_survey':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        await asyncio.sleep(0.5)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        return True
    elif screen_type == 'update_prompt':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        await asyncio.sleep(0.5)
        return True
    elif screen_type == 'trust_folder':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", "y"])
        await asyncio.sleep(0.2)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
        return True
    return False


async def _kill_claude_process() -> None:
    """Kill any running Claude process in this container."""
    await _run_subprocess_output(
        ["bash", "-c", "pkill -f 'claude' 2>/dev/null; pkill -f 'node.*claude' 2>/dev/null; true"],
        timeout=5,
    )


async def _run_auth_state_machine(pane: str) -> dict:
    """Run the auth flow state machine. Returns dict with status info.

    States: start -> waiting_for_screen -> (dismiss blockers | select_login) ->
            waiting_for_url -> success/failed

    This is the core v2 auth logic ported from auth-flow-v2.py, adapted for
    async execution inside the portal server.
    """
    global _captured_oauth_url
    max_retries = 3
    retry_count = 0
    claude_start_timeout = 45.0
    url_wait_timeout = 30.0
    poll_interval = 0.5
    log_entries = []

    def log(msg):
        log_entries.append(msg)
        _save_portal_message(f"[auth-v2] {msg}", role="assistant")

    # Clear tmux scrollback so stale text from prior runs cannot poison _detect_auth_screen
    await _run_subprocess_async(["tmux", "clear-history", "-t", pane])

    while retry_count <= max_retries:
        # --- Phase 1: Start Claude /login ---
        log(f"Starting auth flow (attempt {retry_count + 1}/{max_retries + 1})")

        # Resize tmux so URLs don't wrap
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await asyncio.sleep(0.3)

        claude_running = await _is_claude_running_async(pane)
        if not claude_running:
            log("Claude not running — launching 'claude /login'")
            launch_cmd = f"clear && cd {Path.home()} && claude /login"
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", launch_cmd], check=True)
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        else:
            log("Claude already running — sending /login")
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", "/login"], check=True)
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)

        # --- Phase 2: Wait for screen and handle blockers ---
        phase_start = time.time()
        login_selected = False

        while True:
            await asyncio.sleep(poll_interval)
            elapsed = time.time() - phase_start

            screen_type, screen_content = await _detect_auth_screen(pane)

            if screen_type == 'oauth_url':
                # Goal state — extract URL
                match = OAUTH_URL_PATTERN.search(screen_content)
                if match:
                    url = match.group(0).strip()
                    if 'state=' in url:
                        _captured_oauth_url = url
                        log(f"OAuth URL captured ({len(url)} chars) in {elapsed:.1f}s")
                        return {"started": True, "url": url, "log": log_entries}

            elif screen_type == 'logged_in':
                log("Already logged in — no OAuth URL needed")
                return {"started": True, "already_authenticated": True, "log": log_entries}

            elif screen_type in ('csat_survey', 'update_prompt', 'trust_folder'):
                log(f"Dismissing blocker: {screen_type}")
                await _dismiss_auth_blocker(pane, screen_type)
                await asyncio.sleep(1.0)
                continue

            elif screen_type == 'login_menu' and not login_selected:
                log("Login menu detected — selecting first option (Enter)")
                await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
                login_selected = True
                phase_start = time.time()  # Reset timeout for URL wait phase
                continue

            elif screen_type == 'error':
                log(f"Error detected on screen — will retry")
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
            log(f"Killing Claude for clean restart (retry {retry_count}/{max_retries})")
            await _kill_claude_process()
            await asyncio.sleep(2.0)
            # Verify it's dead
            if await _is_claude_running_async(pane):
                await _kill_claude_process()
                await asyncio.sleep(2.0)
            # Clear tmux scrollback before next attempt so stale error text cannot poison detection
            await _run_subprocess_async(["tmux", "clear-history", "-t", pane])

    log(f"Auth flow FAILED after {max_retries + 1} attempts")
    return {"started": False, "error": "auth flow failed after retries", "log": log_entries}


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


# ---------------------------------------------------------------------------
# Claude sign-in status (t3383). Everything in this block only READS: files
# under ~/.claude, /proc, and `tmux has-session` / `tmux list-panes`. It never
# sends a key to tmux and never signals a process.
# ---------------------------------------------------------------------------

_AUTH_SCAN_WINDOWS = (256 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024)
_AUTH_SCAN_MAX_FILES = 5
_auth_turn_cache: dict = {}


def _primary_project_dir() -> Path:
    """The Claude project dir of the primary session, which runs from $HOME.
    Claude names a project dir after its path with every non-alphanumeric
    character turned into '-': /home/aiciv -> -home-aiciv. Headless and tool
    sessions run from other folders land in other dirs and are never read."""
    return _PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", str(Path.home()))


def _iso_to_ms(ts) -> int | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _is_real_primary_turn(entry: dict) -> bool:
    """A reply the model actually produced in the interactive primary session.
    Not: API error placeholders, synthetic messages, sidechain (subagent)
    turns, or turns from a headless run (entrypoint sdk-*)."""
    if entry.get("type") != "assistant":
        return False
    if entry.get("isSidechain") or entry.get("isApiErrorMessage"):
        return False
    entrypoint = entry.get("entrypoint")
    if entrypoint is not None and entrypoint != "cli":
        return False
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return False
    model = msg.get("model")
    return isinstance(model, str) and bool(model) and model != "<synthetic>"


def _file_turn_after(path: Path, after_ms: int) -> bool:
    """True if the transcript has a real primary turn stamped after after_ms.
    Reads the end of the file only, widening the window until it reaches an
    entry older than after_ms (transcripts are append-only, oldest first)."""
    try:
        st = path.stat()
    except OSError:
        return False
    key = (str(path), st.st_mtime_ns, st.st_size, after_ms)
    if key in _auth_turn_cache:
        return _auth_turn_cache[key]
    result = False
    try:
        with path.open("rb") as f:
            for window in _AUTH_SCAN_WINDOWS:
                start = max(0, st.st_size - window)
                f.seek(start)
                lines = f.read(st.st_size - start).split(b"\n")
                if start > 0:
                    lines = lines[1:]  # first line of the window may be cut
                decided = False
                for raw in reversed(lines):
                    try:
                        entry = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    ts = _iso_to_ms(entry.get("timestamp"))
                    if ts is None:
                        continue
                    if ts <= after_ms:
                        decided = True
                        break
                    if _is_real_primary_turn(entry):
                        result = True
                        decided = True
                        break
                if decided or start == 0:
                    break
    except OSError:
        result = False
    if len(_auth_turn_cache) > 64:
        _auth_turn_cache.clear()
    _auth_turn_cache[key] = result
    return result


def _primary_turn_after(after_ms: int) -> bool:
    """True if the primary session's own transcripts hold a real turn made
    after after_ms. Only top-level *.jsonl in the primary project dir."""
    proj = _primary_project_dir()
    try:
        files = []
        for jf in proj.glob("*.jsonl"):
            try:
                mtime_ms = jf.stat().st_mtime * 1000
            except OSError:
                continue
            if jf.is_file() and mtime_ms > after_ms:
                files.append((mtime_ms, jf))
    except OSError:
        return False
    files.sort(key=lambda x: x[0], reverse=True)
    return any(_file_turn_after(jf, after_ms) for _, jf in files[:_AUTH_SCAN_MAX_FILES])


def _argv_is_claude(argv: list) -> bool:
    for arg in argv[:3]:
        if not arg:
            continue
        base = os.path.basename(arg)
        if base == "claude" or "/claude/versions/" in arg or "@anthropic-ai/claude-code" in arg:
            return True
    return False


def _primary_session_runs_claude_sync() -> bool | None:
    """Is a Claude process running in the primary tmux session? Read only:
    tmux has-session / list-panes and /proc. None = could not tell."""
    session = get_tmux_session()
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return False
        r = subprocess.run(["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_pid}"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return None
        roots = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        if not roots:
            return None
        children: dict = {}
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                stat = (d / "stat").read_text()
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(d.name))
        seen = set()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                argv = [a.decode("utf-8", "replace") for a in argv if a]
            except OSError:
                argv = []
            if _argv_is_claude(argv):
                return True
            stack.extend(children.get(pid, []))
        return False
    except Exception:
        return None


def _civ_is_established() -> bool:
    """First sign-in already happened here (first boot fired or evolution done).
    A newborn has neither marker, so its sign-in path is never changed."""
    return FIRST_BOOT_MARKER.exists() or EVOLUTION_DONE_MARKER.exists()


async def _claude_auth_status_payload() -> dict:
    """Honest Claude sign-in status.

    Signed in when the access token is unexpired; or when it has expired, a
    refresh token exists, and the primary session made a real turn after the
    expiry (Claude refreshes in memory while it works). Otherwise signed out,
    with a reason. A credentials file with no expiresAt keeps the old answer
    (signed in). A live tmux session alone no longer counts as signed in.
    """
    if _engine_is_managed():
        return {"authenticated": True, "managed": True, "account": None,
                "expires_at": None, "subscription": None}
    try:
        if not CREDENTIALS_FILE.exists():
            payload = {"authenticated": False, "account": None, "expires_at": None,
                       "reason": "no_credentials"}
        else:
            creds = json.loads(CREDENTIALS_FILE.read_text())
            oauth = creds.get("claudeAiOauth", {}) or {}
            if not oauth.get("accessToken"):
                payload = {"authenticated": False, "account": None, "expires_at": None,
                           "reason": "no_access_token"}
            else:
                expires_at = oauth.get("expiresAt", 0)
                signed_in = {"authenticated": True, "account": oauth.get("account"),
                             "expires_at": expires_at, "subscription": oauth.get("subscriptionType")}
                signed_out = {"authenticated": False, "account": oauth.get("account"),
                              "expires_at": expires_at}
                now_ms = int(time.time() * 1000)
                if not expires_at:
                    return {**signed_in, "reason": "no_expiry_recorded"}
                if expires_at >= now_ms:
                    return {**signed_in, "reason": "token_valid"}
                if not oauth.get("refreshToken"):
                    payload = {**signed_out, "reason": "expired_no_refresh_token"}
                else:
                    loop = asyncio.get_event_loop()
                    active = await loop.run_in_executor(None, _primary_turn_after, int(expires_at))
                    if active:
                        return {**signed_in, "reason": "expired_but_session_active"}
                    payload = {**signed_out, "reason": "expired_no_activity_since"}
    except Exception:
        payload = {"authenticated": False, "account": None, "expires_at": None,
                   "reason": "unreadable_credentials"}
    # Signed out: say whether a live AI session is running in an established
    # CIV, so the page can show a plain note instead of a sign-in flow that
    # would type into that session. A newborn never gets this flag.
    try:
        established = _civ_is_established()
        running = None
        if established:
            loop = asyncio.get_event_loop()
            running = await loop.run_in_executor(None, _primary_session_runs_claude_sync)
        payload["live_session"] = bool(established and running is not False)
    except Exception:
        payload["live_session"] = False
    return payload


async def api_claude_auth_status(request: Request) -> JSONResponse:
    """Check if Claude is authenticated (has valid OAuth credentials)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await _claude_auth_status_payload())


async def api_claude_auth_reconnect(request: Request) -> JSONResponse:
    """Reconnect Claude: move ~/.claude/.credentials.json to a timestamped
    .bak next to it (nothing deleted, memory untouched) and return the new
    status. No keys are typed and no process is touched; the page then
    shows the normal sign-in dialog, or a plain note if the AI is running."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method != "POST":
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    if _engine_is_managed():
        return JSONResponse({"error": "managed", "managed": True}, status_code=409)
    backup = None
    try:
        if CREDENTIALS_FILE.exists() or CREDENTIALS_FILE.is_symlink():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            dest = CREDENTIALS_FILE.with_name(f"{CREDENTIALS_FILE.name}.bak-{stamp}")
            n = 1
            while dest.exists() or dest.is_symlink():
                dest = CREDENTIALS_FILE.with_name(f"{CREDENTIALS_FILE.name}.bak-{stamp}-{n}")
                n += 1
            os.rename(CREDENTIALS_FILE, dest)
            backup = dest.name
    except OSError as e:
        return JSONResponse({"error": f"could not move the credentials file: {e}"}, status_code=500)
    status = await _claude_auth_status_payload()
    return JSONResponse({**status, "reconnect": {"moved": backup is not None, "backup": backup}})


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Start Claude OAuth flow using v2 state machine with screen detection.

    Drives the full auth flow: starts Claude, detects and dismisses blocking
    dialogs (CSAT surveys, update prompts, trust folder), selects login option,
    and polls for OAuth URL. Returns the URL inline when possible.

    This is a longer-running endpoint (up to ~60s) but returns WITH the URL
    when the flow completes successfully.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    _captured_oauth_url = None
    pane = await _find_primary_pane_async()
    _save_portal_message(f"Auth flow v2 started — {get_tmux_session()} (pane {pane})", role="assistant")
    try:
        result = await _run_auth_state_machine(pane)
        return JSONResponse(result)
    except Exception as e:
        _save_portal_message(f"Auth flow v2 failed: {e}", role="assistant")
        return JSONResponse({"error": f"auth flow error: {e}"}, status_code=500)


async def api_claude_auth_prewarm(request: Request) -> JSONResponse:
    """Pre-warm Claude for faster auth. Called when portal page loads.

    Starts `claude /login` in the background without waiting for URL.
    When the human clicks Authenticate, Claude is already started and the
    login menu is likely rendered, shaving 30-45s off the flow.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _auth_prewarm_task
    # Don't start if already prewarming
    if _auth_prewarm_task and not _auth_prewarm_task.done():
        return JSONResponse({"status": "already_prewarming"})
    pane = await _find_primary_pane_async()
    _save_portal_message("Pre-warming Claude for auth...", role="assistant")
    try:
        # Resize tmux
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await asyncio.sleep(0.3)
        claude_running = await _is_claude_running_async(pane)
        if not claude_running:
            launch_cmd = f"cd {Path.home()} && claude /login"
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", launch_cmd], check=True)
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
            _save_portal_message("Pre-warm: Claude /login launched in background", role="assistant")
        else:
            _save_portal_message("Pre-warm: Claude already running", role="assistant")
        return JSONResponse({"status": "prewarming"})
    except Exception as e:
        return JSONResponse({"error": f"prewarm failed: {e}"}, status_code=500)


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Inject the OAuth authorization code into the Claude tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)
    pane = await _find_primary_pane_async()
    _save_portal_message(f"Auth code submitted — injecting into {get_tmux_session()}...", role="assistant")
    try:
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", code], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        _save_portal_message("Code injected — Claude is authenticating...", role="assistant")
        return JSONResponse({"injected": True})
    except Exception as e:
        _save_portal_message(f"Code injection failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from tmux output."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})
    pane = await _find_primary_pane_async()
    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = await _run_subprocess_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-200"], timeout=5
        )
        if not content:
            return JSONResponse({"url": None, "ready": False})
        match = OAUTH_URL_PATTERN.search(content)
        if match:
            candidate = match.group(0).strip()
            # Validate URL is complete — must contain state= parameter.
            # A truncated URL is worse than no URL (causes "missing state" error on claude.ai).
            if "state=" not in candidate:
                _save_portal_message("OAuth URL found but truncated (missing state=) — retrying capture", role="assistant")
            else:
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
    # Write marker before launching — prevents double-fire on concurrent calls
    try:
        FIRST_BOOT_MARKER.write_text(str(time.time()))
    except Exception as e:
        return JSONResponse({"error": f"could not write marker: {e}"}, status_code=500)

    pane = await _find_primary_pane_async()
    project_dir = str(Path.home())

    # Step 1: Double Ctrl-C to kill the /login Claude instance in the same pane
    _save_portal_message("Auth complete — transitioning to evolution (same pane)...", role="assistant")
    await _kill_claude_process()

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

    # Step 4: Wait for Claude to start in the pane (up to 30 seconds)
    started = False
    for _ in range(60):
        await asyncio.sleep(0.5)
        if await _is_claude_running_async(pane):
            started = True
            break
    if not started:
        _save_portal_message("Claude session didn't confirm within 30s — proceeding with evolution anyway", role="assistant")

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
    Route("/api/auth/reconnect", endpoint=api_claude_auth_reconnect, methods=["POST"]),
    Route("/api/auth/start", endpoint=api_claude_auth_start, methods=["POST"]),
    Route("/api/auth/code", endpoint=api_claude_auth_code, methods=["POST"]),
    Route("/api/auth/url", endpoint=api_claude_auth_url),
    Route("/api/auth/prewarm", endpoint=api_claude_auth_prewarm, methods=["POST"]),
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
