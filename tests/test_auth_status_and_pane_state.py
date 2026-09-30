"""Real Claude sign-in status and auth-v2 never typing shell text into a live
Claude prompt. Ported from coreycottrell/Pyonair-portal (ticket 3270, commits
dc6833d + 430ab58) into the yourAICIV portal (Witness ticket 3383). The Opus
model-floor tests from that branch are NOT ported: the yourAICIV portal takes
its launch model from config/launch_model.txt (tests/test_resume_model.py).

Run:  python -m pytest tests/ -q
Needs: starlette, httpx, aiosqlite, pytest (and tmux for the integration tests,
which are skipped when tmux is missing). No real CIV, credential or tmux server
is touched: HOME is a temp dir (conftest.py) and tmux runs on a private socket.
"""
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import portal_server as ps

NOW_MS = 1_790_000_000_000
HOUR_MS = 3_600_000
ACCESS = "sk-ant-oat01-SECRET-ACCESS-TOKEN-do-not-leak"
REFRESH = "sk-ant-ort01-SECRET-REFRESH-TOKEN-do-not-leak"


def write_creds(path: Path, **oauth):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return path


@pytest.fixture(autouse=True)
def _never_pkill_claude(monkeypatch):
    """SAFETY: the real _kill_claude_process runs `pkill -f claude`, which on a
    developer/ops host would kill every Claude process. Always stub it, and
    record calls so tests can assert a live session is never killed."""
    calls = []

    async def fake_kill():
        calls.append(time.time())
    monkeypatch.setattr(ps, "_kill_claude_process", fake_kill)
    return calls


@pytest.fixture
def client():
    return TestClient(ps.app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {ps.BEARER_TOKEN}"}


# ---------------------------------------------------------------------------
# Defect A — credential evaluation (pure)
# ---------------------------------------------------------------------------

class TestEvaluateCredentials:
    def test_missing_file_is_not_authenticated(self, tmp_path):
        ev = ps._evaluate_claude_credentials(tmp_path / "nope.json", now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["reason"] == "no_credentials_file"

    def test_unreadable_json_fails_closed(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_text("{not json")
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["reason"] == "credentials_unreadable"

    def test_no_access_token(self, tmp_path):
        f = write_creds(tmp_path / "c.json", refreshToken=REFRESH, expiresAt=NOW_MS + HOUR_MS)
        assert ps._evaluate_claude_credentials(f, now_ms=NOW_MS)["authenticated"] is False

    def test_valid_token_is_authenticated(self, tmp_path):
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken=REFRESH,
                        expiresAt=NOW_MS + 6 * HOUR_MS, subscriptionType="max")
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is True
        assert ev["reason"] == "token_valid"
        assert ev["subscription"] == "max"

    def test_valid_token_even_with_empty_refresh(self, tmp_path):
        # Access token still good: working now; flips to false once it expires.
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken="",
                        expiresAt=NOW_MS + HOUR_MS)
        assert ps._evaluate_claude_credentials(f, now_ms=NOW_MS)["authenticated"] is True

    def test_expired_with_empty_refresh_is_not_authenticated(self, tmp_path):
        # dispatch-yash #3263: refreshToken empty, expiresAt 92.8h in the past.
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken="",
                        expiresAt=NOW_MS - int(92.8 * HOUR_MS))
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["reason"] == "expired_no_refresh_token"
        assert ev["needs_live_check"] is False

    def test_expired_with_missing_refresh_key(self, tmp_path):
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, expiresAt=NOW_MS - HOUR_MS)
        assert ps._evaluate_claude_credentials(f, now_ms=NOW_MS)["reason"] == "expired_no_refresh_token"

    def test_long_expired_with_refresh_is_not_authenticated(self, tmp_path):
        # alexia-jordannah #3264: refresh present but rejected, expired 203.6h ago.
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken=REFRESH,
                        expiresAt=NOW_MS - int(203.6 * HOUR_MS))
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["reason"] == "expired_beyond_refresh_grace"

    def test_recently_expired_with_refresh_needs_live_check(self, tmp_path):
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken=REFRESH,
                        expiresAt=NOW_MS - 10 * 60_000)
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["needs_live_check"] is True

    def test_about_to_expire_counts_as_expired(self, tmp_path):
        f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken="",
                        expiresAt=NOW_MS + 30_000)  # inside the 60s skew
        assert ps._evaluate_claude_credentials(f, now_ms=NOW_MS)["authenticated"] is False

    @pytest.mark.parametrize("bad", [None, 0, -5, "1790000000000", True, [], {}])
    def test_unknown_expiry_fails_closed(self, tmp_path, bad):
        oauth = {"accessToken": ACCESS, "refreshToken": REFRESH}
        if bad is not None:
            oauth["expiresAt"] = bad
        f = write_creds(tmp_path / "c.json", **oauth)
        ev = ps._evaluate_claude_credentials(f, now_ms=NOW_MS)
        assert ev["authenticated"] is False
        assert ev["reason"] == "expiry_unknown"

    def test_result_never_contains_secrets(self, tmp_path):
        for exp in (NOW_MS + HOUR_MS, NOW_MS - HOUR_MS, NOW_MS - 500 * HOUR_MS):
            f = write_creds(tmp_path / "c.json", accessToken=ACCESS, refreshToken=REFRESH,
                            expiresAt=exp, subscriptionType="max")
            blob = json.dumps(ps._evaluate_claude_credentials(f, now_ms=NOW_MS))
            assert ACCESS not in blob and REFRESH not in blob


