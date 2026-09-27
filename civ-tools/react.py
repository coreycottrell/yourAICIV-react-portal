#!/usr/bin/env python3
"""
React to a portal chat message as the CIV.
Direct file append — no HTTP, no auth, no server dependency.

Usage:
    python3 ~/civ/tools/react.py <msg_id> <emoji> [msg_preview] [msg_role]

Examples:
    python3 ~/civ/tools/react.py abc123 🚀
    python3 ~/civ/tools/react.py abc123 🔥 "great idea" user
    python3 ~/civ/tools/react.py abc123 💎 "nailed it" user
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Same file portal_server.py writes: <portal dir>/reaction-sentiment.jsonl.
# Where the portal lives, first match wins:
#   1. $PORTAL_DIR
#   2. ~/.portal_dir, a one-line pointer the running portal writes at startup
#      (so installs at any path, e.g. the directory named by the birth
#      template's tools/watchdog.sh PORTAL_DIR line, keep working)
#   3. the portal checkout this file sits in (civ-tools/../portal_server.py)
#   4. ~/youraiciv_portal
DEFAULT_PORTAL_DIR = Path.home() / "youraiciv_portal"


def _portal_dir() -> Path:
    env = os.environ.get("PORTAL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        pointer = (Path.home() / ".portal_dir").read_text().strip()
        if pointer and (Path(pointer) / "portal_server.py").is_file():
            return Path(pointer)
    except OSError:
        pass
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "portal_server.py").is_file():
        return checkout
    return DEFAULT_PORTAL_DIR


PORTAL_DIR = _portal_dir()
REACTION_LOG = PORTAL_DIR / "reaction-sentiment.jsonl"

EMOJI_SENTIMENT = {
    "\U0001F44D": ("thumbs-up",   "positive",  1),
    "\U0001F44E": ("thumbs-down", "negative",  -1),
    "\U0001F680": ("rocket",      "excited",   2),
    "\U0001F525": ("fire",        "fire",      2),
    "\u2705":     ("check",       "positive",  1),
    "\U0001F4A5": ("explosion",   "excited",   2),
    "\U0001F92F": ("mind-blown",  "amazed",    3),
    "\U0001F4AA": ("muscle",      "positive",  1),
    "\U0001F3AF": ("bullseye",    "precision", 2),
    "\U0001F48E": ("gem",         "premium",   2),
    "\u2764\uFE0F": ("heart",     "love",      5),
    "\U0001F60D": ("heart-eyes",  "love",      10),
    "\U0001F622": ("sad",         "negative",  -1),
    "\U0001F610": ("neutral",     "neutral",   0),
}

def react(msg_id: str, emoji: str, msg_preview: str = "", msg_role: str = "user") -> dict:
    name, sentiment, weight = EMOJI_SENTIMENT.get(emoji, (emoji, "unknown", 0))
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "msg_id": msg_id,
        "emoji": emoji,
        "emoji_name": name,
        "sentiment": sentiment,
        "weight": weight,
        "action": "add",
        "msg_role": msg_role,
        "msg_preview": msg_preview[:200],
        "reactor": "civ",
    }
    with open(REACTION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: react.py <msg_id> <emoji> [msg_preview] [msg_role]")
        sys.exit(1)
    result = react(
        msg_id=sys.argv[1],
        emoji=sys.argv[2],
        msg_preview=sys.argv[3] if len(sys.argv) > 3 else "",
        msg_role=sys.argv[4] if len(sys.argv) > 4 else "user",
    )
    print(json.dumps(result, indent=2))
