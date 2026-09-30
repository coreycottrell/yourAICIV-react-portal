"""Round 2 for ticket 3270 (Pyonair-portal).

(1) auth-v2 "Authenticate" on a brand-new Korus AI stopped at Claude Code's
    "New MCP server found" startup dialog: it was detected but never dismissed,
    so the flow timed out 4/4 (midwife test birth, 2026-09-24). The dialog
    screens below are VERBATIM captures from the real Claude Code 2.1.280.
(2) /api/auth/status: absurd expiresAt values are UNKNOWN (not signed in), and
    the 2h refresh grace now needs POSITIVE evidence (a real model turn after
    expiry, from the CIV's own transcript). The independent skeptic's round-1
    refutations are pinned as tests here.

Run:  python -m pytest tests/ -q
No real CIV, credential or tmux server is touched (see conftest.py).
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

HOUR_MS = 3_600_000
ACCESS = "sk-ant-oat01-SECRET-ACCESS-TOKEN-do-not-leak"
REFRESH = "sk-ant-ort01-SECRET-REFRESH-TOKEN-do-not-leak"


@pytest.fixture(autouse=True)
def _never_pkill_claude(monkeypatch):
    """SAFETY: the real _kill_claude_process runs `pkill -f claude`."""
    calls = []

    async def fake_kill():
        calls.append(time.time())
    monkeypatch.setattr(ps, "_kill_claude_process", fake_kill)
    return calls


# ---------------------------------------------------------------------------
# (1) MCP dialog planner — verbatim screens from Claude Code 2.1.280
# ---------------------------------------------------------------------------

SINGLE_DEFAULT = """bash-5.2$ cd /home/aiciv && claude /login
────────────────────────────────────────────────────────────────────────
  New MCP server found in this project: playwright

  MCP servers may execute code or access system resources. All tool calls require approval. Learn more in the MCP documentation.

    Use this MCP server
    Use this and all future MCP servers in this project
  ❯ Continue without using this MCP server

  Enter to confirm · Esc to cancel
"""

SINGLE_ON_TARGET = SINGLE_DEFAULT.replace(
    "    Use this MCP server\n", "  ❯ Use this MCP server\n").replace(
    "  ❯ Continue without", "    Continue without")

MULTI_DEFAULT = """────────────────────────────────────────────────────────────────────────
  2 new MCP servers found in this project

  Select any you wish to enable.

  MCP servers may execute code or access system resources. All tool calls require approval. Learn more in the MCP documentation.

  ❯ [✔] playwright
    [✔] other
       Enable selected

 Space to select · Esc to reject all
"""

MULTI_ON_TARGET = MULTI_DEFAULT.replace("  ❯ [✔] playwright", "    [✔] playwright").replace(
    "       Enable selected", "  ❯    Enable selected")

LOGIN_MENU = """  Select login method:
  ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
    2. Anthropic Console account · API usage billing
