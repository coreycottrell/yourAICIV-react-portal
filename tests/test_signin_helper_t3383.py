"""Sign-in without touching the AI (t3383).

A newborn (never signed in, no AI running) keeps main's first sign-in flow.
Every other CIV signs in through the portal's own helper tmux session, so the
AI's pane never gets a key and no kill reaches the AI. These tests run with a
guard that fails on any real tmux send-keys / pkill / os.kill.
"""
import asyncio
import importlib
import json
import os
import subprocess
import sys
import time

import pytest
from starlette.testclient import TestClient

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
_REAL_KILL = os.kill


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
    mod._real_turn_cache.clear()
    mod._claude_processes_sync_orig = mod._claude_processes_sync
    mod._claude_processes_sync = lambda: []   # never the host's real Claude
    calls = []
    real_run = subprocess.run

    def guarded_run(cmd, *a, **kw):
        flat = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
        calls.append(flat)
        if "send-keys" in flat or "pkill" in flat:
            raise AssertionError(f"must not type or kill: {flat}")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "tmux":
            return subprocess.CompletedProcess(cmd, 1, "", "no server running")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", guarded_run)
    monkeypatch.setattr(os, "kill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("os.kill called")))
    return mod, home, calls


def _primary_dir(mod):
    d = mod._primary_project_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _turn(**over):
    e = {"type": "assistant", "timestamp": "2026-09-01T00:00:00Z", "entrypoint": "cli", "isSidechain": False,
         "message": {"role": "assistant", "model": "claude-opus-5", "content": [{"type": "text", "text": "hi"}]}}
    e.update(over)
    return e


def _expired(home):
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": "", "expiresAt": int(time.time() * 1000) - 3600_000}}))


def test_argv_is_signin_only():
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert ps._argv_is_signin_only(["claude", "/login"])
    assert ps._argv_is_signin_only(["/x/claude", "auth", "login"])
    assert ps._argv_is_signin_only(["claude", "/opt/fake.py", "/login"])
    assert not ps._argv_is_signin_only(["claude", "--dangerously-skip-permissions"])
    assert not ps._argv_is_signin_only(["claude", "please /login later"])
    assert not ps._argv_is_signin_only(["claude"])


def test_newborn_mode_only_without_markers_turns_or_ai(portal, monkeypatch):
    mod, home, _ = portal
    assert mod._signin_mode_sync() == "newborn"
    # a bare `claude /login` (main's own first sign-in) keeps it a newborn
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [os.getpid()])
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "/login"])
    assert mod._signin_mode_sync() == "newborn"
    # a working AI makes it a helper sign-in
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "--dangerously-skip-permissions"])
    assert mod._signin_mode_sync() == "helper"
    # unreadable argv or an unreadable scan counts as an AI
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: [])
    assert mod._signin_mode_sync() == "helper"
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: None)
    assert mod._signin_mode_sync() == "helper"


def test_markerless_civ_with_real_turn_is_established(portal):
    """Exposure (a): no first-boot / evolution marker, but the primary has worked."""
    mod, home, _ = portal
    d = _primary_dir(mod)
    (d / "headless.jsonl").write_text(json.dumps(_turn(entrypoint="sdk-cli")) + "\n")
    (d / "sub.jsonl").write_text(json.dumps(_turn(isSidechain=True)) + "\n")
    assert not mod._civ_is_established() and mod._signin_mode_sync() == "newborn"
    (d / "primary.jsonl").write_text('{"type":"user"}\n' + json.dumps(_turn()) + "\n")
    assert mod._civ_is_established() and mod._signin_mode_sync() == "helper"


def test_process_scan_is_uid_agnostic(portal, monkeypatch):
    """Exposure (b): a portal under another uid (root) still sees the AI."""
    mod, home, _ = portal
    ai = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
    try:
        time.sleep(0.3)
        monkeypatch.setattr(os, "getuid", lambda: 4242)
        monkeypatch.setattr(os, "geteuid", lambda: 4242)
        assert ai.pid in (mod._claude_processes_sync_orig() or [])
    finally:
        _REAL_KILL(ai.pid, 9)
        ai.wait()


