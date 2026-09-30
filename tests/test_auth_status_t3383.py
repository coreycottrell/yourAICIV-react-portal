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

def test_newborn_never_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: True)
    s = _status(mod)
    assert s["authenticated"] is False and s["live_session"] is False


def test_established_civ_with_claude_running_flagged_live(portal, monkeypatch):
    mod, home, _ = portal
    mod.FIRST_BOOT_MARKER.write_text("1")
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: True)
    assert _status(mod)["live_session"] is True
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: None)
    assert _status(mod)["live_session"] is True  # cannot tell -> treat as live
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: False)
    assert _status(mod)["live_session"] is False


def test_process_detection_reads_only_and_exempts_the_signin_flow(portal, monkeypatch):
    """Real /proc scan: finds a process whose argv[0] is 'claude' anywhere
    (not only in tmux); a Claude started after an allowed sign-in flow began is
    exempt until the sign-in succeeds; nothing is signalled."""
    mod, home, typed = portal
    mine = [p for p, _ in (mod._claude_processes_sync_orig() or [])]  # host's own, ignored

    def only_test_procs():
        procs = _REAL_CLAUDE_SCAN(mod)
        return [(p, s) for p, s in procs if p not in mine]
    monkeypatch.setattr(mod, "_claude_processes_sync", only_test_procs)
    assert mod._working_ai_running_sync(fresh=True) is False
    ai = _REAL_POPEN(["bash", "-c", "exec -a claude sleep 30"])
    try:
        time.sleep(0.3)
        assert mod._working_ai_running_sync(fresh=True) is True
        # a sign-in flow that started AFTER the AI does not exempt it
        monkeypatch.setattr(mod, "_signin_flow_started_at", time.time())
        assert mod._working_ai_running_sync(fresh=True) is True
    finally:
        _REAL_KILL(ai.pid, 9); ai.wait()
    monkeypatch.setattr(mod, "_signin_flow_started_at", time.time() - 1)
    helper = _REAL_POPEN(["bash", "-c", "exec -a claude sleep 30"])  # started after the flow began
    try:
        time.sleep(0.3)
        assert mod._working_ai_running_sync(fresh=True) is False  # the flow's own claude /login
        mod._signed_in_done({})                                     # sign-in succeeded
        assert mod._working_ai_running_sync(fresh=True) is True   # now it is the working AI
        monkeypatch.setattr(mod, "_signin_flow_started_at", time.time() - 3600)
        assert mod._working_ai_running_sync(fresh=True) is True   # stale flow window
    finally:
        _REAL_KILL(helper.pid, 9); helper.wait()
    assert typed == []


_REAL_POPEN = subprocess.Popen


def _REAL_CLAUDE_SCAN(mod):
    return [x for x in (mod.__dict__["_claude_processes_sync_orig"]() or [])]


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
               ps._working_ai_running_sync, ps._claude_processes_sync, ps._primary_turn_after,
               ps._file_turn_after, ps._established_ai_running):
        src = inspect.getsource(fn)
        for bad in ("send-keys", "pkill", "os.kill", "_kill_claude_process", "signal.", "restart"):
            assert bad not in src, (fn.__name__, bad)


def test_reconnect_held_while_established_ai_runs(portal, monkeypatch):
    """Established CIV with its AI running (or unknown): nothing is moved."""
    mod, home, typed = portal
    mod.FIRST_BOOT_MARKER.write_text("1")
    _creds(home, accessToken="a", refreshToken="r", expiresAt=_now_ms() + 3600_000)
    original = (home / ".claude" / ".credentials.json").read_text()
    c = TestClient(mod.app)
    for running in (True, None):
        monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False, r=running: r)
        body = c.post("/api/auth/reconnect", headers=H).json()
        assert body["reconnect"] == {"moved": False, "backup": None, "held": "live_session"}
        assert body["live_session"] is True
        assert body["authenticated"] is True  # still signed in: nothing touched
        assert (home / ".claude" / ".credentials.json").read_text() == original
        assert not [f for f in (home / ".claude").iterdir() if ".bak-" in f.name]
    assert typed == []


