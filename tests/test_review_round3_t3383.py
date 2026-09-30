"""Regression tests for the SECOND independent review (ticket 3383, of commit
8b6c2b0). Each test FAILS on 8b6c2b0 and passes after the fix.

  1  blocker words in the AI's own conversation (above the input prompt) and
     a mid-turn session never earn a key press
  2  a startup dialog already open in the live session is dismissed properly,
     "/login" is never typed into it
  3  a Close that reaches the server before its Start still cancels it
  5  "idle is not signed out" is bounded (14 days by default)
  6  concurrent status calls never advance the transcript offset twice
  7  /api/auth/verify reads the ACTIVE success screen, not line counts
  8  the account label survives a half-written ~/.claude.json
  9  a stack of leftover sign-in screens is closed one key at a time
 10  prewarm and Start never type into the pane at the same time
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

import portal_server as ps
from test_review_round2_t3383 import (  # noqa: F401  (autouse fixture re-used)
    ACCESS, BUSY, CODE, CODE_PROMPT, HEADERS, IDLE_PROMPT, LOGIN_PICKER, REFRESH,
    FakePane, _client, _host_safety, _run, _status_env,
)

TRUST_IN_CONVERSATION = (" ⏺ The vendor asked: Do you want to trust this project? and got a thumbs up.\n"
                         " Would you recommend us?\n\n > \n")
class KeyLog(list):
    """A key log that calls `hook(key)` after every recorded key."""
    hook = None

    def append(self, k):
        super().append(k)
        if self.hook:
            self.hook(k)


MCP_DIALOG = (" New MCP server found in this project: playwright\n\n"
              " MCP servers may execute code or access system resources.\n\n"
              "   Use this MCP server\n"
              "   Use this and all future MCP servers in this project\n"
              " ❯ Continue without using this MCP server\n\n"
              " Enter to confirm · Esc to cancel\n")


# 1 -------------------------------------------------------------------------
def test_1_blocker_words_above_the_input_prompt_get_no_key(monkeypatch):
    pane = FakePane(monkeypatch, visible=TRUST_IN_CONVERSATION)
    for kind in ("trust_folder", "csat_survey"):
        _run(ps._dismiss_auth_blocker("%1", kind))
    assert pane.keys == [], pane.keys


def test_1_no_login_menu_enter_while_the_ai_is_mid_turn(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")
    pane.keys = KeyLog()

    def on_key(k):
        if k == ["Enter"] and ["-l", "/login"] in pane.keys and pane.full == "":
            # /login accepted; the picker draws while a BOOP turn starts
            pane.full = LOGIN_PICKER + BUSY
            pane.visible = LOGIN_PICKER + BUSY
    pane.keys.hook = on_key
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 2.0, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 0, raising=False)
    _run(ps._run_auth_state_machine("%1"), timeout=10)
    assert pane.keys == [["-l", "/login"], ["Enter"]], f"key pressed into a mid-turn session: {pane.keys}"


# 2 -------------------------------------------------------------------------
def test_2_login_is_never_typed_into_an_open_startup_dialog(monkeypatch):
    pane = FakePane(monkeypatch, visible=MCP_DIALOG, full=MCP_DIALOG)
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 0, raising=False)
    monkeypatch.setattr(ps, "AUTH_BLOCKER_MAX_DISMISSALS", 1)
    result = _run(ps._run_auth_state_machine("%1"), timeout=30)
    assert ["-l", "/login"] not in pane.keys, f"/login typed into the MCP dialog: {pane.keys}"
    assert pane.keys and pane.keys[0] == ["Up"], pane.keys  # navigated toward 'Use this MCP server'
    assert result.get("started") is False


# 3 -------------------------------------------------------------------------
def test_3_close_that_arrives_before_its_start_still_cancels_it(monkeypatch):
    pane = FakePane(monkeypatch, visible=IDLE_PROMPT, full="")

    async def scenario():
        async with _client() as c:
            await c.post("/api/auth/close", headers=HEADERS, json={"attempt": "att-123"})
            r = await asyncio.wait_for(
                c.post("/api/auth/start", headers=HEADERS, json={"attempt": "att-123"}), timeout=5)
            return r.json()
    body = _run(scenario())
    assert body.get("cancelled") is True, body
    assert pane.keys == [], pane.keys


# 5 -------------------------------------------------------------------------
def test_5_idle_bound_long_idle_reads_signed_out(tmp_path, monkeypatch):
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=int(time.time() * 1000) - 20 * 86_400_000)
    body = _run(get())
    assert body["authenticated"] is False and body["reason"] == "expired_refresh_too_old", body


def test_5_idle_within_bound_still_signed_in(tmp_path, monkeypatch):
    put, get = _status_env(tmp_path, monkeypatch)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=int(time.time() * 1000) - 3 * 86_400_000)
    assert _run(get())["authenticated"] is True


# 6 -------------------------------------------------------------------------
def _row(ts_ms, kind="real"):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ms / 1000)) + ".000Z"
    return {"type": "assistant", "timestamp": iso, "message": {"model": "claude-opus-4-8", "content": "ok"}}


def test_6_concurrent_scans_advance_the_offset_once(tmp_path, monkeypatch):
    ps._auth_evidence_cache.clear()
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps(_row(1_000_000_000)) + "\n")
    ps._auth_turn_evidence([f])
    with open(f, "a") as fh:
        for i in range(5):
            fh.write(json.dumps(_row(2_000_000_000 + i * 1000)) + "\n")
    real_scan = ps._scan_lines

    def slow_scan(*a, **k):
        time.sleep(0.05)  # widen the race window
        return real_scan(*a, **k)
    monkeypatch.setattr(ps, "_scan_lines", slow_scan)
    ts = [threading.Thread(target=ps._auth_turn_evidence, args=([f],)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert ps._auth_evidence_cache[str(f)]["offset"] == f.stat().st_size


# 7 -------------------------------------------------------------------------
def _verify_env(tmp_path, monkeypatch, full_before):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    now = int(time.time() * 1000)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS, "refreshToken": REFRESH,
                                                   "expiresAt": now + 3_600_000}}))
    monkeypatch.setattr(ps, "CREDENTIALS_FILE", creds)
    pane = FakePane(monkeypatch, visible=CODE_PROMPT, full=full_before)

    def rewrite():
        time.sleep(0.02)
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": ACCESS + "n", "refreshToken": REFRESH,
                                                       "expiresAt": now + 7_200_000}}))
    return pane, rewrite


def test_7_real_success_is_seen_even_when_old_lines_scrolled_away(tmp_path, monkeypatch):
    old = " Login successful. Press Enter to continue…\n > \n" * 2
    pane, rewrite = _verify_env(tmp_path, monkeypatch, old + CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            assert (await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})).json()["injected"]
            rewrite()
            pane.visible = CODE_PROMPT + CODE + "\n Login successful. Press Enter to continue…\n"
            pane.full = pane.visible  # the 300-line window lost the old lines
            return (await c.get("/api/auth/verify", headers=HEADERS)).json()
    assert _run(scenario())["confirmed"] is True


def test_7_old_success_text_above_the_prompt_is_not_a_new_signin(tmp_path, monkeypatch):
    pane, rewrite = _verify_env(tmp_path, monkeypatch, CODE_PROMPT)

    async def scenario():
        async with _client() as c:
            assert (await c.post("/api/auth/code", headers=HEADERS, json={"code": CODE})).json()["injected"]
            rewrite()  # a background refresh
            redraw = " Login successful. Press Enter to continue…\n > \n"
            pane.visible = redraw
            pane.full = CODE_PROMPT + redraw * 3  # a redraw repeats old text
            return (await c.get("/api/auth/verify", headers=HEADERS)).json()
    assert _run(scenario())["confirmed"] is False


# 8 -------------------------------------------------------------------------
def test_8_account_label_survives_a_half_written_claude_json(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cj = home / ".claude.json"
    cj.write_text(json.dumps({"oauthAccount": {"emailAddress": "dave@example.com"}}))
    ps._account_label_cache.clear()
    put, get = _status_env(tmp_path, monkeypatch)
    exp = int(time.time() * 1000) + 3_600_000
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=exp)
    assert _run(get())["account"] == "dave@example.com"
    time.sleep(0.02)
    cj.write_text('{"oauthAccount": {"emailAddr')  # Claude Code mid-rewrite
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=exp + 1)
    assert _run(get())["account"] == "dave@example.com"


# 9 -------------------------------------------------------------------------
def test_9_stacked_leftover_screens_are_closed_one_key_at_a_time(monkeypatch):
    screens = [CODE_PROMPT, LOGIN_PICKER, IDLE_PROMPT]
    pane = FakePane(monkeypatch, visible=screens[0], full="")
    pane.keys = KeyLog()

    def on_key(k):
        if k == ["Escape"] and screens:
            screens.pop(0)
            pane.visible = screens[0] if screens else IDLE_PROMPT
    pane.keys.hook = on_key
    monkeypatch.setattr(ps, "AUTH_CLAUDE_START_TIMEOUT_S", 1.0, raising=False)
    monkeypatch.setattr(ps, "AUTH_MAX_RETRIES", 0, raising=False)
    _run(ps._run_auth_state_machine("%1"), timeout=20)
    assert pane.keys[:4] == [["Escape"], ["Escape"], ["-l", "/login"], ["Enter"]], pane.keys


# 10 ------------------------------------------------------------------------
def test_10_start_is_refused_while_prewarm_is_typing(monkeypatch):
    pane = FakePane(monkeypatch, state="shell", visible="$ ", full="")

    async def slow_state(_p):
        await asyncio.sleep(1.0)
        return "shell"
    monkeypatch.setattr(ps, "_pane_process_state", slow_state)

    async def scenario():
        async with _client() as c:
            pre = asyncio.create_task(c.post("/api/auth/prewarm", headers=HEADERS))
            await asyncio.sleep(0.3)
            r = await asyncio.wait_for(c.post("/api/auth/start", headers=HEADERS), timeout=3)
            await pre
            return r.json()
    body = _run(scenario())
    assert body.get("in_progress") is True, body