def test_status_payload_names_helper_mode_only_when_not_newborn(portal, monkeypatch):
    mod, home, _ = portal
    _expired(home)
    c = TestClient(mod.app)
    s = c.get("/api/auth/status", headers=H).json()
    assert s["authenticated"] is False and "signin_mode" not in s   # newborn payload unchanged
    mod.FIRST_BOOT_MARKER.write_text("1")
    s = c.get("/api/auth/status", headers=H).json()
    assert s["signin_mode"] == "helper"


def test_non_newborn_endpoints_never_touch_the_primary_pane(portal, monkeypatch):
    mod, home, calls = portal
    _expired(home)
    mod.FIRST_BOOT_MARKER.write_text("1")
    c = TestClient(mod.app)
    assert c.post("/api/auth/prewarm", headers=H).json() == {"status": "skipped", "mode": "helper"}
    r = c.post("/api/auth/code", headers=H, json={"code": "abc#def"}).json()
    assert r["mode"] == "helper" and "error" in r and not r.get("injected")
    assert c.get("/api/auth/url", headers=H).json() == {"url": None, "ready": False}
    assert not any("send-keys" in x or "capture-pane" in x or "pkill" in x for x in calls), calls


def test_start_on_non_newborn_goes_to_the_helper(portal, monkeypatch):
    mod, home, calls = portal
    mod.EVOLUTION_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    mod.EVOLUTION_DONE_MARKER.touch()
    seen = {}

    async def fake_helper():
        seen["helper"] = True
        return {"started": True, "url": "https://claude.ai/oauth/authorize?x=1&state=s", "mode": "helper"}

    async def boom(pane):
        raise AssertionError("main's primary-pane flow must not run")

    monkeypatch.setattr(mod, "_run_signin_helper", fake_helper)
    monkeypatch.setattr(mod, "_run_auth_state_machine", boom)
    r = TestClient(mod.app).post("/api/auth/start", headers=H).json()
    assert seen == {"helper": True} and r["mode"] == "helper"


def test_first_boot_refuses_a_markerless_civ_that_has_worked(portal, monkeypatch):
    mod, home, calls = portal
    (_primary_dir(mod) / "p.jsonl").write_text(json.dumps(_turn()) + "\n")
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r == {"status": "skipped_not_newborn"}
    assert not mod.FIRST_BOOT_MARKER.exists()
    assert not any("send-keys" in x or "pkill" in x for x in calls)


def test_newborn_flow_rechecks_before_every_key_and_kill(portal, monkeypatch):
    """Exposure (c): an AI that appears after the click stops the flow at the next key."""
    mod, home, calls = portal
    modes = iter(["newborn"] + ["helper"] * 50)
    monkeypatch.setattr(mod, "_signin_mode_sync", lambda: next(modes))
    r = TestClient(mod.app).post("/api/auth/start", headers=H).json()
    assert r["started"] is False and r["live_session"] is True
    assert not any("send-keys" in x or "pkill" in x for x in calls), calls

    async def under_guard(coro_fn):
        tok = mod._newborn_flow_guard.set(True)
        try:
            return await coro_fn()
        finally:
            mod._newborn_flow_guard.reset(tok)

    monkeypatch.setattr(mod, "_signin_mode_sync", lambda: "helper")
    with pytest.raises(mod._SigninGuardStop):
        asyncio.run(under_guard(lambda: mod._run_subprocess_async(["tmux", "send-keys", "-t", "%1", "x"])))
    with pytest.raises(mod._SigninGuardStop):
        asyncio.run(under_guard(mod._kill_claude_process))
    # outside the flow nothing changes (the guard is off)
    asyncio.run(mod._run_subprocess_async(["tmux", "has-session", "-t", "x"]))


