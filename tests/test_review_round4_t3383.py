"""Regression tests for the THIRD independent review (ticket 3383, of commit
78025aa) and for the real Claude Code 2.1.x screen layout found first-order
while fixing it: the input prompt is "❯ " directly under a horizontal rule
(empty box shows a 'Try "…"' placeholder), and pickers use the same "❯" as
their cursor ("❯ 1. …"). Each test FAILS on 78025aa and passes after the fix.
"""
import asyncio
import json
import time

import pytest

import portal_server as ps
from test_review_round2_t3383 import (  # noqa: F401  (autouse fixture re-used)
    ACCESS, CODE, CODE_PROMPT, HEADERS, REFRESH, FakePane, _client, _host_safety, _run, _status_env,
)
from test_review_round3_t3383 import KeyLog

RULE = "─" * 60
REAL_IDLE = f" ⏺ Done — the report is saved.\n\n{RULE}\n❯ Try \"fix lint errors\"\n{RULE}\n  ⏵⏵ bypass permissions on\n"
REAL_PICKER = " Select login method:\n\n ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise\n   2. Anthropic Console account · API usage billing\n"
REAL_PERMISSION = (f" ⏺ Bash(rm -rf build/)\n{RULE}\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently\n")
REAL_DRAFT = f" ⏺ Done.\n\n{RULE}\n❯ please also check the invoices from\n{RULE}\n"


# --- the real prompt model ---------------------------------------------------
def test_real_tui_code_prompt_above_the_live_prompt_is_not_active():
    # A cancelled sign-in's "Paste code here" stays in the scrollback above the
    # real "❯" input box. Typing the code now would send it as a chat message.
    screen = CODE_PROMPT + " (login cancelled)\n\n" + REAL_IDLE
    assert ps._pane_awaits_auth_code(screen) is False


def test_real_tui_old_login_picker_above_the_prompt_earns_no_key():
    assert ps._plan_auth_close(REAL_PICKER + "\n" + REAL_IDLE) is None


def test_real_tui_picker_cursor_is_not_an_input_prompt():
    assert ps._pane_awaits_auth_code(CODE_PROMPT) is True
    assert ps._plan_auth_close(REAL_PICKER) == "Escape"


def test_code_is_refused_on_the_real_idle_prompt(monkeypatch):
    pane = FakePane(monkeypatch, visible=CODE_PROMPT + " (login cancelled)\n" + REAL_IDLE)

    async def scenario():
        async with _client() as c:
            return (await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})).json()
    assert _run(scenario()).get("injected") is not True
    assert pane.keys == []


# --- #4: /login only into an idle, EMPTY input box ---------------------------
@pytest.mark.parametrize("screen", [REAL_PERMISSION, REAL_DRAFT])
def test_4_login_is_not_typed_over_a_question_or_the_owners_draft(monkeypatch, screen):
    pane = FakePane(monkeypatch, visible=screen, full="")
    _run(ps._run_auth_state_machine("%1"), timeout=10)
    assert pane.keys == [], f"typed into a question / over the owner's draft: {pane.keys}"


def test_4_login_is_typed_into_the_real_empty_prompt(monkeypatch):
    pane = FakePane(monkeypatch, visible=REAL_IDLE, full="")
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 0.8, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 0, raising=False)
    _run(ps._run_auth_state_machine("%1"), timeout=10)
    assert pane.keys[:2] == [["-l", "/login"], ["Enter"]], pane.keys


# --- #1: a restart-loop bash is not a shell prompt; only /login is killed ----
def _p(ppid, pgrp, tpgid, *argv):
    return {"ppid": ppid, "pgrp": pgrp, "tpgid": tpgid, "argv": list(argv)}


def test_1_script_bash_between_relaunches_is_not_a_shell_prompt():
    table = {100: _p(1, 100, 100, "bash", "/home/aiciv/restart-self.sh")}
    assert ps._classify_pane("bash", 100, table) == "unknown"
    table = {100: _p(1, 100, 100, "bash", "-c", "while true; do claude; sleep 5; done")}
    assert ps._classify_pane("bash", 100, table) == "unknown"


def test_1_interactive_shell_is_still_a_shell():
    for argv in (["bash"], ["-bash"], ["bash", "--norc", "--noprofile"], ["bash", "-l"]):
        assert ps._classify_pane("bash", 100, {100: _p(1, 100, 100, *argv)}) == "shell", argv


def test_1_retry_never_kills_a_wrapper_relaunched_live_claude(monkeypatch):
    table = {
        100: _p(1, 100, 100, "bash"),
        200: _p(100, 200, 200, "claude", "--dangerously-skip-permissions"),  # relaunched live session
    }
    monkeypatch.setattr(ps, "_read_proc_table", lambda: table)
    signals = []

    async def run_output(cmd, timeout=5):
        return "100" if "display-message" in cmd else ""
    monkeypatch.setattr(ps, "_run_subprocess_output", run_output)

    async def fake_signal(pids, sig):
        signals.append(sorted(pids))
    monkeypatch.setattr(ps, "_signal_pids", fake_signal)
    killed = _run(ps._kill_claude_process("%1", exclude=set(), require_arg="/login"))
    assert killed == [] and signals == []


