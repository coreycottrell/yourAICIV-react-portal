"""The chat feed reads only this civ's own session files.

Headless sessions run from other folders (plugins, SDK tools, workflow agents in
isolated worktrees) land in other project dirs. Their "user" turns are tool
prompts, so reading them showed tool prompts in the chat as if the human wrote them.
"""
import importlib
import os
import sys
import time

import pytest


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIV_ROOT", str(home))
    monkeypatch.delenv("TRIAL_CONFIG_PATH", raising=False)
    monkeypatch.setenv("PORTAL_TOKEN_FILE", str(tmp_path / "token"))
    (tmp_path / "token").write_text("test-token")
    sys.modules.pop("portal_server", None)
    mod = importlib.import_module("portal_server")
    return mod, home


def test_chat_reads_only_own_project_dir(portal, monkeypatch):
    mod, home = portal
    projects = home / ".claude" / "projects"
    own = projects / ("-" + str(home).strip("/").replace("/", "-"))
    other = projects / ("-" + str(home).strip("/").replace("/", "-") + "-builds-worktree")
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    own_log = own / "primary.jsonl"
    own_log.write_text("{}\n")
    other_log = other / "headless-review.jsonl"
    other_log.write_text("{}\n")
    # The headless session is the newest file; it must still be ignored.
    now = time.time()
    os.utime(own_log, (now - 60, now - 60))
    os.utime(other_log, (now, now))

    monkeypatch.setattr(mod, "_PROJECTS_DIR", projects)
    mod._project_jsonl_cache = (0, [])

    paths = mod._get_all_session_log_paths(max_files=3)
    assert [str(p) for p in paths] == [str(own_log)]