def test_newborn_code_is_still_typed_into_the_primary_pane(portal, monkeypatch):
    """The newborn path is main's: the code goes to the primary pane."""
    mod, home, calls = portal
    typed = []

    async def fake_async(cmd, timeout=5, check=False):
        if mod._newborn_flow_guard.get() and mod._is_send_keys(cmd):
            mod._newborn_guard_check("key")
        typed.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    async def primary():
        return "%7"

    monkeypatch.setattr(mod, "_run_subprocess_async", fake_async)
    monkeypatch.setattr(mod, "_find_primary_pane_async", primary)
    r = TestClient(mod.app).post("/api/auth/code", headers=H, json={"code": "CODE#STATE"}).json()
    assert r == {"injected": True}
    assert typed == [["tmux", "send-keys", "-t", "%7", "-l", "CODE#STATE"], ["tmux", "send-keys", "-t", "%7", "Enter"]]


def test_descendants_scope(portal):
    mod, home, _ = portal
    p = subprocess.Popen(["bash", "-c", "sleep 30 & wait"])
    try:
        time.sleep(0.3)
        tree = mod._descendants_sync(p.pid)
        assert p.pid in tree and len(tree) >= 2 and os.getpid() not in tree
    finally:
        _REAL_KILL(p.pid, 9)
        p.wait()


def test_helper_kill_code_never_uses_pkill():
    import inspect
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    for fn in (ps._close_signin_helper, ps._run_signin_helper, ps._signin_helper_submit_code,
               ps._signin_helper_send):
        src = inspect.getsource(fn)
        assert "pkill" not in src and "_kill_claude_process" not in src, fn.__name__


def test_login_process_counts_as_ai_once_signed_in(portal, monkeypatch):
    """A `claude /login` that already has a valid sign-in is a working session."""
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [os.getpid()])
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "/login"])
    assert mod._ai_processes_sync() == []
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "expiresAt": int(time.time() * 1000) + 3600_000}}))
    assert mod._ai_processes_sync() == [os.getpid()]
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "auth", "login"])
    assert mod._ai_processes_sync() == []      # the helper command exits after sign-in


def test_live_session_ignores_the_helper_process(portal, monkeypatch):
    mod, home, _ = portal
    _expired(home)
    mod.FIRST_BOOT_MARKER.write_text("1")
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [os.getpid()])
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["/x/claude", "auth", "login"])
    s = TestClient(mod.app).get("/api/auth/status", headers=H).json()
    assert s["live_session"] is False and s["signin_mode"] == "helper"


def _helper_pane_fn(mod, pane_pid, dead=False):
    async def fn():
        return {"pane_id": "%9", "pane_pid": pane_pid, "dead": dead}
    return fn


def test_close_helper_never_signals_a_dead_pane_or_non_signin_root(portal, monkeypatch):
    """Review r0 #2: a stale pane_pid may belong to another process now."""
    mod, home, calls = portal
    ai = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
    try:
        time.sleep(0.3)
        async def no_primary():
            return set()
        monkeypatch.setattr(mod, "_primary_pane_pids", no_primary)
        for dead in (True, False):   # dead pane; live pane whose root is an AI, not a sign-in
            monkeypatch.setattr(mod, "_signin_helper_pane", _helper_pane_fn(mod, ai.pid, dead))
            asyncio.run(mod._close_signin_helper("test"))   # os.kill is a hard failure in this fixture
        assert ai.poll() is None
    finally:
        _REAL_KILL(ai.pid, 9)
        ai.wait()


