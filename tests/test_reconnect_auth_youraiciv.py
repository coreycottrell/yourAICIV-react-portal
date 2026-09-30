"""yourAICIV-specific guards for the Reconnect Claude + real sign-in status port
(Witness ticket 3383).

- The M3-trial / router-backed engine keeps reporting managed:true (no personal
  Claude login is asked for), even after the real-credential status fix.
- THE BUG (negative test): a live tmux session must NOT make a signed-out CIV
  read as signed in. The old endpoint returned authenticated:true for any
  credentials file while the tmux session was alive.
- The awakening prompt keeps its 30 s settle time after the Claude-detection
  fix (it used to wait 30 s only because detection always failed).
- Nothing the status endpoint returns ever contains a token.
"""
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import portal_server as ps

ACCESS = "sk-ant-oat01-SECRET-ACCESS-TOKEN-do-not-leak"
REFRESH = "sk-ant-ort01-SECRET-REFRESH-TOKEN-do-not-leak"


@pytest.fixture(autouse=True)
def _never_pkill_claude(monkeypatch):
    async def fake_kill():
        return None
    monkeypatch.setattr(ps, "_kill_claude_process", fake_kill)


@pytest.fixture
def env(tmp_path, monkeypatch):
    creds = tmp_path / ".claude" / ".credentials.json"
    monkeypatch.setattr(ps, "CREDENTIALS_FILE", creds)

    class _OK:  # every tmux call "succeeds": the session is alive
        returncode = 0
        stdout = None

    async def tmux_alive(*a, **k):
        return _OK()

    async def fake_output(cmd, timeout=5):
        return "> ready"

    async def fake_pane():
        return "%0"
    monkeypatch.setattr(ps, "_run_subprocess_async", tmux_alive)
    monkeypatch.setattr(ps, "_run_subprocess_output", fake_output)
    monkeypatch.setattr(ps, "_find_primary_pane_async", fake_pane)
    monkeypatch.setattr(ps, "_find_all_project_jsonl", lambda: [])
    monkeypatch.setattr(ps, "_engine_is_managed", lambda: False)
    if hasattr(ps, "_auth_evidence_cache"):  # absent on the pre-fix code (negative control)
        ps._auth_evidence_cache.clear()
    client = TestClient(ps.app)
    headers = {"Authorization": f"Bearer {ps.BEARER_TOKEN}"}

    def put(**oauth):
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({"claudeAiOauth": oauth}))

    def get():
        r = client.get("/api/auth/status", headers=headers)
        assert r.status_code == 200
        assert ACCESS not in r.text and REFRESH not in r.text
        return r.json()
    return put, get, creds, monkeypatch


def test_no_credentials_with_live_tmux_is_signed_out(env):
    put, get, creds, _ = env
    body = get()
    assert body["authenticated"] is False
    assert body["reason"] == "no_credentials_file"


def test_BUG_expired_dead_token_with_live_tmux_is_signed_out(env):
    """The ticket-2153 / 3270 bug: the old code answered authenticated:true
    here because the tmux session was alive."""
    put, get, creds, _ = env
    now = int(time.time() * 1000)
    put(accessToken=ACCESS, refreshToken="", expiresAt=now - 5 * 86_400_000)
    body = get()
    assert body["authenticated"] is False
    assert body["reason"] == "expired_no_refresh_token"


def test_BUG_long_expired_with_refresh_and_live_tmux_is_signed_out(env):
    put, get, creds, _ = env
    now = int(time.time() * 1000)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now - 3 * 86_400_000)
    assert get()["authenticated"] is False


def test_valid_credentials_are_signed_in(env):
    put, get, creds, _ = env
    now = int(time.time() * 1000)
    put(accessToken=ACCESS, refreshToken=REFRESH, expiresAt=now + 8 * 3_600_000,
        subscriptionType="max")
    body = get()
    assert body["authenticated"] is True
    assert body["reason"] == "token_valid"
    assert body["subscription"] == "max"
    assert body["account"] is None


