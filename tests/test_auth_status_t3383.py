"""Honest Claude sign-in status + Reconnect (t3383).

Old rule: signed in whenever the token was unexpired OR any tmux session was
alive, so a dead token looked signed in. New rule: signed in when the token is
unexpired, or it expired but a refresh token exists and the PRIMARY session made
a real turn after the expiry. Reconnect only moves the credentials file aside.

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

    typed = []
    real_run = subprocess.run

    def guarded_run(cmd, *a, **kw):
        flat = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "send-keys" in flat or "pkill" in flat or flat.startswith("kill"):
            typed.append(flat)
            raise AssertionError(f"auth status/reconnect must not type or kill: {flat}")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            # Never reach a real tmux server from tests: pretend no session.
            return subprocess.CompletedProcess(cmd, 1, "", "no server")
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

def test_newborn_never_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_primary_session_runs_claude_sync", lambda: True)
    s = _status(mod)
    assert s["authenticated"] is False and s["live_session"] is False


def test_established_civ_with_claude_running_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    mod.FIRST_BOOT_MARKER.write_text("1")
    monkeypatch.setattr(mod, "_primary_session_runs_claude_sync", lambda: True)
    assert _status(mod)["live_session"] is True
    monkeypatch.setattr(mod, "_primary_session_runs_claude_sync", lambda: None)
    assert _status(mod)["live_session"] is True  # cannot tell -> treat as live
    monkeypatch.setattr(mod, "_primary_session_runs_claude_sync", lambda: False)
    assert _status(mod)["live_session"] is False


def test_session_detection_reads_only(portal, monkeypatch):
    """Real detection against a real private tmux server: finds a process whose
    argv[0] is 'claude' in the pane tree, and never types into the pane."""
    mod, home, typed = portal
    import shutil
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    sock = str(home / "t.sock")

    def run_private(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            if "send-keys" in cmd:
                typed.append(" ".join(cmd))
                raise AssertionError("typed")
            cmd = ["tmux", "-S", sock] + list(cmd[1:])
        return _REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", run_private)
    monkeypatch.setattr(mod, "get_tmux_session", lambda: "sbx-primary")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    _REAL_RUN(["tmux", "-S", sock, "new-session", "-d", "-s", "sbx-primary", "bash --norc --noprofile"], env=env, check=True)
    try:
        assert mod._primary_session_runs_claude_sync() is False
        _REAL_RUN(["tmux", "-S", sock, "new-window", "-t", "sbx-primary",
                   "exec -a claude sleep 30"], env=env, check=True)
        time.sleep(0.5)
        assert mod._primary_session_runs_claude_sync() is True
    finally:
        _REAL_RUN(["tmux", "-S", sock, "kill-server"], env=env)
    monkeypatch.setattr(mod, "get_tmux_session", lambda: "no-such-session")
    assert mod._primary_session_runs_claude_sync() is False
    assert typed == []


def test_argv_is_claude():
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert ps._argv_is_claude(["claude", "--dangerously-skip-permissions"])
    assert ps._argv_is_claude(["/home/a/.local/share/claude/versions/2.1.280", "--x"])
    assert ps._argv_is_claude(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
    assert not ps._argv_is_claude(["bash", "--norc"])
    assert not ps._argv_is_claude(["python3", "portal_server.py"])


# --- reconnect ---------------------------------------------------------------

def test_reconnect_moves_credentials_aside_and_returns_status(portal):
    mod, home, typed = portal
    mod.FIRST_BOOT_MARKER.write_text("1")
    _creds(home, accessToken="a", refreshToken="r", expiresAt=_now_ms() + 3600_000)
    original = (home / ".claude" / ".credentials.json").read_text()
    r = TestClient(mod.app).post("/api/auth/reconnect", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False and body["reason"] == "no_credentials"
    assert body["reconnect"]["moved"] is True
    backup = home / ".claude" / body["reconnect"]["backup"]
    assert backup.name.startswith(".credentials.json.bak-")
    assert backup.read_text() == original  # nothing lost
    assert not (home / ".claude" / ".credentials.json").exists()
    assert typed == []


def test_reconnect_twice_keeps_both_backups(portal):
    mod, home, _ = portal
    c = TestClient(mod.app)
    _creds(home, accessToken="first")
    b1 = c.post("/api/auth/reconnect", headers=H).json()["reconnect"]["backup"]
    _creds(home, accessToken="second")
    b2 = c.post("/api/auth/reconnect", headers=H).json()["reconnect"]["backup"]
    assert b1 != b2
    assert "first" in (home / ".claude" / b1).read_text()
    assert "second" in (home / ".claude" / b2).read_text()


def test_reconnect_without_credentials_is_harmless(portal):
    mod, home, _ = portal
    body = TestClient(mod.app).post("/api/auth/reconnect", headers=H).json()
    assert body["reconnect"] == {"moved": False, "backup": None}
    assert body["authenticated"] is False


def test_reconnect_requires_token_post_and_non_managed(portal, monkeypatch):
    mod, home, _ = portal
    _creds(home, accessToken="a")
    c = TestClient(mod.app)
    assert c.post("/api/auth/reconnect").status_code == 401
    assert c.get("/api/auth/reconnect", headers=H).status_code == 405
    monkeypatch.setenv("PORTAL_ENGINE_MANAGED", "1")
    assert c.post("/api/auth/reconnect", headers=H).status_code == 409
    assert (home / ".claude" / ".credentials.json").exists()


def test_new_code_has_no_key_or_kill_paths():
    """Static guard: the new status/reconnect code never sends keys or signals."""
    import inspect
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    for fn in (ps._claude_auth_status_payload, ps.api_claude_auth_status, ps.api_claude_auth_reconnect,
               ps._primary_session_runs_claude_sync, ps._primary_turn_after, ps._file_turn_after):
        src = inspect.getsource(fn)
        for bad in ("send-keys", "pkill", "os.kill", "_kill_claude_process", "signal.", "restart"):
            assert bad not in src, (fn.__name__, bad)