def test_close_helper_kills_only_the_helper_tree(portal, monkeypatch):
    mod, home, calls = portal
    helper = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30", "auth", "login"])
    # argv of the helper root: make it read as `claude auth login`
    other = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
    killed = []
    try:
        time.sleep(0.3)
        monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "auth", "login"] if pid == helper.pid
                            else (["claude"] if pid == other.pid else None))

        async def no_primary():
            return set()
        monkeypatch.setattr(mod, "_primary_pane_pids", no_primary)
        monkeypatch.setattr(mod, "_signin_helper_pane", _helper_pane_fn(mod, helper.pid))
        monkeypatch.setattr(os, "kill", lambda pid, sig: (killed.append(pid), _REAL_KILL(pid, sig)))
        asyncio.run(mod._close_signin_helper("test"))
        assert killed and set(killed) == {helper.pid} and other.poll() is None
    finally:
        for p in (helper, other):
            try:
                _REAL_KILL(p.pid, 9)
            except ProcessLookupError:
                pass
            p.wait()


def test_established_by_a_turn_in_another_project_dir(portal):
    """Review r1 #1: an AI launched from another folder has worked; never a newborn."""
    mod, home, _ = portal
    other = mod._PROJECTS_DIR / "-home-aiciv-civ"
    other.mkdir(parents=True)
    (other / "s.jsonl").write_text(json.dumps(_turn()) + "\n")
    assert mod._civ_is_established()
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r == {"status": "skipped_not_newborn"} and not mod.FIRST_BOOT_MARKER.exists()


def test_established_despite_many_error_only_sessions_and_a_late_first_turn(portal):
    """Review r1 #2/#3: 30 newer error-only sessions, and the real turn after 1.5MB."""
    mod, home, _ = portal
    d = _primary_dir(mod)
    big = json.dumps({"type": "user", "message": {"content": "x" * (1536 * 1024)}})
    old = d / "old.jsonl"
    old.write_text(big + "\n" + json.dumps(_turn()) + "\n")
    os.utime(old, (1_000_000, 1_000_000))
    for i in range(30):
        (d / f"err{i}.jsonl").write_text(json.dumps(_turn(isApiErrorMessage=True)) + "\n")
    assert mod._primary_has_real_turn()


def test_auth_login_detection_failure_is_not_cached(portal, monkeypatch):
    mod, home, _ = portal
    monkeypatch.setattr(mod, "_find_claude_binary", lambda: "/x/claude")
    outs = iter(["", "Commands:\n  login [options]   Sign in\n"])

    async def fake_out(cmd, timeout=5):
        return next(outs)
    monkeypatch.setattr(mod, "_run_subprocess_output", fake_out)
    argv1, _ = asyncio.run(mod._signin_helper_argv())
    argv2, _ = asyncio.run(mod._signin_helper_argv())
    assert argv1 == ["/x/claude", "/login"] and argv2 == ["/x/claude", "auth", "login"]
    assert mod._signin_helper_has_auth_login is True


def test_guard_also_covers_clear_history_and_resize(portal):
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert ps._is_send_keys(["tmux", "clear-history", "-t", "%1"])
    assert ps._is_send_keys(["tmux", "resize-window", "-t", "%1", "-x", "500"])
    assert ps._is_send_keys(["tmux", "send-keys", "-t", "%1", "x"])
    assert not ps._is_send_keys(["tmux", "capture-pane", "-p", "-t", "%1"])
    assert ps._is_send_keys(["tmux", "-L", "portal-signin", "send-keys", "-t", "%1", "x"])
    assert ps._is_send_keys(["tmux", "-S", "/x/sock", "resize-window", "-t", "%1"])
    assert not ps._is_send_keys(["tmux", "-L", "send-keys", "list-panes"])   # socket named send-keys
    assert not ps._is_send_keys(["tmux", "capture-pane", "-t", "send-keys"])