"""


class TestMcpDialogPlanner:
    def test_single_default_cursor_moves_up_twice_to_use_this(self):
        plan = ps._plan_mcp_dialog(SINGLE_DEFAULT)
        assert plan == {"variant": "single", "target": "use_this",
                        "moves": ["Up", "Up"], "on_target": False}

    def test_single_never_targets_all_future_or_continue_without(self):
        plan = ps._plan_mcp_dialog(SINGLE_DEFAULT)
        assert plan["target"] == "use_this"  # least privilege, keeps the CIV's MCP working

    def test_single_on_target(self):
        plan = ps._plan_mcp_dialog(SINGLE_ON_TARGET)
        assert plan["on_target"] is True and plan["moves"] == []

    def test_multi_default_moves_down_to_enable_selected(self):
        plan = ps._plan_mcp_dialog(MULTI_DEFAULT)
        assert plan == {"variant": "multi", "target": "enable_selected",
                        "moves": ["Down", "Down"], "on_target": False}

    def test_multi_on_target(self):
        assert ps._plan_mcp_dialog(MULTI_ON_TARGET)["on_target"] is True

    def test_numbered_variant_is_understood(self):
        numbered = SINGLE_DEFAULT.replace("    Use this MCP server", "    1. Use this MCP server").replace(
            "    Use this and all future", "    2. Use this and all future").replace(
            "  ❯ Continue without", "  ❯ 3. Continue without")
        assert ps._plan_mcp_dialog(numbered)["moves"] == ["Up", "Up"]

    @pytest.mark.parametrize("text", ["", LOGIN_MENU, "API Error: 401", "> ready"])
    def test_no_dialog_means_no_plan(self, text):
        assert ps._plan_mcp_dialog(text) is None

    def test_header_without_cursor_is_not_navigable(self):
        assert ps._plan_mcp_dialog(SINGLE_DEFAULT.replace("❯", " ")) is None

    def test_stale_frame_above_uses_the_last_dialog(self):
        # An older frame (cursor on "Use this") sits above the current one.
        assert ps._plan_mcp_dialog(SINGLE_ON_TARGET + SINGLE_DEFAULT)["moves"] == ["Up", "Up"]

    def test_detector_matches_both_variants(self):
        assert ps.AUTH_SCREEN_PATTERNS["mcp_server"].search(SINGLE_DEFAULT)
        assert ps.AUTH_SCREEN_PATTERNS["mcp_server"].search(MULTI_DEFAULT)

    def test_login_menu_still_outranks_leftover_mcp_text(self):
        pr = ps.AUTH_SCREEN_PRIORITY
        assert pr.index("login_menu") < pr.index("mcp_server")


class TestDismissMcpDialogKeys:
    """Drive _dismiss_mcp_dialog against a scripted screen, recording keys."""

    def run(self, monkeypatch, screens):
        sent = []
        seq = list(screens)

        async def fake_capture(pane):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        async def fake_run(cmd, **k):
            sent.append(cmd[-1])
        monkeypatch.setattr(ps, "_capture_visible", fake_capture)
        monkeypatch.setattr(ps, "_run_subprocess_async", fake_run)

        async def no_sleep(*a, **k):
            return None
        monkeypatch.setattr(ps.asyncio, "sleep", no_sleep)
        return asyncio.run(ps._dismiss_mcp_dialog("%0")), sent

    def test_single_navigates_then_confirms(self, monkeypatch):
        out, sent = self.run(monkeypatch, [SINGLE_DEFAULT, SINGLE_ON_TARGET])
        assert out == "enabled_this"
        assert sent == ["Up", "Up", "Enter"]

    def test_multi_navigates_then_confirms_never_toggles(self, monkeypatch):
        out, sent = self.run(monkeypatch, [MULTI_DEFAULT, MULTI_ON_TARGET])
        assert out == "enabled_selected"
        assert sent == ["Down", "Down", "Enter"]  # Enter only on "Enable selected"

    def test_not_visible_presses_nothing(self, monkeypatch):
        out, sent = self.run(monkeypatch, [LOGIN_MENU])
        assert out == "not_visible" and sent == []

    def test_single_stuck_cursor_falls_back_to_default_so_signin_proceeds(self, monkeypatch):
        out, sent = self.run(monkeypatch, [SINGLE_DEFAULT])
        assert out == "fallback_default"
        assert sent[-1] == "Enter"

    def test_multi_stuck_cursor_falls_back_to_escape(self, monkeypatch):
        out, sent = self.run(monkeypatch, [MULTI_DEFAULT])
        assert out == "fallback_escape"
        assert sent[-1] == "Escape" and "Enter" not in sent


# ---------------------------------------------------------------------------
# (1) Integration: the REAL state machine on a private tmux server, against a
#     fake Claude TUI that renders the dialog and reacts to arrow keys.
# ---------------------------------------------------------------------------

FAKE_TUI = textwrap.dedent(r'''
    import os, sys, termios, tty, json
    mode, choice_file = sys.argv[1], sys.argv[2]   # mode: single | multi | stuck
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    def out(s):
        os.write(1, s.replace("\n", "\r\n").encode())
    SINGLE = ["Use this MCP server", "Use this and all future MCP servers in this project",
              "Continue without using this MCP server"]
    state = {"screen": "mcp", "cur": 2 if mode != "multi" else 0,
             "checked": {"playwright": True, "other": True}}
    def render():
        out("\x1b[2J\x1b[H")
        if state["screen"] == "mcp" and mode != "multi":
            out("  New MCP server found in this project: playwright\n\n")
            out("  MCP servers may execute code or access system resources.\n\n")
            for i, o in enumerate(SINGLE):
                out(("  ❯ " if i == state["cur"] else "    ") + o + "\n")
            out("\n  Enter to confirm · Esc to cancel\n")
        elif state["screen"] == "mcp":
            out("  2 new MCP servers found in this project\n\n  Select any you wish to enable.\n\n")
            items = ["playwright", "other"]
            for i, n in enumerate(items):
                box = "[✔]" if state["checked"][n] else "[ ]"
                out(("  ❯ " if i == state["cur"] else "    ") + box + " " + n + "\n")
            out(("  ❯ " if state["cur"] == 2 else "    ") + "   Enable selected\n")
            out("\n Space to select · Esc to reject all\n")
        elif state["screen"] == "login":
            out("  Select login method:\n  ❯ 1. Claude account with subscription\n")
        elif state["screen"] == "url":
            out("Browser didn't open? Use the url below to sign in:\n")
            out("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz123\n")
            out("Paste code here if prompted >\n")
    def record(v):
        with open(choice_file, "w") as fh:
            fh.write(json.dumps(v))
    render()
    buf = b""
    while True:
        b = os.read(fd, 16)
        if not b:
            break
        buf += b
        while buf:
            if buf.startswith((b"\x1b[A", b"\x1bOA")):
                k, buf = "up", buf[3:]
            elif buf.startswith((b"\x1b[B", b"\x1bOB")):
                k, buf = "down", buf[3:]
            elif buf[:1] in (b"\r", b"\n"):
                k, buf = "enter", buf[1:]
            elif buf[:1] == b"\x1b":
                k, buf = "esc", buf[1:]
            else:
                k, buf = "char", buf[1:]
            s = state["screen"]
            n_opts = 3
            if s == "mcp":
                if k in ("up", "down") and mode != "stuck":
                    state["cur"] = max(0, min(n_opts - 1, state["cur"] + (-1 if k == "up" else 1)))
                elif k == "enter" and mode != "multi":
                    record({"choice": SINGLE[state["cur"]]}); state["screen"] = "login"
                elif k == "enter":
                    if state["cur"] == 2:
                        record({"enabled": [n for n, v in state["checked"].items() if v]})
                        state["screen"] = "login"
                    else:
                        n = ["playwright", "other"][state["cur"]]
                        state["checked"][n] = not state["checked"][n]
                elif k == "esc":
                    record({"choice": "rejected"}); state["screen"] = "login"
            elif s == "login" and k == "enter":
                state["screen"] = "url"
            render()
''')

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None or not sys.platform.startswith("linux"),
                                reason="needs tmux + /proc")


@pytest.fixture
def private_tmux_with_fake_claude(tmp_path, monkeypatch):
    """Private tmux socket + a `claude` on PATH that is the fake TUI."""
    real = shutil.which("tmux")
    sock = tmp_path / "t.sock"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "tmux"
    shim.write_text(f'#!/bin/sh\nexec "{real}" -S "{sock}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    tui = tmp_path / "fake_tui.py"
    tui.write_text(FAKE_TUI)
    choice = tmp_path / "choice.json"
    mode_file = tmp_path / "mode"
    fake_claude = bindir / "claude"
    fake_claude.write_text(
        f'#!/bin/bash\nexec -a claude {sys.executable} {tui} "$(cat {mode_file})" {choice}\n')
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(ps, "_save_portal_message", lambda *a, **k: None)

    def start(name, mode):
        mode_file.write_text(mode)
        subprocess.run([str(shim), "new-session", "-d", "-s", name, "-x", "220", "-y", "50",
                        "bash --norc --noprofile"], check=True)
        time.sleep(0.8)
        return name

    yield start, choice
    out = subprocess.run([real, "-S", str(sock), "list-panes", "-a", "-F", "#{pane_pid}"],
                         capture_output=True, text=True)
    for pid in out.stdout.split():
        try:
            subprocess.run(["pkill", "-TERM", "-P", pid], check=False)
            os.kill(int(pid), 15)
        except Exception:
            pass
    subprocess.run([real, "-S", str(sock), "kill-server"], capture_output=True)


def _run_flow(pane):
    return asyncio.run(asyncio.wait_for(ps._run_auth_state_machine(pane), timeout=120))


@needs_tmux
def test_newborn_single_mcp_dialog_enables_server_and_gets_url(private_tmux_with_fake_claude, _never_pkill_claude):
    start, choice = private_tmux_with_fake_claude
    pane = start("newborn1", "single")
    result = _run_flow(pane)
    assert result.get("started") is True, result.get("log")
    assert "oauth/authorize" in result["url"] and "state=" in result["url"]
    assert json.loads(choice.read_text()) == {"choice": "Use this MCP server"}
    assert any("MCP server dialog: enabled_this" in m for m in result["log"]), result["log"]
    assert not any("Timeout" in m for m in result["log"]), result["log"]
    assert _never_pkill_claude == []


@needs_tmux
def test_newborn_multi_mcp_dialog_enables_all_and_gets_url(private_tmux_with_fake_claude, _never_pkill_claude):
    start, choice = private_tmux_with_fake_claude
    pane = start("newborn2", "multi")
    result = _run_flow(pane)
    assert result.get("started") is True, result.get("log")
    assert json.loads(choice.read_text()) == {"enabled": ["playwright", "other"]}
    assert any("enabled_selected" in m for m in result["log"]), result["log"]
    assert _never_pkill_claude == []


@needs_tmux
def test_unnavigable_dialog_still_reaches_signin(private_tmux_with_fake_claude, _never_pkill_claude):
    start, choice = private_tmux_with_fake_claude
    pane = start("newborn3", "stuck")
    result = _run_flow(pane)
    assert result.get("started") is True, result.get("log")
    assert any("fallback_default" in m for m in result["log"]), result["log"]
    assert _never_pkill_claude == []


# ---------------------------------------------------------------------------
# (2) /api/auth/status — absurd expiry + evidence-gated grace
# ---------------------------------------------------------------------------

def write_creds_raw(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def creds_json(**oauth):
    return json.dumps({"claudeAiOauth": oauth})


class TestAbsurdExpiry:
    NOW = 1_790_000_000_000

    def ev(self, tmp_path, expires):
        p = write_creds_raw(tmp_path / "c.json", creds_json(accessToken=ACCESS, refreshToken=REFRESH,
                                                            expiresAt=expires))
        return ps._evaluate_claude_credentials(p, now_ms=self.NOW)

    @pytest.mark.parametrize("expires", [1e18, 10 ** 18, 2 ** 62, NOW + 31 * 86_400_000,
                                         NOW + 365 * 86_400_000])
    def test_far_future_expiry_is_unknown_not_signed_in(self, tmp_path, expires):
        out = self.ev(tmp_path, expires)
        assert out["authenticated"] is False
        assert out["reason"] == "expiry_unknown_implausible"

    def test_plausible_future_expiry_is_valid(self, tmp_path):
        assert self.ev(tmp_path, self.NOW + 8 * HOUR_MS)["authenticated"] is True
        assert self.ev(tmp_path, self.NOW + 29 * 86_400_000)["authenticated"] is True

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_expiry_is_unknown(self, tmp_path, raw):
        p = write_creds_raw(tmp_path / "c.json",
                            '{"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": %s}}' % raw)
        out = ps._evaluate_claude_credentials(p, now_ms=self.NOW)
        assert out["authenticated"] is False and out["reason"] == "expiry_unknown"

    def test_seconds_instead_of_ms_is_not_signed_in(self, tmp_path):
        out = self.ev(tmp_path, 1_790_000_000)  # epoch SECONDS -> reads as 1970
        assert out["authenticated"] is False


def _row(ts_ms, kind):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".123Z"
    if kind == "real":
        return {"type": "assistant", "timestamp": iso,
                "message": {"model": "claude-opus-4-8", "content": [{"type": "text", "text": "done"}]}}
    if kind == "auth_fail":  # verbatim shape seen on disk (Claude Code 2.1.2xx)
        return {"type": "assistant", "timestamp": iso, "isApiErrorMessage": True,
                "error": "authentication_failed", "apiErrorStatus": None,
                "message": {"model": "<synthetic>",
                            "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}}
    if kind == "rate_limit":
        return {"type": "assistant", "timestamp": iso, "isApiErrorMessage": True,
                "error": "rate_limit", "apiErrorStatus": 429,
                "message": {"model": "<synthetic>",
                            "content": [{"type": "text", "text": "You've hit your weekly limit"}]}}
    if kind == "server":
        return {"type": "assistant", "timestamp": iso, "isApiErrorMessage": True,
                "error": "server_error", "apiErrorStatus": 529,
                "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 529 Overloaded."}]}}
    if kind == "user":
        return {"type": "user", "timestamp": iso, "message": {"content": "hi"}}
    raise ValueError(kind)


class TestTranscriptEvidence:
    def test_classification(self):
        assert ps._classify_assistant_record(_row(0, "real")) == "real"
        assert ps._classify_assistant_record(_row(0, "auth_fail")) == "auth_fail"
        assert ps._classify_assistant_record(_row(0, "rate_limit")) is None
        assert ps._classify_assistant_record(_row(0, "server")) is None
        assert ps._classify_assistant_record(_row(0, "user")) is None
        legacy = {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 401,
                  "message": {"model": "<synthetic>", "content": "API Error: 401"}}
        assert ps._classify_assistant_record(legacy) == "auth_fail"
        assert ps._classify_assistant_record({"type": "assistant", "message": {"model": "<synthetic>"}}) is None

    def test_scan_finds_latest_of_each_and_survives_partial_first_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_AUTH_EVIDENCE_TAIL_BYTES", 2048)
        f = tmp_path / "s.jsonl"
        rows = [_row(1_000_000, "real")] + [_row(1_000_000 + i, "user") for i in range(40)]
        rows += [_row(2_000_000, "auth_fail"), _row(3_000_000, "real"), _row(3_500_000, "rate_limit")]
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        res = ps._scan_transcript_tail(f)
        assert res["last_real_ms"] == 3_000_123
        assert res["last_auth_fail_ms"] == 2_000_123

    def test_evidence_is_cached_on_path_mtime_size(self, tmp_path, monkeypatch):
        ps._auth_evidence_cache.clear()
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps(_row(5_000_000, "real")) + "\n")
        calls = []
        real_scan = ps._scan_transcript_tail
        monkeypatch.setattr(ps, "_scan_transcript_tail", lambda p: calls.append(p) or real_scan(p))
        a = ps._auth_turn_evidence([f])
        b = ps._auth_turn_evidence([f])
        assert a == b and len(calls) == 1
        with open(f, "a") as fh:
            fh.write(json.dumps(_row(6_000_000, "auth_fail")) + "\n")
        c = ps._auth_turn_evidence([f])
        assert len(calls) == 2 and c["last_auth_fail_ms"] == 6_000_123

    def test_evidence_never_raises(self, tmp_path):
        assert ps._auth_turn_evidence([tmp_path / "missing.jsonl"]) == {
            "last_real_ms": None, "last_auth_fail_ms": None}
        (tmp_path / "junk.jsonl").write_bytes(b"\x00\xff not json \"assistant\"\n")
        assert ps._auth_turn_evidence([tmp_path / "junk.jsonl"])["last_real_ms"] is None


class TestDecideWithEvidence:
    EXP = 10_000_000

    def grace(self):
        return {"authenticated": False, "reason": "expired_within_refresh_grace",
                "expires_at": self.EXP, "needs_live_check": True}

    def valid(self):
        return {"authenticated": True, "reason": "token_valid", "expires_at": self.EXP + HOUR_MS,
                "needs_live_check": False}

    def ev(self, real=None, fail=None):
        return {"last_real_ms": real, "last_auth_fail_ms": fail}

    def test_grace_without_any_evidence_fails_closed(self):
        assert ps._decide_auth_with_evidence(self.grace(), self.ev(), None)[:2] == (
            False, "expired_no_evidence_of_refresh")

    def test_grace_with_turn_only_BEFORE_expiry_fails_closed(self):
        # skeptic: idle CIV, revoked refresh token, last good turn predates expiry
        assert ps._decide_auth_with_evidence(self.grace(), self.ev(real=self.EXP - 1), None)[0] is False

    def test_grace_with_turn_after_expiry_passes_to_pane_guard(self):
        assert ps._decide_auth_with_evidence(self.grace(), self.ev(real=self.EXP + 1), None) == (
            True, "expired_refresh_proven_by_recent_turn", True)

    def test_grace_auth_failure_after_last_turn_is_not_signed_in(self):
        out = ps._decide_auth_with_evidence(self.grace(), self.ev(real=self.EXP + 1, fail=self.EXP + 2), None)
        assert out[:2] == (False, "expired_api_reports_auth_failure")

    def test_valid_token_rejected_after_creds_written_is_not_signed_in(self):
        mtime = self.EXP - HOUR_MS
        out = ps._decide_auth_with_evidence(self.valid(), self.ev(fail=mtime + 5), mtime)
        assert out[:2] == (False, "token_rejected_by_api")

    def test_valid_token_ignores_failures_from_before_signin(self):
        # "Not logged in" rows written BEFORE this credentials file existed
        # (the newborn's pre-sign-in errors) must not keep the modal up.
        mtime = self.EXP
        assert ps._decide_auth_with_evidence(self.valid(), self.ev(fail=mtime - 5), mtime)[0] is True

    def test_valid_token_success_after_failure_is_signed_in(self):
        mtime = self.EXP
        assert ps._decide_auth_with_evidence(self.valid(), self.ev(fail=mtime + 5, real=mtime + 9), mtime)[0] is True

    def test_valid_token_unknown_mtime_keeps_valid(self):
        assert ps._decide_auth_with_evidence(self.valid(), self.ev(fail=1), None)[0] is True


class TestAuthStatusEndpointRound2:
    """End-to-end through the real Starlette endpoint, tmux 'alive'."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.creds = tmp_path / ".claude" / ".credentials.json"
        monkeypatch.setattr(ps, "CREDENTIALS_FILE", self.creds)

        async def fake_pane():
            return "%0"
        monkeypatch.setattr(ps, "_find_primary_pane_async", fake_pane)

        class _OK:
            returncode = 0
            stdout = None

        async def tmux_alive(*a, **k):
            return _OK()
        monkeypatch.setattr(ps, "_run_subprocess_async", tmux_alive)
        self.pane_text = "● Hourly recap sent."

        async def fake_output(cmd, timeout=5):
            return self.pane_text
        monkeypatch.setattr(ps, "_run_subprocess_output", fake_output)
        self.transcript = tmp_path / "projects" / "-home-aiciv" / "s.jsonl"
        self.transcript.parent.mkdir(parents=True)
        monkeypatch.setattr(ps, "_find_all_project_jsonl",
                            lambda: [self.transcript] if self.transcript.exists() else [])
        ps._auth_evidence_cache.clear()
        self.client = TestClient(ps.app)
        self.headers = {"Authorization": f"Bearer {ps.BEARER_TOKEN}"}

    def turn(self, ts_ms, kind):
        rec = _row(ts_ms, kind)
        rec["message"]["content"] = [{"type": "text", "text": f"{ACCESS} {REFRESH}"}] \
            if kind == "real" else rec["message"]["content"]
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def put_creds(self, **oauth):
        write_creds_raw(self.creds, creds_json(**oauth))

    def get(self):
        r = self.client.get("/api/auth/status", headers=self.headers)
        assert r.status_code == 200
        assert ACCESS not in r.text and REFRESH not in r.text
        assert set(r.json()) == {"authenticated", "account", "reason", "expires_at", "subscription"}
        return r.json()

    def test_skeptic_a1_expired_30min_refresh_clean_pane_idle_is_NOT_signed_in(self):
        now = int(time.time() * 1000)
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 30 * 60_000)
        self.turn(now - 3 * HOUR_MS, "real")  # last activity long before expiry
        body = self.get()
        assert body["authenticated"] is False
        assert body["reason"] == "expired_no_evidence_of_refresh"

    def test_expired_but_refresh_proven_by_turn_after_expiry_is_signed_in(self):
        now = int(time.time() * 1000)
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 30 * 60_000)
        self.turn(now - 60_000, "real")
        body = self.get()
        assert body["authenticated"] is True
        assert body["reason"] == "expired_refresh_proven_by_recent_turn"

    def test_proven_turn_but_pane_401_is_NOT_signed_in(self):
        now = int(time.time() * 1000)
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 30 * 60_000)
        self.turn(now - 60_000, "real")
        self.pane_text = "API Error: 401 · Please run /login"
        assert self.get()["reason"] == "expired_pane_reports_auth_failure"

    def test_expired_rate_limited_only_is_NOT_proof(self):
        now = int(time.time() * 1000)
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 30 * 60_000)
        self.turn(now - 60_000, "rate_limit")
        assert self.get()["authenticated"] is False

    def test_skeptic_a2_valid_token_empty_refresh_is_signed_in_until_rejected(self):
        now = int(time.time() * 1000)
        self.put_creds(accessToken=ACCESS, refreshToken="", expiresAt=now + 2 * HOUR_MS)
        assert self.get()["authenticated"] is True  # a live access token IS signed in
        time.sleep(0.01)
        self.turn(int(time.time() * 1000) + 1000, "auth_fail")  # revoked server-side
        body = self.get()
        assert body["authenticated"] is False and body["reason"] == "token_rejected_by_api"

    def test_newborn_old_not_logged_in_rows_do_not_block_after_signin(self):
        now = int(time.time() * 1000)
        self.turn(now - 10 * 60_000, "auth_fail")  # pre-sign-in errors
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now + 8 * HOUR_MS)
        body = self.get()
        assert body["authenticated"] is True and body["reason"] == "token_valid"

    def test_skeptic_a3_expiry_1e18_is_NOT_signed_in(self):
        self.put_creds(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=1e18)
        body = self.get()
        assert body["authenticated"] is False
        assert body["reason"] == "expiry_unknown_implausible"
        assert body["expires_at"] is None