def test_managed_engine_still_reports_managed(env):
    put, get, creds, mp = env
    mp.setattr(ps, "_engine_is_managed", lambda: True)
    body = get()  # no credentials at all
    assert body["authenticated"] is True
    assert body["managed"] is True


def test_first_boot_settle_default_is_30s():
    assert ps.FIRST_BOOT_SETTLE_S == 30.0


def test_is_claude_running_no_longer_reads_devnull_stdout():
    """_run_subprocess_async discards stdout, so the old check could never see
    the pane command and always answered 'not running'."""
    import inspect
    src = inspect.getsource(ps._pane_process_state)
    assert "_run_subprocess_output" in src
    assert "r.stdout" not in inspect.getsource(ps._is_claude_running_async)


# ---------------------------------------------------------------------------
# /api/auth/close — tidy the LIVE session after a reconnect (cancel / success)
# ---------------------------------------------------------------------------

LOGIN_PICKER = """
 Select login method:

 ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
   2. Anthropic Console account · API usage billing
"""
CODE_PROMPT = """
 Browser didn't open? Use the url below to sign in:

 https://claude.ai/oauth/authorize?code=true&client_id=x&state=abc

 Paste code here if prompted >
"""
SUCCESS = """
 Login successful. Press Enter to continue…
"""
NORMAL = """
 > hello
 ⏺ Hi — ready.
"""


@pytest.mark.parametrize("screen,key", [
    (LOGIN_PICKER, "Escape"), (CODE_PROMPT, "Escape"), (SUCCESS, "Enter"),
    (NORMAL, None), ("", None),
    ("Login successful", None),  # no "Press Enter": nothing to press
])
def test_plan_auth_close(screen, key):
    assert ps._plan_auth_close(screen) == key


@pytest.fixture
def close_env(monkeypatch):
    sent = []
    screens = []

    async def fake_async(cmd, timeout=5, check=False):
        if cmd[:2] == ["tmux", "send-keys"]:
            sent.append(cmd[-1])
            if screens:
                screens.pop(0)

        class _R:
            returncode = 0
        return _R()

    async def fake_visible(pane):
        return screens[0] if screens else NORMAL

    async def fake_pane():
        return "%0"
    state = {"v": "claude"}

    async def fake_state(pane):
        return state["v"]
    monkeypatch.setattr(ps, "_run_subprocess_async", fake_async)
    monkeypatch.setattr(ps, "_capture_visible", fake_visible)
    monkeypatch.setattr(ps, "_find_primary_pane_async", fake_pane)
    monkeypatch.setattr(ps, "_pane_process_state", fake_state)

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(ps.asyncio, "sleep", no_sleep)
    client = TestClient(ps.app)
    headers = {"Authorization": f"Bearer {ps.BEARER_TOKEN}"}
    return client, headers, sent, screens, state


def test_close_requires_bearer(close_env):
    client, headers, sent, screens, state = close_env
    assert client.post("/api/auth/close").status_code == 401
    assert sent == []


def test_close_escapes_login_picker_in_live_claude(close_env):
    client, headers, sent, screens, state = close_env
    screens.append(LOGIN_PICKER)
    r = client.post("/api/auth/close", headers=headers).json()
    assert r["pressed"] == ["Escape"] and sent == ["Escape"]


def test_close_presses_enter_after_success(close_env):
    client, headers, sent, screens, state = close_env
    screens.append(SUCCESS)
    assert client.post("/api/auth/close", headers=headers).json()["pressed"] == ["Enter"]


def test_close_on_a_normal_screen_presses_nothing(close_env):
    client, headers, sent, screens, state = close_env
    r = client.post("/api/auth/close", headers=headers).json()
    assert r["closed"] is False and sent == []


