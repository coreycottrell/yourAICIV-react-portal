"""Honest Claude sign-in status (t3383).

Old rule: signed in whenever the token was unexpired OR any tmux session was
alive, so a dead token looked signed in. New rule: signed in when the token is
unexpired, or it expired but a refresh token exists and the PRIMARY session made
a real turn after the expiry.

Nothing here may press a key or signal a process: every test runs with a guard
that fails on any tmux send-keys / pkill / kill.
"""
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
_REAL_RUN = subprocess.run
_REAL_KILL = os.kill  # the test's own sleep processes only


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIV_ROOT", str(home))
    monkeypatch.delenv("TRIAL_CONFIG_PATH", raising=False)
    monkeypatch.delenv("PORTAL_ENGINE_MANAGED", raising=False)
    monkeypatch.setenv("PORTAL_TOKEN_FILE", str(tmp_path / "token"))
    (tmp_path / "token").write_text(TOKEN)
    sys.modules.pop("portal_server", None)
    mod = importlib.import_module("portal_server")
    getattr(mod, "_auth_turn_cache", {}).clear()
    if hasattr(mod, "_claude_processes_sync"):
        mod._claude_processes_sync_orig = mod._claude_processes_sync
        # Tests never see the host's real Claude processes unless they ask.
        mod._claude_processes_sync = lambda: []

    typed = []
    real_run = subprocess.run

    def guarded_run(cmd, *a, **kw):
        flat = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "send-keys" in flat or "pkill" in flat or flat.startswith("kill"):
            typed.append(flat)
            raise AssertionError(f"auth status/reconnect must not type or kill: {flat}")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            # Never reach a real tmux server from tests: pretend no session.
            return subprocess.CompletedProcess(cmd, 1, "", "no server running on /tmp/tmux-test/default")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", guarded_run)
    monkeypatch.setattr(os, "kill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("os.kill called")))
    return mod, home, typed


def _creds(home, **oauth):
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def _now_ms():
    return int(time.time() * 1000)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _turn(ms, **over):
    e = {"type": "assistant", "timestamp": _iso(ms), "entrypoint": "cli", "isSidechain": False,
         "message": {"role": "assistant", "model": "claude-opus-5", "content": [{"type": "text", "text": "hi"}]}}
    e.update(over)
    return e


def _write_transcript(dirpath, name, entries, mtime_ms=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / name
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    if mtime_ms is not None:
        os.utime(f, (mtime_ms / 1000, mtime_ms / 1000))
    return f


def _primary_dir(home):
    return home / ".claude" / "projects" / ("-" + str(home).strip("/").replace("/", "-").replace("_", "-").replace(".", "-"))


def _status(mod):
    return TestClient(mod.app).get("/api/auth/status", headers=H).json()


# --- status: signed in -----------------------------------------------------

def test_unexpired_token_is_signed_in(portal):
    mod, home, typed = portal
    _creds(home, accessToken="a", refreshToken="r", expiresAt=_now_ms() + 3600_000, subscriptionType="max")
    s = _status(mod)
    assert s["authenticated"] is True and s["reason"] == "token_valid"
    assert typed == []


def test_actively_working_civ_with_expired_token_stays_signed_in(portal):
    mod, home, typed = portal
    exp = _now_ms() - 2 * 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    _write_transcript(_primary_dir(home), "primary.jsonl",
                      [_turn(exp - 60_000), {"type": "user", "timestamp": _iso(exp + 1000)},
                       _turn(exp + 5 * 60_000)])
    s = _status(mod)
    assert s["authenticated"] is True
    assert s["reason"] == "expired_but_session_active"
    assert typed == []


def test_missing_expires_at_keeps_old_answer(portal):
    mod, home, _ = portal
    _creds(home, accessToken="a")
    s = _status(mod)
    assert s["authenticated"] is True and s["reason"] == "no_expiry_recorded"


def test_managed_engine_unchanged(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setenv("PORTAL_ENGINE_MANAGED", "1")
    s = _status(mod)
    assert s == {"authenticated": True, "managed": True, "account": None,
                 "expires_at": None, "subscription": None}


# --- status: signed out (the bug) ------------------------------------------

def test_expired_idle_civ_reads_signed_out_even_with_tmux_alive(portal, monkeypatch):
    """The real bug: main said signed in because a tmux session was alive."""
    mod, home, typed = portal
    exp = _now_ms() - 3 * 86400_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp - 3600_000)], mtime_ms=exp - 3600_000)
    real = mod.subprocess.run

    def tmux_alive(cmd, *a, **kw):  # every tmux call succeeds: the session is up
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *a, **kw)
    monkeypatch.setattr(mod.subprocess, "run", tmux_alive)

    async def fake_async(cmd, timeout=5, check=False):  # main's tmux has-session path
        return subprocess.CompletedProcess(cmd, 0, None, None)
    monkeypatch.setattr(mod, "_run_subprocess_async", fake_async)
    s = _status(mod)
    assert s["authenticated"] is False
    assert s["reason"] == "expired_no_activity_since"
    assert typed == []


def test_expired_without_refresh_token_is_signed_out_even_if_working(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="", expiresAt=exp)
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp + 60_000)])
    s = _status(mod)
    assert s["authenticated"] is False and s["reason"] == "expired_no_refresh_token"