# --- #2/#3: first-boot never re-awakens (or kills) an AI that has conversations
def test_3_first_boot_declines_an_ai_with_real_conversations(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "EVOLUTION_DONE_MARKER", tmp_path / ".evolution-done")
    monkeypatch.setattr(ps, "FIRST_BOOT_MARKER", tmp_path / ".first-boot-fired")
    monkeypatch.setattr(ps, "FIRST_BOOT_SETTLE_S", 0.0)
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-01T10:00:00.000Z",
                             "message": {"model": "claude-opus-4-8", "content": "done"}}) + "\n")
    monkeypatch.setattr(ps, "_find_all_project_jsonl", lambda: [t])
    ps._auth_evidence_cache.clear()
    pane = FakePane(monkeypatch, state="claude", visible=REAL_IDLE)
    killed = []

    async def no_kill(*a, **k):
        killed.append(a)
        return []
    monkeypatch.setattr(ps, "_kill_claude_process", no_kill)

    async def scenario():
        async with _client() as c:
            return (await asyncio.wait_for(c.post("/api/evolution/first-boot", headers=HEADERS), timeout=5)).json()
    body = _run(scenario())
    assert body.get("status") == "already_active", body
    assert killed == [] and pane.keys == []
    assert not (tmp_path / ".first-boot-fired").exists()


# --- #5: quoted MCP dialog text above the prompt earns no key ------------------
def test_5_quoted_mcp_dialog_above_the_prompt_is_not_a_blocker(monkeypatch):
    quoted = (" New MCP server found in this project: playwright\n"
              "   Use this MCP server\n ❯ Continue without using this MCP server\n\n" + REAL_IDLE)
    assert ps._active_blocker(quoted) is None
    pane = FakePane(monkeypatch, visible=quoted)
    assert _run(ps._dismiss_mcp_dialog("%1")) == "not_visible"
    assert pane.keys == []


# --- #6: the idle bound never overrides proof ---------------------------------
def test_6_old_expiry_but_a_real_turn_after_it_is_signed_in(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    t = tmp_path / "s.jsonl"
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((now - 60_000) / 1000)) + ".000Z"
    t.write_text(json.dumps({"type": "assistant", "timestamp": iso,
                             "message": {"model": "claude-opus-4-8", "content": "ok"}}) + "\n")
    put, get = _status_env(tmp_path, monkeypatch, transcripts=[t])
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 30 * 86_400_000)
    body = _run(get())
    assert body["authenticated"] is True and body["reason"] == "expired_refresh_proven_by_recent_turn", body


# --- #7: an unreadable-once transcript never poisons the scan -----------------
def test_7_a_poisoned_cache_entry_does_not_disable_evidence(tmp_path):
    ps._auth_evidence_cache.clear()
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-01T10:00:00.000Z",
                             "message": {"model": "claude-opus-4-8", "content": "ok"}}) + "\n")
    ps._auth_evidence_cache[str(t)] = {"ino": t.stat().st_ino, "offset": None,
                                       "res": {"last_real_ms": None, "last_auth_fail_ms": None}}
    assert ps._auth_turn_evidence([t])["last_real_ms"] is not None


# --- #8: Start never blocks past a proxy timeout; the outcome is pollable ------
def test_8_start_returns_pending_and_url_reports_the_outcome(monkeypatch):
    FakePane(monkeypatch, visible=REAL_IDLE, full="")
    monkeypatch.setattr(ps, "AUTH_START_WAIT_S", 0.5, raising=False)
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 1.5, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 0, raising=False)

    async def scenario():
        async with _client() as c:
            r = (await asyncio.wait_for(c.post("/api/auth/start", headers=HEADERS), timeout=3)).json()
            await asyncio.sleep(3.0)
            u = (await c.get("/api/auth/url", headers=HEADERS)).json()
            return r, u
    r, u = _run(scenario())
    assert r.get("pending") is True, r
    assert u.get("done") is True and u.get("error"), u


# --- #9: the portal's own injectors hold while a sign-in owns the pane --------
def test_9_chat_send_is_held_while_the_signin_screen_is_open(monkeypatch):
    pane = FakePane(monkeypatch, visible=CODE_PROMPT)
    monkeypatch.setattr(ps, "_auth_screen_open_until", time.time() + 60, raising=False)

    async def scenario():
        async with _client() as c:
            return await c.post("/api/chat/send", headers=HEADERS, json={"message": "hello"})
    r = _run(scenario())
    assert r.status_code == 409, r.text
    assert pane.keys == [], pane.keys


# --- #10: "/login" and its Enter are one unit ---------------------------------
def test_10_cancel_between_text_and_enter_never_strands_login(monkeypatch):
    pane = FakePane(monkeypatch, visible=REAL_IDLE, full="")
    pane.keys = KeyLog()
    holder = {}

    def on_key(k):
        if k == ["-l", "/login"] and "flow" in holder:
            holder["flow"].cancel.set()  # the owner hits × right now
    pane.keys.hook = on_key

    async def scenario():
        flow = ps._AuthFlow("%1")
        holder["flow"] = flow
        try:
            await ps._run_auth_state_machine("%1", flow)
        except ps._AuthCancelled:
            pass
    _run(scenario(), timeout=10)
    assert pane.keys[:2] == [["-l", "/login"], ["Enter"]], pane.keys
