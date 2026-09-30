"""Regression tests for the independent review of the Reconnect Claude branch
(Witness ticket 3383, review round 2). One block per finding:

  a  closing the dialog STOPS the server sign-in flow; one flow per pane
  b  the auth code is typed ONLY into Claude's "Paste code here" prompt
  c  blocker screens are fresh-checked (conversation text never earns a key)
  d  never `pkill -f claude`; only the /login Claude this flow launched, by PID
  e  an idle CIV with a refresh token is not "signed out"
  f  nothing is typed into a session that is mid-turn; no blind Escape
  h  a reconnect counts only a REAL new sign-in (/api/auth/verify)
  i  the Status page's engine-account row has data again
  j  the transcript scan reads only appended bytes

Every test here FAILS on a8d2edd (the reviewed commit) and passes after the
fix. SAFETY: no test ever runs a `pkill`-based _kill_claude_process on this
host — the autouse guard below replaces such an implementation with a recorder
before any test body runs. tmux runs on a private socket; HOME is a temp dir.
"""
import asyncio
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

import portal_server as ps

ACCESS = "sk-ant-oat01-SECRET-ACCESS-TOKEN-do-not-leak"
REFRESH = "sk-ant-ort01-SECRET-REFRESH-TOKEN-do-not-leak"
HEADERS = {"Authorization": f"Bearer {ps.BEARER_TOKEN}"}
_ORIG_SIGNAL_PIDS = getattr(ps, "_signal_pids", None)

LOGIN_PICKER = (" Select login method:\n\n"
                " ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise\n"
                "   2. Anthropic Console account · API usage billing\n")
CODE_PROMPT = (" Browser didn't open? Use the url below to sign in:\n\n"
               " https://claude.ai/oauth/authorize?code=true&client_id=x&state=abc\n\n"
               " Paste code here if prompted >\n")
IDLE_PROMPT = " ⏺ Done — the report is saved.\n\n > \n"
BUSY = " ⏺ Reading files…\n\n ✻ Working… (12s · ↑ 1.2k tokens · esc to interrupt)\n\n > \n"
CODE = "Abc123-_xyz.456#state789"


def _pkill_based(fn) -> bool:
    try:
        return "pkill" in inspect.getsource(fn) and "_signal_pids" not in inspect.getsource(fn)
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _host_safety(monkeypatch):
    """Never let a pkill-based kill (the reviewed code) run on this host, and
    record (not deliver) signals from the new PID-scoped kill by default."""
    killed = []
    if _pkill_based(ps._kill_claude_process):
        async def recorder(*a, **k):
            killed.append(("pkill-based kill was CALLED", a, k))
            return None
        monkeypatch.setattr(ps, "_kill_claude_process", recorder)
    if hasattr(ps, "_signal_pids"):
        async def fake_signal(pids, sig):
            killed.append((sorted(pids), sig))
        monkeypatch.setattr(ps, "_signal_pids", fake_signal)
    monkeypatch.setattr(ps, "_save_portal_message", lambda *a, **k: None)
    return killed


class FakePane:
    """A scripted primary pane: records every key, serves scripted screens."""

    def __init__(self, monkeypatch, state="claude", visible=IDLE_PROMPT, full=None):
        self.state, self.visible = state, visible
        self.full = full  # None -> same as visible
        self.keys = []
        pane = self

        class _OK:
            returncode = 0
            stdout = None

        async def run_async(cmd, timeout=5, check=False):
            if len(cmd) > 1 and cmd[0] == "tmux" and cmd[1] == "send-keys":
                pane.keys.append(cmd[4:] if cmd[3:4] else cmd[3:])
            return _OK()

        async def run_output(cmd, timeout=5):
            if "capture-pane" in cmd:
                if "-S" in cmd:
                    return pane.full if pane.full is not None else pane.visible
                return pane.visible
            if "display-message" in cmd:
                return "100"
            return ""

        async def pane_state(_pane):
            return pane.state

        async def find_pane():
            return "%1"
        monkeypatch.setattr(ps, "_run_subprocess_async", run_async)
        monkeypatch.setattr(ps, "_run_subprocess_output", run_output)
        monkeypatch.setattr(ps, "_pane_process_state", pane_state)
        monkeypatch.setattr(ps, "_find_primary_pane_async", find_pane)
        if hasattr(ps, "_auth_flow"):
            monkeypatch.setattr(ps, "_auth_flow", None)
        if hasattr(ps, "_auth_code_submission"):
            monkeypatch.setattr(ps, "_auth_code_submission", None)
        monkeypatch.setattr(ps, "_captured_oauth_url", None)

    def typed(self):
        return [k for k in self.keys]


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ps.app), base_url="http://portal")