def test_turn_only_in_headless_session_does_not_count(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    _write_transcript(_primary_dir(home), "headless.jsonl", [_turn(exp + 60_000, entrypoint="sdk-cli")])
    assert _status(mod)["authenticated"] is False


def test_turn_only_in_another_project_dir_does_not_count(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    other = home / ".claude" / "projects" / ("-" + str(home).strip("/").replace("/", "-") + "-tools-worktree")
    _write_transcript(other, "tool.jsonl", [_turn(exp + 60_000)])
    assert _status(mod)["authenticated"] is False


def test_subagent_transcript_does_not_count(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    _write_transcript(_primary_dir(home) / "sess" / "subagents", "agent-1.jsonl", [_turn(exp + 60_000)])
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp + 60_000, isSidechain=True)])
    assert _status(mod)["authenticated"] is False


def test_api_error_after_expiry_does_not_count(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    err = _turn(exp + 60_000, isApiErrorMessage=True)
    err["message"]["model"] = "<synthetic>"
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp - 60_000), err])
    s = _status(mod)
    assert s["authenticated"] is False and s["reason"] == "expired_no_activity_since"


def test_turn_before_expiry_does_not_count_even_if_file_touched_later(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    _write_transcript(_primary_dir(home), "primary.jsonl",
                      [_turn(exp - 60_000), {"type": "user", "timestamp": _iso(exp + 60_000)}])
    assert _status(mod)["authenticated"] is False


def test_no_credentials_and_no_access_token(portal):
    mod, home, _ = portal
    s = _status(mod)
    assert s["authenticated"] is False and s["reason"] == "no_credentials"
    _creds(home, refreshToken="r")
    s = _status(mod)
    assert s["authenticated"] is False and s["reason"] == "no_access_token"


def test_large_transcript_only_tail_is_read(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    filler = [{"type": "user", "timestamp": _iso(exp - 10_000), "pad": "x" * 2000}] * 2000  # ~4MB
    _write_transcript(_primary_dir(home), "primary.jsonl", filler + [_turn(exp + 60_000)])
    assert _status(mod)["authenticated"] is True


# --- live_session flag (drives the plain note instead of the sign-in flow) --

def test_argv_is_claude():
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert ps._argv_is_claude(["claude", "--dangerously-skip-permissions"])
    assert ps._argv_is_claude(["/home/a/.local/share/claude/versions/2.1.280", "--x"])
    assert ps._argv_is_claude(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert not ps._argv_is_claude(["bash", "--norc"])
    assert not ps._argv_is_claude(["python3", "portal_server.py"])


def test_auth_error_after_last_turn_reads_signed_out(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    err = _turn(exp + 120_000, isApiErrorMessage=True, error="authentication_failed")
    err["message"]["model"] = "<synthetic>"
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp + 60_000), err])
    s = _status(mod)
    assert s["authenticated"] is False and s["reason"] == "expired_no_activity_since"
    # a real turn AFTER the failure (refresh worked again) reads signed in
    mod._auth_turn_cache.clear()
    _write_transcript(_primary_dir(home), "primary.jsonl", [_turn(exp + 60_000), err, _turn(exp + 180_000)])
    assert _status(mod)["authenticated"] is True


def test_newest_transcript_decides(portal):
    mod, home, _ = portal
    exp = _now_ms() - 3600_000
    _creds(home, accessToken="a", refreshToken="r", expiresAt=exp)
    err = _turn(exp + 600_000, isApiErrorMessage=True, error="authentication_failed")
    err["message"]["model"] = "<synthetic>"
    _write_transcript(_primary_dir(home), "old.jsonl", [_turn(exp + 60_000)], mtime_ms=exp + 60_000)
    _write_transcript(_primary_dir(home), "new.jsonl", [err], mtime_ms=exp + 600_000)
    assert _status(mod)["authenticated"] is False



# --- live_session flag (drives the plain note instead of the sign-in flow) --

def test_newborn_never_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [123])
    s = _status(mod)
    assert s["authenticated"] is False and s["live_session"] is False


def test_established_civ_with_claude_running_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    # t3383 helper: live = a working AI (argv), not a bare sign-in
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "--dangerously-skip-permissions"])
    mod.FIRST_BOOT_MARKER.write_text("1")
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [123])
    assert _status(mod)["live_session"] is True
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: None)
    assert _status(mod)["live_session"] is True  # cannot tell -> treat as live
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [])
    assert _status(mod)["live_session"] is False


def test_evolution_done_marker_alone_counts_as_established(portal, monkeypatch):
    mod, home, _ = portal
    # t3383 helper: live = a working AI (argv), not a bare sign-in
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "--dangerously-skip-permissions"])
    mod.EVOLUTION_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    mod.EVOLUTION_DONE_MARKER.touch()
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [123])
    assert _status(mod)["live_session"] is True


def test_process_scan_is_real_and_read_only(portal):
    """Real /proc scan finds a process whose argv[0] is 'claude'; nothing is signalled."""
    mod, home, typed = portal
    before = set(mod._claude_processes_sync_orig() or [])
    ai = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
    try:
        time.sleep(0.3)
        after = set(mod._claude_processes_sync_orig() or [])
        assert ai.pid in after and ai.pid not in before
    finally:
        _REAL_KILL(ai.pid, 9); ai.wait()
    assert typed == []


def test_status_code_never_types_or_kills():
    """Static guard: the new status code never sends keys or signals."""
    import inspect
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    for fn in (ps._claude_auth_status_payload, ps.api_claude_auth_status, ps._claude_processes_sync,
               ps._established_ai_running, ps._primary_turn_after, ps._file_turn_after):
        src = inspect.getsource(fn)
        for bad in ("send-keys", "pkill", "os.kill", "_kill_claude_process", "signal.", "restart", "tmux"):
            assert bad not in src, (fn.__name__, bad)