def test_reconnect_moves_for_newborn_even_if_claude_runs(portal, monkeypatch):
    """A newborn mid-sign-in (claude /login in the pane) is not an established AI."""
    mod, home, _ = portal
    _creds(home, accessToken="a")
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: True)
    body = TestClient(mod.app).post("/api/auth/reconnect", headers=H).json()
    assert body["reconnect"]["moved"] is True


def test_signin_flow_refuses_over_a_working_ai(portal, monkeypatch):
    """Server-side guard: /api/auth/start and /prewarm never touch the pane of
    an established CIV while its AI runs; a newborn is unaffected."""
    mod, home, typed = portal
    touched = []

    async def fake_async(cmd, timeout=5, check=False):
        touched.append(cmd)
        raise AssertionError(f"touched tmux: {cmd}")
    monkeypatch.setattr(mod, "_run_subprocess_async", fake_async)
    mod.FIRST_BOOT_MARKER.write_text("1")
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: True)
    c = TestClient(mod.app)
    for path in ("/api/auth/start", "/api/auth/prewarm"):
        body = c.post(path, headers=H).json()
        assert body["started"] is False and body["live_session"] is True and "running" in body["error"]
    assert touched == [] and typed == []


def test_signin_flow_unchanged_for_newborn(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_working_ai_running_sync", lambda fresh=False: True)
    called = []

    async def fake_machine(pane):
        called.append(pane)
        return {"started": True, "url": "https://claude.ai/oauth/authorize?state=x"}
    monkeypatch.setattr(mod, "_run_auth_state_machine", fake_machine)

    async def pane():
        return "%0"
    monkeypatch.setattr(mod, "_find_primary_pane_async", pane)
    body = TestClient(mod.app).post("/api/auth/start", headers=H).json()
    assert body["started"] is True and called == ["%0"]


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


def test_code_refused_over_a_working_ai_but_allowed_for_the_flows_own_login(portal, monkeypatch):
    mod, home, typed = portal
    mod.FIRST_BOOT_MARKER.write_text("1")
    sent = []

    async def fake_async(cmd, timeout=5, check=False):
        sent.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, None, None)
    monkeypatch.setattr(mod, "_run_subprocess_async", fake_async)

    async def pane():
        return "%0"
    monkeypatch.setattr(mod, "_find_primary_pane_async", pane)
    c = TestClient(mod.app)
    # a working AI started before any sign-in flow: refused, nothing typed
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [(4242, time.time() - 600)])
    body = c.post("/api/auth/code", headers=H, json={"code": "abc#def"}).json()
    assert body.get("live_session") is True and "error" in body and sent == []
    # the flow's own claude /login (started after the flow began): allowed
    monkeypatch.setattr(mod, "_signin_flow_started_at", time.time() - 5)
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [(4243, time.time() - 2)])
    body = c.post("/api/auth/code", headers=H, json={"code": "abc#def"}).json()
    assert body.get("injected") is True and any("abc#def" in x for x in map(" ".join, sent))


def test_start_records_the_flow_only_when_allowed(portal, monkeypatch):
    mod, home, _ = portal
    mod.FIRST_BOOT_MARKER.write_text("1")

    async def fake_machine(pane):
        return {"started": True}
    monkeypatch.setattr(mod, "_run_auth_state_machine", fake_machine)

    async def pane():
        return "%0"
    monkeypatch.setattr(mod, "_find_primary_pane_async", pane)
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [(1, time.time() - 600)])
    c = TestClient(mod.app)
    assert c.post("/api/auth/start", headers=H).json()["started"] is False
    assert mod._signin_flow_started_at is None
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [])
    assert c.post("/api/auth/start", headers=H).json()["started"] is True
    assert mod._signin_flow_started_at is not None
    _creds(home, accessToken="a", expiresAt=_now_ms() + 3600_000)
    assert _status(mod)["authenticated"] is True
    assert mod._signin_flow_started_at is None  # sign-in done: its claude is the AI now