def _run(coro, timeout=30):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


# ---------------------------------------------------------------------------
# (a) close STOPS the flow; only one flow per pane
# ---------------------------------------------------------------------------

def test_a_close_stops_the_running_signin_flow(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")  # a URL never appears

    async def scenario():
        async with _client() as c:
            start = asyncio.create_task(c.post("/api/auth/start", headers=HEADERS))
            await asyncio.sleep(1.0)
            keys_before_close = len(pane.keys)
            close = await c.post("/api/auth/close", headers=HEADERS)
            # The start request must END promptly once closed (it used to run
            # on for minutes and keep typing).
            r = await asyncio.wait_for(start, timeout=5)
            await asyncio.sleep(1.5)
            return keys_before_close, close.json(), r.json()

    keys_before_close, close, start = _run(scenario())
    assert start.get("started") is False and start.get("cancelled") is True, start
    assert pane.keys[:2] == [["-l", "/login"], ["Enter"]], pane.keys
    assert len(pane.keys) == keys_before_close, f"keys pressed after close: {pane.keys[keys_before_close:]}"
    assert close.get("flow_stopped") is True, close


def test_a_second_start_is_refused_while_a_flow_runs(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")

    async def scenario():
        async with _client() as c:
            first = asyncio.create_task(c.post("/api/auth/start", headers=HEADERS))
            await asyncio.sleep(0.8)
            second = await asyncio.wait_for(c.post("/api/auth/start", headers=HEADERS), timeout=3)
            keys = list(pane.keys)
            await c.post("/api/auth/close", headers=HEADERS)
            await asyncio.wait_for(first, timeout=5)
            return second.json(), keys

    second, keys = _run(scenario())
    assert second.get("started") is False and second.get("in_progress") is True, second
    assert keys == [["-l", "/login"], ["Enter"]], f"a second flow typed into the pane: {keys}"


# ---------------------------------------------------------------------------
# (b) the auth code only goes into the "Paste code here" prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("screen", [
    IDLE_PROMPT,                                             # login screen already closed
    CODE_PROMPT + "\n Login successful. Press Enter to continue…\n",  # already finished
    CODE_PROMPT + "\n > \n",                                 # back at the chat input
    LOGIN_PICKER,                                            # not at the code step
])
def test_b_code_is_refused_when_not_on_the_paste_prompt(monkeypatch, screen):
    pane = FakePane(monkeypatch, visible=screen)

    async def scenario():
        async with _client() as c:
            return await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})
    r = _run(scenario())
    assert r.json().get("injected") is not True, r.json()
    assert pane.keys == [], f"the code was typed into the AI's pane: {pane.keys}"


def test_b_code_is_typed_on_the_paste_prompt(monkeypatch):
    pane = FakePane(monkeypatch, visible=CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            return await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})
    r = _run(scenario())
    assert r.json().get("injected") is True, r.json()
    assert pane.keys == [["-l", CODE], ["Enter"]]