class TestAuthScreenPatterns:
    def test_401_is_not_a_launch_error(self):
        err = ps.AUTH_SCREEN_PATTERNS["error"]
        assert not err.search("API Error: 401 OAuth access token has expired")
        assert err.search("bash: claude: command not found")

    def test_passive_update_banner_is_not_a_blocker(self):
        up = ps.AUTH_SCREEN_PATTERNS["update_prompt"]
        assert not up.search("✗ Auto-update failed · Try claude doctor")
        assert not up.search("Update available! Run: claude update")

    def test_login_menu_outranks_blockers(self):
        pr = ps.AUTH_SCREEN_PRIORITY
        assert pr.index("login_menu") < pr.index("update_prompt")
        assert pr.index("login_menu") < pr.index("csat_survey")


class TestPaneAuthFailure:
    @pytest.mark.parametrize("text", [
        "API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}",
        "OAuth access token has expired. Please run /login",
        "Login expired",
        "Unable to validate model: Could not resolve authentication method",
    ])
    def test_failure_markers(self, text):
        assert ps._pane_shows_auth_failure(text) is True

    def test_healthy_pane(self):
        assert ps._pane_shows_auth_failure("> hello\n● Hi Yash, here is your recap") is False
        assert ps._pane_shows_auth_failure("") is False


# ---------------------------------------------------------------------------
# Defect A — /api/auth/status endpoint
# ---------------------------------------------------------------------------

class TestAuthStatusEndpoint:
    @pytest.fixture(autouse=True)
    def _creds(self, tmp_path, monkeypatch):
        self.creds = tmp_path / ".claude" / ".credentials.json"
        monkeypatch.setattr(ps, "CREDENTIALS_FILE", self.creds)

        async def fake_pane():
            return "%0"
        monkeypatch.setattr(ps, "_find_primary_pane_async", fake_pane)

        # REGRESSION GUARD: every tmux call "succeeds" — i.e. the tmux session is
        # alive, which is exactly what used to force authenticated:true.
        class _OK:
            returncode = 0
            stdout = None

        async def tmux_alive(*a, **k):
            return _OK()
        monkeypatch.setattr(ps, "_run_subprocess_async", tmux_alive)
        self.pane_text = "> ready"

        async def fake_output(cmd, timeout=5):
            return self.pane_text
        monkeypatch.setattr(ps, "_run_subprocess_output", fake_output)

        # Round 2: the grace path needs transcript evidence. Tests control the
        # transcript list explicitly (never the real ~/.claude/projects scan).
        self.transcripts = []
        monkeypatch.setattr(ps, "_find_all_project_jsonl", lambda: list(self.transcripts))
        ps._auth_evidence_cache.clear()
        self.tmp = tmp_path

    def add_turn(self, ts_ms, kind="real"):
        """Append one assistant row to a transcript (real turn or auth failure)."""
        f = self.tmp / "projects" / "-home-aiciv" / "s1.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".000Z"
        if kind == "real":
            rec = {"type": "assistant", "timestamp": iso,
                   "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "ok"}]}}
        else:
            rec = {"type": "assistant", "timestamp": iso, "isApiErrorMessage": True,
                   "error": "authentication_failed",
                   "message": {"model": "<synthetic>",
                               "content": [{"type": "text", "text": "Not logged in \u00b7 Please run /login"}]}}
        with open(f, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if f not in self.transcripts:
            self.transcripts.append(f)
        return f

    def get(self, client, headers):
        r = client.get("/api/auth/status", headers=headers)
        assert r.status_code == 200
        assert ACCESS not in r.text and REFRESH not in r.text
        return r.json()

    def test_requires_bearer(self, client):
        assert client.get("/api/auth/status").status_code == 401

    def test_expired_dead_grant_with_live_tmux_is_NOT_authenticated(self, client, auth_headers):
        write_creds(self.creds, accessToken=ACCESS, refreshToken="",
                    expiresAt=int(time.time() * 1000) - 93 * HOUR_MS)
        body = self.get(client, auth_headers)
        assert body["authenticated"] is False
        assert body["reason"] == "expired_no_refresh_token"

    def test_long_expired_with_refresh_and_live_tmux_is_NOT_authenticated(self, client, auth_headers):
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH,
                    expiresAt=int(time.time() * 1000) - 204 * HOUR_MS)
        assert self.get(client, auth_headers)["authenticated"] is False

    def test_valid_token(self, client, auth_headers):
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH,
                    expiresAt=int(time.time() * 1000) + 5 * HOUR_MS, subscriptionType="max")
        body = self.get(client, auth_headers)
        assert body["authenticated"] is True
        assert body["subscription"] == "max"
        assert body["account"] is None

    def test_no_credentials(self, client, auth_headers):
        assert self.get(client, auth_headers)["authenticated"] is False

    def test_recent_expiry_pane_ok_WITHOUT_evidence_is_NOT_authenticated(self, client, auth_headers):
        # Round 2 (skeptic case a): a clean pane alone is NOT proof. An idle CIV
        # whose refresh token was revoked server-side looks exactly like this.
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH,
                    expiresAt=int(time.time() * 1000) - 5 * 60_000)
        self.pane_text = "● Hourly recap sent to Yash."
        body = self.get(client, auth_headers)
        assert body["authenticated"] is False
        assert body["reason"] == "expired_no_evidence_of_refresh"

    def test_recent_expiry_with_real_turn_after_expiry_is_authenticated(self, client, auth_headers):
        now = int(time.time() * 1000)
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 5 * 60_000)
        self.add_turn(now - 60_000, "real")
        self.pane_text = "● Hourly recap sent to Yash."
        body = self.get(client, auth_headers)
        assert body["authenticated"] is True
        assert body["reason"] == "expired_refresh_proven_by_recent_turn"

    def test_recent_expiry_pane_401_is_NOT_authenticated(self, client, auth_headers):
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH,
                    expiresAt=int(time.time() * 1000) - 5 * 60_000)
        self.pane_text = "API Error: 401 OAuth access token has expired"
        assert self.get(client, auth_headers)["authenticated"] is False

    def test_recent_expiry_pane_unreadable_fails_closed(self, client, auth_headers):
        now = int(time.time() * 1000)
        write_creds(self.creds, accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 5 * 60_000)
        self.add_turn(now - 60_000, "real")  # evidence present, so the pane guard is reached
        self.pane_text = ""
        body = self.get(client, auth_headers)
        assert body["authenticated"] is False
        assert body["reason"] == "expired_pane_unreadable"