def test_newborn_start_closes_a_leftover_helper_first(portal, monkeypatch):
    mod, home, _ = portal
    order = []

    async def fake_close(reason):
        order.append(("close", reason))
        return True

    async def fake_sm(pane):
        order.append(("flow", pane))
        return {"started": True}

    async def primary():
        return "%7"
    monkeypatch.setattr(mod, "_close_signin_helper", fake_close)
    monkeypatch.setattr(mod, "_run_auth_state_machine", fake_sm)
    monkeypatch.setattr(mod, "_find_primary_pane_async", primary)
    assert TestClient(mod.app).post("/api/auth/start", headers=H).json() == {"started": True}
    assert order == [("close", "newborn sign-in"), ("flow", "%7")]


# ---------------------------------------------------------------- round 3 (t3383)

def _fb_fakes(mod, monkeypatch, did):
    async def fake_async(cmd, timeout=5, check=False):
        if mod._newborn_flow_guard.get() and mod._is_send_keys(cmd):
            await mod._newborn_guard_check("key")
        did.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    async def fake_kill(pane=None):
        if mod._newborn_flow_guard.get():
            await mod._newborn_guard_check("kill")
        did.append("kill")

    async def primary():
        return "%7"

    async def no_claude(pane):
        return False

    async def fast_sleep(_s):
        return None

    async def no_adopt(pane, tail):
        did.append(("adopt", tail))

    monkeypatch.setattr(mod, "_run_subprocess_async", fake_async)
    monkeypatch.setattr(mod, "_kill_claude_process", fake_kill)
    monkeypatch.setattr(mod, "_find_primary_pane_async", primary)
    monkeypatch.setattr(mod, "_is_claude_running_async", no_claude)
    monkeypatch.setattr(mod, "_adopt_newborn_launch", no_adopt)
    monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)


def test_first_boot_refuses_when_any_working_ai_runs(portal, monkeypatch):
    """Item 1: no marker and no transcript, but a working AI process runs
    (a stray, or an AI whose turns cannot be found): never kill it."""
    mod, home, calls = portal
    did = []
    _fb_fakes(mod, monkeypatch, did)
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [4242])
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "--dangerously-skip-permissions"])
    c = TestClient(mod.app)
    assert c.post("/api/evolution/first-boot", headers=H).json() == {"status": "skipped_ai_running"}
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: None)      # cannot tell = refuse
    assert c.post("/api/evolution/first-boot", headers=H).json() == {"status": "skipped_ai_running"}
    assert did == [] and not mod.FIRST_BOOT_MARKER.exists()


def test_first_boot_fires_when_the_only_claude_is_the_flows_own(portal, monkeypatch):
    """The newborn path: the sign-in Claude the first sign-in launched (now signed
    in) is the flow's own, so main's awakening still runs; the guard re-checks
    every key and kill."""
    mod, home, calls = portal
    did = []
    _fb_fakes(mod, monkeypatch, did)
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "expiresAt": int(time.time() * 1000) + 3600_000}}))
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [4242])
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude", "/login"])
    assert mod._foreign_ai_sync() == [4242]                              # not adopted: foreign
    monkeypatch.setattr(mod, "_newborn_own_pids_sync", lambda: {4242})
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r["status"] == "fired" and mod.FIRST_BOOT_MARKER.exists()
    assert did[0] == "kill" and ("adopt", "--dangerously-skip-permissions") in did
    assert any(isinstance(x, list) and "--dangerously-skip-permissions" in " ".join(x) for x in did)


def test_first_boot_stops_at_the_next_key_when_an_ai_appears(portal, monkeypatch):
    mod, home, calls = portal
    did = []
    _fb_fakes(mod, monkeypatch, did)
    state = {"n": 0}

    def procs():
        state["n"] += 1
        return [] if state["n"] <= 2 else [4242]     # AI appears after the kill
    monkeypatch.setattr(mod, "_claude_processes_sync", procs)
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude"])
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r == {"status": "stopped_ai_running"}
    assert did == ["kill"], did                      # no key reached the pane
    assert not mod.FIRST_BOOT_MARKER.exists()        # a stopped first boot can re-check later