@pytest.mark.parametrize("bad", ["abc def12345", "abcdefgh\nrm -rf ~", "abc\x1b[Adef1234"])
def test_b_malformed_code_is_never_typed(monkeypatch, bad):
    pane = FakePane(monkeypatch, visible=CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            return await c.post("/api/auth/code", headers=HEADERS, json={"code": bad})
    r = _run(scenario())
    assert r.json().get("injected") is not True
    assert pane.keys == []


# ---------------------------------------------------------------------------
# (c) blocker screens are fresh-checked
# ---------------------------------------------------------------------------

CONVERSATION = textwrap.dedent("""
    > the customer survey asked "Would you recommend us?" and got a thumbs up
    ⏺ Noted. Earlier the script said "command not found" and "Connection refused".
    > Do you want to trust the new vendor?
""")


def test_c_blocker_words_already_in_the_conversation_are_not_blockers():
    baseline = ps._auth_screen_baseline(CONVERSATION)
    assert ps._classify_auth_screen(CONVERSATION, baseline) not in (
        "csat_survey", "trust_folder", "update_prompt", "error"), \
        "old conversation text was classified as a blocker"


def test_c_a_NEW_blocker_after_login_is_still_detected():
    baseline = ps._auth_screen_baseline(CONVERSATION)
    later = CONVERSATION + "\n Do you trust the authors of the files in this folder?\n"
    assert ps._classify_auth_screen(later, baseline) == "trust_folder"


def test_c_blocker_not_on_the_visible_screen_gets_no_key(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full=CONVERSATION + IDLE_PROMPT)
    for kind in ("csat_survey", "trust_folder", "update_prompt"):
        _run(ps._dismiss_auth_blocker("%1", kind))
    assert pane.keys == [], f"keys pressed for a blocker that is not on screen: {pane.keys}"


# ---------------------------------------------------------------------------
# (d) never pkill -f claude
# ---------------------------------------------------------------------------

def test_d_no_pkill_command_anywhere_in_the_portal():
    src = Path(ps.__file__).read_text()
    assert not re.search(r'["\']\s*pkill', src), "a pkill command string is still in portal_server.py"


def _proc(ppid, *argv):
    return {"ppid": ppid, "pgrp": 0, "tpgid": 0, "argv": list(argv)}


TABLE = {
    1: _proc(0, "init"),
    100: _proc(1, "bash"),                                    # primary pane root
    200: _proc(100, "claude", "/login"),                      # the /login Claude in the pane
    300: _proc(1, "claude", "--dangerously-skip-permissions"),  # the CIV's live session elsewhere
    400: _proc(1, "/home/aiciv/.local/share/claude/versions/2.1.280"),
}


def _kill_env(monkeypatch, table):
    signals = []

    async def run_output(cmd, timeout=5):
        if "display-message" in cmd:
            return "100"
        signals.append(("SUBPROCESS", cmd))  # e.g. a pkill: recorded, never run
        return ""
    monkeypatch.setattr(ps, "_run_subprocess_output", run_output)
    monkeypatch.setattr(ps, "_read_proc_table", lambda: table)
    if hasattr(ps, "_signal_pids"):
        async def fake_signal(pids, sig):
            signals.append((sorted(pids), int(sig)))
        monkeypatch.setattr(ps, "_signal_pids", fake_signal)
    return signals


def test_d_kill_touches_only_the_claude_in_this_pane(monkeypatch):
    signals = _kill_env(monkeypatch, TABLE)
    fn = ps._kill_claude_process
    assert "pane" in inspect.signature(fn).parameters, "kill is not scoped to a pane (pkill -f)"
    killed = _run(fn("%1"))
    assert killed == [200]
    assert all(s[0] == [200] for s in signals), signals
    assert not any(s[0] == "SUBPROCESS" for s in signals), signals


def test_d_kill_refuses_when_it_cannot_identify_a_pane_claude(monkeypatch):
    table = {k: v for k, v in TABLE.items() if k != 200}
    signals = _kill_env(monkeypatch, table)
    assert "pane" in inspect.signature(ps._kill_claude_process).parameters
    assert _run(ps._kill_claude_process("%1")) == []
    assert signals == []


def test_d_kill_excludes_pids_that_existed_before_the_launch(monkeypatch):
    signals = _kill_env(monkeypatch, TABLE)
    assert "exclude" in inspect.signature(ps._kill_claude_process).parameters
    assert _run(ps._kill_claude_process("%1", exclude={200, 300})) == []
    assert signals == []


def test_d_native_versioned_binary_is_recognised_as_claude():
    assert ps._argv_is_claude(["/home/aiciv/.local/share/claude/versions/2.1.280"]) is True
    assert ps._argv_is_claude(["/usr/lib/python3/2.1.280"]) is False


needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None or not sys.platform.startswith("linux"),
                                reason="needs tmux + /proc")

STUCK_LOGIN = textwrap.dedent(r'''
    import os, sys, tty, time
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    os.write(1, b"\x1b[2J\x1b[H Select login method:\r\n \xe2\x9d\xaf 1. Claude account with subscription\r\n")
    while True:
        b = os.read(fd, 16)
        if not b:
            break
        if b[:1] in (b"\r", b"\n"):
            os.write(1, b"\r\n Opening browser to sign in...\r\n")   # ...and never a URL
''')
LIVE_CIV = textwrap.dedent(r'''
    import time
    while True:
        time.sleep(1)
''')