@pytest.mark.parametrize("pane_state", ["shell", "unknown"])
def test_close_never_touches_a_pane_that_is_not_claude(close_env, pane_state):
    client, headers, sent, screens, state = close_env
    state["v"] = pane_state
    screens.append(LOGIN_PICKER)
    r = client.post("/api/auth/close", headers=headers).json()
    assert r["closed"] is False and sent == []


def test_plan_auth_close_ignores_an_old_login_screen_above_the_prompt():
    """After a cancel the picker text is still on screen above the prompt: a
    second Escape there would open Claude Code's rewind menu."""
    screen = LOGIN_PICKER + CODE_PROMPT + "\n (login cancelled)\n\n> \n"
    assert ps._plan_auth_close(screen) is None
    assert ps._plan_auth_close(SUCCESS + "\n> \n") is None


def test_close_presses_exactly_one_key(close_env):
    client, headers, sent, screens, state = close_env
    screens.extend([LOGIN_PICKER, LOGIN_PICKER, LOGIN_PICKER])
    client.post("/api/auth/close", headers=headers)
    assert sent == ["Escape"]


# ---------------------------------------------------------------------------
# Stale sign-in text in a LIVE session (found by the sandbox by-effect test):
# a second reconnect must never hand out the previous, dead URL.
# ---------------------------------------------------------------------------

OLD_URL = "https://claude.ai/oauth/authorize?code=true&client_id=x&state=OLD111"
NEW_URL = "https://claude.ai/oauth/authorize?code=true&client_id=x&state=NEW222"
FIRST_RUN = f"""> /login
 Select login method:
 ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
 Browser didn't open? Use the url below to sign in:
{OLD_URL}
 Paste code here if prompted >
 (login cancelled)
> """


def test_stale_url_is_not_a_fresh_url():
    base = ps._auth_screen_baseline(FIRST_RUN)
    assert ps._classify_auth_screen(FIRST_RUN + "/login\n", base) == "unknown"
    assert ps._fresh_oauth_url(FIRST_RUN, base["urls"]) is None


def test_new_login_menu_after_stale_one_is_detected():
    base = ps._auth_screen_baseline(FIRST_RUN)
    now = FIRST_RUN + "/login\n Select login method:\n ❯ 1. Claude account with subscription · Pro\n"
    assert ps._classify_auth_screen(now, base) == "login_menu"


def test_new_url_after_stale_one_is_the_one_returned():
    base = ps._auth_screen_baseline(FIRST_RUN)
    now = FIRST_RUN + f"/login\n Select login method:\n{NEW_URL}\n Paste code here if prompted >"
    assert ps._classify_auth_screen(now, base) == "oauth_url"
    assert ps._fresh_oauth_url(now, base["urls"]) == NEW_URL


def test_stale_login_successful_is_not_already_authenticated():
    screen = FIRST_RUN.replace(" (login cancelled)", " Login successful. Press Enter to continue…")
    base = ps._auth_screen_baseline(screen)
    assert ps._classify_auth_screen(screen + "/login\n", base) == "unknown"


def test_without_baseline_behaviour_is_unchanged():
    assert ps._classify_auth_screen(FIRST_RUN) == "oauth_url"


def test_auth_url_endpoint_skips_stale_urls(monkeypatch):
    async def fake_pane():
        return "%0"

    async def fake_output(cmd, timeout=5):
        return FIRST_RUN
    monkeypatch.setattr(ps, "_find_primary_pane_async", fake_pane)
    monkeypatch.setattr(ps, "_run_subprocess_output", fake_output)
    monkeypatch.setattr(ps, "_captured_oauth_url", None)
    monkeypatch.setattr(ps, "_stale_oauth_urls", {OLD_URL})
    client = TestClient(ps.app)
    r = client.get("/api/auth/url", headers={"Authorization": f"Bearer {ps.BEARER_TOKEN}"}).json()
    assert r == {"url": None, "ready": False}