# ---------------------------------------------------------------------------
# auth-v2 — pane classification (pure)
# ---------------------------------------------------------------------------

def proc(ppid, pgrp, tpgid, *argv):
    return {"ppid": ppid, "pgrp": pgrp, "tpgid": tpgid, "argv": list(argv)}


class TestPaneClassification:
    def test_claude_under_bash_wrapper_is_claude(self):
        # The dispatch-yash shape: pane_current_command reports the WRAPPER.
        table = {100: proc(1, 100, 100, "bash", "-c", "claude --resume x; echo exited"),
                 101: proc(100, 100, 100, "claude", "--resume", "x")}
        assert ps._classify_pane("bash", "100", table) == "claude"

    def test_native_exe_under_wrapper(self):
        table = {100: proc(1, 100, 100, "sh"),
                 101: proc(100, 100, 100, "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")}
        assert ps._classify_pane("sh", 100, table) == "claude"

    def test_npm_node_build_under_wrapper(self):
        table = {100: proc(1, 100, 100, "bash"),
                 101: proc(100, 100, 100, "node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js")}
        assert ps._classify_pane("bash", 100, table) == "claude"

    def test_background_claude_under_idle_shell_is_shell(self):
        table = {100: proc(1, 100, 100, "bash"),
                 101: proc(100, 101, 100, "claude", "-p", "hi")}  # own pgrp, not foreground
        assert ps._classify_pane("bash", 100, table) == "shell"

    def test_idle_shell(self):
        assert ps._classify_pane("bash", 100, {100: proc(1, 100, 100, "bash")}) == "shell"

    @pytest.mark.parametrize("cmd", ["claude", "claude.exe", "2.1.273"])
    def test_name_based_claude(self, cmd):
        assert ps._classify_pane(cmd, None, {}) == "claude"

    def test_unknown_program(self):
        assert ps._classify_pane("python3", 100, {100: proc(1, 100, 100, "python3", "x.py")}) == "unknown"

    def test_argv_detection(self):
        assert ps._argv_is_claude(["claude"]) is True
        assert ps._argv_is_claude(["/usr/bin/claude", "--resume", "x"]) is True
        assert ps._argv_is_claude(["node", "/srv/app/server.js"]) is False
        assert ps._argv_is_claude(["python3", "claude_helper.py"]) is False
        assert ps._argv_is_claude([]) is False