@needs_tmux
def test_d_retry_stops_only_the_login_claude_it_launched_and_the_live_one_survives(tmp_path, monkeypatch):
    """By effect, on a private tmux server: the CIV's live Claude runs in its
    own pane; the primary pane is at a shell. The flow launches `claude /login`
    (a stuck fake), times out, and on retry must stop ONLY that instance."""
    assert not _pkill_based(ps._kill_claude_process), "reviewed code: kill is `pkill -f claude` (not run here)"
    # the real PID-scoped signal for THIS test (it can only reach our private panes)
    monkeypatch.setattr(ps, "_signal_pids", _ORIG_SIGNAL_PIDS)
    real = shutil.which("tmux")
    sock = tmp_path / "t.sock"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tmux").write_text(f'#!/bin/sh\nexec "{real}" -S "{sock}" "$@"\n')
    (bindir / "tmux").chmod(0o755)
    (tmp_path / "stuck.py").write_text(STUCK_LOGIN)
    (tmp_path / "live.py").write_text(LIVE_CIV)
    (bindir / "claude").write_text(f'#!/bin/bash\nexec -a claude {sys.executable} {tmp_path / "stuck.py"}\n')
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 4.0)
    monkeypatch.setattr(ps, "AUTH_URL_WAIT_TIMEOUT_S", 2.0)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 1)
    tm = str(bindir / "tmux")
    try:
        subprocess.run([tm, "new-session", "-d", "-s", "live", "-x", "200", "-y", "40",
                        f"bash -c 'exec -a claude {sys.executable} {tmp_path / 'live.py'}'"], check=True)
        subprocess.run([tm, "new-session", "-d", "-s", "primary", "-x", "200", "-y", "40",
                        "bash --norc --noprofile"], check=True)
        time.sleep(1.0)
        live_root = int(subprocess.run([tm, "display-message", "-t", "live", "-p", "#{pane_pid}"],
                                       capture_output=True, text=True).stdout.strip())
        live_pids = ps._claude_pids_in_tree(live_root, ps._read_proc_table())
        assert len(live_pids) == 1, "precondition: the live fake Claude is running"
        live_pid = live_pids.pop()
        result = asyncio.run(asyncio.wait_for(ps._run_auth_state_machine("primary"), timeout=60))
        log = "\n".join(result.get("log", []))
        assert "Stopped the sign-in Claude this portal launched" in log, log
        assert Path(f"/proc/{live_pid}").exists(), "the CIV's live Claude was killed"
    finally:
        subprocess.run([real, "-S", str(sock), "kill-server"], capture_output=True)


# ---------------------------------------------------------------------------
# (e) idle CIV with a refresh token is signed in (pending refresh)
# ---------------------------------------------------------------------------

def _status_env(tmp_path, monkeypatch, transcripts=()):
    creds = tmp_path / ".claude" / ".credentials.json"
    monkeypatch.setattr(ps, "CREDENTIALS_FILE", creds)
    FakePane(monkeypatch, visible="we talked about API Error: 401 yesterday\n > ")
    monkeypatch.setattr(ps, "_find_all_project_jsonl", lambda: list(transcripts))
    monkeypatch.setattr(ps, "_engine_is_managed", lambda: False)
    ps._auth_evidence_cache.clear()

    def put(**oauth):
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({"claudeAiOauth": oauth}))

    async def get():
        async with _client() as c:
            r = await c.get("/api/auth/status", headers=HEADERS)
            assert ACCESS not in r.text and REFRESH not in r.text
            return r.json()
    return put, get


def test_e_idle_civ_expired_days_ago_with_refresh_token_is_not_signed_out(tmp_path, monkeypatch):
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=int(time.time() * 1000) - 3 * 86_400_000)
    body = _run(get())
    assert body["authenticated"] is True, body
    assert body["reason"] == "expired_refresh_pending"


def test_e_dead_refresh_grant_still_reads_signed_out(tmp_path, monkeypatch):
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken="", expiresAt=int(time.time() * 1000) - 3 * 86_400_000)
    assert _run(get())["authenticated"] is False


# ---------------------------------------------------------------------------
# (f) nothing typed into a busy session; no blind Escape on retry
# ---------------------------------------------------------------------------

def test_f_start_refuses_to_type_into_a_busy_session(monkeypatch):
    pane = FakePane(monkeypatch, visible=BUSY)

    async def scenario():
        async with _client() as c:
            return await asyncio.wait_for(c.post("/api/auth/start", headers=HEADERS), timeout=5)
    r = _run(scenario())
    assert r.json().get("started") is False and r.json().get("busy") is True, r.json()
    assert pane.keys == [], f"typed into a mid-turn session: {pane.keys}"


def test_f_retry_in_live_session_never_presses_a_blind_escape(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")  # no sign-in screen ever shows
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 1, raising=False)
    _run(ps._run_auth_state_machine("%1"), timeout=10)
    assert ["Escape"] not in pane.keys, f"Escape pressed at the AI's live prompt: {pane.keys}"