def test_own_claude_is_adopted_only_as_a_child_of_the_pane_shell(portal):
    mod, home, calls = portal
    py = 'exec -a claude python3 -c "import time; time.sleep(30)"'
    # the /login Claude runs under a wrapper shell, not as a direct child (review r3 #3)
    shell = subprocess.Popen(["bash", "-c", f"(bash -c '{py} /login; true') & ({py} --other) & wait"])
    other = subprocess.Popen(["bash", "-c", f"{py} /login"])
    try:
        time.sleep(0.5)
        since = mod._boot_ticks_now() - 10 * os.sysconf("SC_CLK_TCK")
        kids = mod._adopt_children_sync(shell.pid, "/login", since)
        assert len(kids) == 1 and other.pid not in kids
        assert mod._proc_argv(kids[0])[-1] == "/login"
        assert set(kids) <= mod._newborn_own_pids_sync()
        mod._newborn_own.clear()
        assert mod._adopt_children_sync(shell.pid, "/login", mod._boot_ticks_now() + 1000) == []
    finally:
        for pid in sorted(mod._descendants_sync(shell.pid) | {other.pid}, reverse=True):
            try:
                _REAL_KILL(pid, 9)
            except ProcessLookupError:
                pass
        shell.wait()
        other.wait()


def test_kill_is_scoped_to_the_primary_pane_tree(portal, monkeypatch):
    """Item 4: no container-wide pkill; only Claude processes under the pane."""
    mod, home, calls = portal
    pane_shell = subprocess.Popen(["bash", "-c", "(exec -a claude sleep 30) & (exec -a sleep sleep 30) & wait"])
    outside = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"])
    killed = []
    try:
        time.sleep(0.4)
        monkeypatch.setattr(os, "kill", lambda pid, sig: (killed.append((pid, sig)), _REAL_KILL(pid, sig)))
        k = mod._kill_claude_in_tree_sync(pane_shell.pid)
        assert len(k) == 1 and outside.poll() is None and pane_shell.poll() is None
        assert [s for _, s in killed] == [15]
        assert mod._proc_argv(outside.pid)[0] == "claude"
    finally:
        for pid in sorted(mod._descendants_sync(pane_shell.pid) | {outside.pid}, reverse=True):
            try:
                _REAL_KILL(pid, 9)
            except ProcessLookupError:
                pass
        pane_shell.wait()
        outside.wait()


def test_kill_claude_process_has_no_pkill():
    import inspect
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert "pkill" not in inspect.getsource(ps._kill_claude_process)
    assert "pkill" not in inspect.getsource(ps._kill_claude_in_tree_sync)


def test_is_claude_running_reads_the_pane_command(portal, monkeypatch):
    """Item 4: main read .stdout of a call that discards stdout (always False)."""
    mod, home, calls = portal
    outs = {"%1": "claude\n", "%2": "node\n", "%3": "bash\n", "%4": ""}

    async def fake_out(cmd, timeout=5):
        return outs[cmd[cmd.index("-t") + 1]]
    monkeypatch.setattr(mod, "_run_subprocess_output", fake_out)
    got = [asyncio.run(mod._is_claude_running_async(p)) for p in ("%1", "%2", "%3", "%4")]
    assert got == [True, True, False, False]


def test_helper_commands_use_the_helpers_own_tmux_server():
    """Item 2: every helper tmux command carries -L portal-signin; the AI's
    session resolver has no helper special case."""
    import inspect
    sys.modules.pop("portal_server", None)
    import portal_server as ps
    assert ps._HELPER_TMUX == ["tmux", "-L", "portal-signin"]
    for fn in (ps._signin_helper_pane, ps._signin_helper_send, ps._close_signin_helper,
               ps._run_signin_helper, ps._signin_helper_submit_code):
        src = inspect.getsource(fn)
        assert '["tmux"' not in src and "_find_primary_pane" not in src or fn is ps._close_signin_helper, fn.__name__
    assert "SIGNIN_HELPER" not in inspect.getsource(ps.get_tmux_session)