# ---------------------------------------------------------------------------
# auth-v2 — integration on a PRIVATE tmux server (real processes, fake Claude)
# ---------------------------------------------------------------------------

FAKE_CLAUDE = textwrap.dedent(r'''
    import sys
    log = open(sys.argv[1], "a", buffering=1)
    print("Claude Code v2.1.273  Opus 4.8 (1M context)", flush=True)
    print("✗ Auto-update failed · Try claude doctor or npm i -g @anthropic-ai/claude-code", flush=True)
    print("API Error: 401 OAuth access token has expired", flush=True)
    for line in sys.stdin:
        line = line.rstrip("\n")
        log.write(repr(line) + "\n")
        if line.strip() == "/login":
            print("Select login method:", flush=True)
            print(" > 1. Claude account with subscription", flush=True)
        elif line.strip() == "":
            print("Browser didn't open? Use the url below to sign in:", flush=True)
            print("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz123", flush=True)
            print("Paste code here if prompted >", flush=True)
''')

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None or not sys.platform.startswith("linux"),
                                reason="needs tmux + /proc")


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    """Put a `tmux` shim first on PATH that pins every call to a private socket."""
    real = shutil.which("tmux")
    sock = tmp_path / "t.sock"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(f'#!/bin/sh\nexec "{real}" -S "{sock}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    started = []

    def start(name, command):
        subprocess.run([str(shim), "new-session", "-d", "-s", name, "-x", "220", "-y", "50", command],
                       check=True)
        started.append(name)
        return name

    yield start, sock
    # Teardown: end ONLY our own test processes on our own private socket.
    out = subprocess.run([real, "-S", str(sock), "list-panes", "-a", "-F", "#{pane_pid}"],
                         capture_output=True, text=True)
    for pid in out.stdout.split():
        try:
            subprocess.run(["pkill", "-TERM", "-P", pid], check=False)
            os.kill(int(pid), 15)
        except Exception:
            pass
    # And the private tmux server itself (it outlived the test otherwise).
    subprocess.run([real, "-S", str(sock), "kill-server"], capture_output=True)


def _wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


@needs_tmux
def test_pane_state_detects_claude_under_wrapper_on_real_tmux(private_tmux, tmp_path):
    start, _ = private_tmux
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    log = tmp_path / "typed.log"
    # A wrapper (compound command) keeps bash as the pane's current command.
    start("wrapped", f"bash -c 'exec -a claude python3 {fake} {log}'; echo exited; sleep 30")
    start("idle", "bash --norc --noprofile")
    assert _wait_for(lambda: log.parent.exists())
    time.sleep(1.0)
    pcc = subprocess.run(["tmux", "display-message", "-t", "wrapped", "-p", "#{pane_current_command}"],
                         capture_output=True, text=True).stdout.strip()
    assert pcc not in ("claude",), "precondition: tmux reports the wrapper, not claude"
    assert asyncio.run(ps._pane_process_state("wrapped")) == "claude"
    assert asyncio.run(ps._is_claude_running_async("wrapped")) is True
    assert asyncio.run(ps._pane_process_state("idle")) == "shell"
    assert asyncio.run(ps._pane_process_state("no-such-session")) == "unknown"


@needs_tmux
def test_auth_v2_sends_login_to_live_claude_and_gets_url(private_tmux, tmp_path, monkeypatch, _never_pkill_claude):
    """dispatch-yash reproduction: a signed-out Claude running under a wrapper,
    with the passive 'Auto-update failed' banner on screen. auth-v2 must send
    /login (never a shell line), pass the login picker and return the URL."""
    start, _ = private_tmux
    monkeypatch.setattr(ps, "_save_portal_message", lambda *a, **k: None)
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    typed = tmp_path / "typed.log"
    start("penelope", f"bash -c 'exec -a claude python3 {fake} {typed}'; echo exited; sleep 30")
    time.sleep(1.5)

    result = asyncio.run(asyncio.wait_for(ps._run_auth_state_machine("penelope"), timeout=90))
    lines = typed.read_text().splitlines() if typed.exists() else []
    assert not any("clear && cd" in l or "claude /login" in l for l in lines), lines
    assert "'/login'" in lines, lines
    assert result.get("started") is True, result
    assert "oauth/authorize" in result.get("url", "") and "state=" in result["url"]
    assert not any("update_prompt" in m for m in result.get("log", [])), result.get("log")
    assert _never_pkill_claude == [], "auth-v2 must never kill the CIV's live session"


@needs_tmux
def test_auth_v2_refuses_to_type_into_unknown_pane(private_tmux, tmp_path, monkeypatch):
    start, _ = private_tmux
    monkeypatch.setattr(ps, "_save_portal_message", lambda *a, **k: None)
    result = asyncio.run(ps._run_auth_state_machine("does-not-exist"))
    assert result["started"] is False
    assert "unknown" in result["error"]