def test_f_retry_stops_when_the_ai_starts_a_turn(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 2, raising=False)

    async def scenario():
        task = asyncio.create_task(ps._run_auth_state_machine("%1"))
        await asyncio.sleep(0.6)
        pane.visible = BUSY  # the AI picked up a task while we waited
        return await task
    result = _run(scenario(), timeout=10)
    assert result.get("busy") is True, result
    assert pane.keys == [["-l", "/login"], ["Enter"]], pane.keys


# ---------------------------------------------------------------------------
# (h) a REAL new sign-in, not any expiresAt change
# ---------------------------------------------------------------------------

def test_h_verify_needs_a_fresh_login_success_not_just_new_credentials(tmp_path, monkeypatch):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    now = int(time.time() * 1000)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS, "refreshToken": REFRESH,
                                                   "expiresAt": now + 3_600_000}}))
    monkeypatch.setattr(ps, "CREDENTIALS_FILE", creds)
    pane = FakePane(monkeypatch, visible=CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            r = await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})
            assert r.json().get("injected") is True
            time.sleep(0.02)
            # Claude's own background refresh: new file, new expiresAt, NO sign-in.
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS + "2", "refreshToken": REFRESH,
                                                           "expiresAt": now + 7_200_000}}))
            v1 = (await c.get("/api/auth/verify", headers=HEADERS))
            pane.visible = CODE_PROMPT + CODE + "\n Login successful. Press Enter to continue…\n"
            v2 = (await c.get("/api/auth/verify", headers=HEADERS))
            return v1, v2
    v1, v2 = _run(scenario())
    assert v1.status_code == 200 and v1.json()["confirmed"] is False, v1.text
    assert v2.status_code == 200 and v2.json()["confirmed"] is True, v2.text


def test_h_verify_needs_rewritten_credentials_too(tmp_path, monkeypatch):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS, "refreshToken": REFRESH,
                                                   "expiresAt": int(time.time() * 1000) + 3_600_000}}))
    monkeypatch.setattr(ps, "CREDENTIALS_FILE", creds)
    pane = FakePane(monkeypatch, visible=CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})
            pane.visible = CODE_PROMPT + " Login successful. Press Enter to continue…\n"
            return await c.get("/api/auth/verify", headers=HEADERS)
    v = _run(scenario())
    assert v.status_code == 200 and v.json()["confirmed"] is False, v.text


# ---------------------------------------------------------------------------
# (i) engine-account row
# ---------------------------------------------------------------------------

def test_i_status_reports_the_engine_account_again(tmp_path, monkeypatch):
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=int(time.time() * 1000) + 3_600_000,
        account="owner@example.com")
    assert _run(get())["account"] == "owner@example.com"


def test_i_account_falls_back_to_claude_json_oauth_account(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "dave@example.com"},
                                                   "projects": {}}))
    if hasattr(ps, "_account_label_cache"):
        ps._account_label_cache.clear()
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=int(time.time() * 1000) + 3_600_000)
    body = _run(get())
    assert body["account"] == "dave@example.com", body


# ---------------------------------------------------------------------------
# (j) cheap transcript scan
# ---------------------------------------------------------------------------

def _row(ts_ms, kind):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".000Z"
    if kind == "real":
        return {"type": "assistant", "timestamp": iso, "message": {"model": "claude-opus-4-8", "content": "ok"}}
    return {"type": "assistant", "timestamp": iso, "isApiErrorMessage": True, "error": "authentication_failed",
            "message": {"model": "<synthetic>", "content": "Not logged in · Please run /login"}}


def test_j_growing_transcript_is_read_incrementally(tmp_path, monkeypatch):
    ps._auth_evidence_cache.clear()
    f = tmp_path / "s.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 2000}})
    f.write_text("\n".join([filler] * 200 + [json.dumps(_row(1_000_000_000, "real"))]) + "\n")
    opened = []
    real_open = open

    def counting_open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        if str(path) == str(f):
            orig_read = fh.read

            def read(n=-1):
                data = orig_read(n)
                opened.append(len(data))
                return data
            fh.read = read
        return fh
    monkeypatch.setattr("builtins.open", counting_open)
    ps._auth_turn_evidence([f])
    first = sum(opened)
    opened.clear()
    with real_open(f, "a") as fh:
        fh.write(json.dumps(_row(2_000_000_000, "auth_fail")) + "\n")
    res = ps._auth_turn_evidence([f])
    assert res["last_auth_fail_ms"] == 2_000_000_000 and res["last_real_ms"] == 1_000_000_000
    assert first > 100_000
    assert sum(opened) < 1_000, f"re-read {sum(opened)} bytes for a {len(json.dumps(_row(0, 'x')))}-byte append"