def _submit_fakes(mod, monkeypatch, frames, write_at=None, exit_at=None):
    """frames: list of screen texts per poll; creds rewritten at poll write_at;
    helper pane gone from poll exit_at."""
    n = {"poll": 0, "closed": None}
    home_creds = mod.CREDENTIALS_FILE

    async def pane():
        if exit_at is not None and n["poll"] >= exit_at:
            return None
        return {"pane_id": "%1", "pane_pid": 1, "dead": False}

    async def send(*a, **k):
        return None

    async def screen(p, tmux_cmd=("tmux",)):
        return "unknown", frames[min(n["poll"], len(frames) - 1)]

    async def tick(_s):
        n["poll"] += 1
        if write_at is not None and n["poll"] == write_at:
            home_creds.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "new", "expiresAt": int(time.time() * 1000) + 3600_000}}))

    async def close(reason):
        n["closed"] = (n["poll"], reason)
        return True
    monkeypatch.setattr(mod, "_signin_helper_pane", pane)
    monkeypatch.setattr(mod, "_signin_helper_send", send)
    monkeypatch.setattr(mod, "_detect_auth_screen", screen)
    monkeypatch.setattr(mod.asyncio, "sleep", tick)
    monkeypatch.setattr(mod, "_close_signin_helper", close)
    return n


def test_submit_waits_for_the_credentials_after_login_successful(portal, monkeypatch):
    """Item 3: 'Login successful' shows before the file is written: keep waiting,
    never close the helper before the rewrite; then signed in."""
    mod, home, calls = portal
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code", "Login successful. Press Enter"], write_at=12)
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "signed_in" and n["closed"][1] == "signed in" and n["closed"][0] >= 12


def test_submit_ignores_a_rewrite_before_the_helper_finished(portal, monkeypatch):
    """Review r3 #9a: the AI's own token refresh while the helper still waits on
    the code is not this sign-in."""
    mod, home, calls = portal
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code"], write_at=2)       # helper never says done
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "pending" and n["closed"] is None


def test_submit_write_then_exit_is_signed_in(portal, monkeypatch):
    """Review r3 #9b: the helper writes and exits in the same poll: signed in."""
    mod, home, calls = portal
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code"], write_at=5, exit_at=5)
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "signed_in"


def test_submit_fails_only_after_a_bounded_wait(portal, monkeypatch):
    mod, home, calls = portal
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Login successful. Press Enter"])     # never writes
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "failed" and n["closed"][1] == "sign-in failed" and n["closed"][0] >= 30
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code"], exit_at=3)            # helper exited, no file
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "failed"
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code"])                        # still exchanging
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "pending" and n["closed"] is None


# ------------------------------------------------ final review of round 3 (t3383)

def test_own_record_survives_a_portal_restart(portal):
    mod, home, calls = portal
    py = 'exec -a claude python3 -c "import time; time.sleep(30)"'
    shell = subprocess.Popen(["bash", "-c", f"({py} /login) & wait"])
    try:
        time.sleep(0.5)
        kids = mod._adopt_children_sync(shell.pid, "/login", mod._boot_ticks_now() - 1000)
        assert len(kids) == 1 and mod._NEWBORN_OWN_FILE.exists()
        sys.modules.pop("portal_server", None)          # "restart" the portal
        mod2 = importlib.import_module("portal_server")
        assert mod2._newborn_own == {}
        assert kids[0] in mod2._newborn_own_pids_sync()
        mod2._newborn_own_clear()
        assert not mod2._NEWBORN_OWN_FILE.exists() and mod2._newborn_own_pids_sync() == set()
    finally:
        for pid in sorted(mod._descendants_sync(shell.pid), reverse=True):
            try:
                _REAL_KILL(pid, 9)
            except ProcessLookupError:
                pass
        shell.wait()


