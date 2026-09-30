"""Pytest setup for portal_server.py backend tests.

portal_server.py has import-time side effects (reads/creates files under HOME,
generates a bearer token). Point HOME at a throwaway directory BEFORE import so
tests never touch a real CIV home, and never talk to a real tmux server.
"""
import os
import sys
import tempfile
from pathlib import Path

_TEST_HOME = tempfile.mkdtemp(prefix="portal-test-home-")
os.environ["HOME"] = _TEST_HOME
os.environ.pop("TMUX", None)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