def test_no_adoption_when_boot_time_cannot_be_read(portal, monkeypatch):
    mod, home, calls = portal
    monkeypatch.setattr(mod, "_boot_ticks_now", lambda: None)
    seen = []

    async def out(cmd, timeout=5):
        seen.append(cmd)
        return "123"
    monkeypatch.setattr(mod, "_run_subprocess_output", out)
    asyncio.run(mod._adopt_newborn_launch("%1", "/login"))
    assert seen == [] and mod._newborn_own == {}


def test_first_boot_leaves_nothing_exempt_afterwards(portal, monkeypatch):
    """Final review #3: after first boot (fired or stopped), whatever it launched
    is a working AI, never the flow's own."""
    mod, home, calls = portal
    did = []
    _fb_fakes(mod, monkeypatch, did)
    mod._newborn_own[4242] = 1
    mod._newborn_own_save()
    monkeypatch.setattr(mod, "_claude_processes_sync", lambda: [])
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r["status"] == "fired"
    assert mod._newborn_own == {} and not mod._NEWBORN_OWN_FILE.exists()


def test_one_failed_pane_lookup_is_not_an_exit(portal, monkeypatch):
    """Final review #5: a single slow tmux call must not fail and kill a helper
    that is still exchanging the code."""
    mod, home, calls = portal
    _expired(home)
    n = _submit_fakes(mod, monkeypatch, ["Paste code"], write_at=None)
    real_pane = mod._signin_helper_pane
    seq = {"i": 0}

    async def flaky():
        seq["i"] += 1
        return None if seq["i"] == 3 else await real_pane()
    monkeypatch.setattr(mod, "_signin_helper_pane", flaky)
    r = asyncio.run(mod._signin_helper_submit_code("c#s"))
    assert r["result"] == "pending" and n["closed"] is None



def test_first_boot_stopped_before_its_kill_keeps_the_sign_in_record(portal, monkeypatch):
    """Delta review #1: a stop at the kill check keeps the record, so a retry can
    still recognise the sign-in Claude."""
    mod, home, calls = portal
    did = []
    _fb_fakes(mod, monkeypatch, did)
    me = os.getpid()                                   # a live process stands in for the sign-in Claude
    mod._newborn_own[me] = mod._proc_start_ticks(me)
    mod._newborn_own_save()
    state = {"n": 0}

    def procs():
        state["n"] += 1
        return [] if state["n"] == 1 else [777]      # a foreign AI appears before the kill
    monkeypatch.setattr(mod, "_claude_processes_sync", procs)
    monkeypatch.setattr(mod, "_proc_argv", lambda pid: ["claude"])
    r = TestClient(mod.app).post("/api/evolution/first-boot", headers=H).json()
    assert r == {"status": "stopped_ai_running"} and did == []
    assert me in mod._newborn_own and mod._NEWBORN_OWN_FILE.exists()
    assert not mod.FIRST_BOOT_MARKER.exists()


def test_own_record_save_is_atomic_and_clear_survives_unlink_failure(portal, monkeypatch):
    mod, home, calls = portal
    mod._newborn_own[1] = 2
    mod._newborn_own_save()
    assert json.loads(mod._NEWBORN_OWN_FILE.read_text()) == {"1": 2}
    assert not mod._NEWBORN_OWN_FILE.with_name(mod._NEWBORN_OWN_FILE.name + ".tmp").exists()
    real_unlink = type(mod._NEWBORN_OWN_FILE).unlink

    def bad_unlink(self, *a, **k):
        if self == mod._NEWBORN_OWN_FILE:
            raise PermissionError("read-only")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(type(mod._NEWBORN_OWN_FILE), "unlink", bad_unlink)
    mod._newborn_own_clear()
    assert json.loads(mod._NEWBORN_OWN_FILE.read_text()) == {}
    mod._newborn_own_load()
    assert mod._newborn_own == {}
