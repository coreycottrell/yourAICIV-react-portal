"""Tests for the yourAICIV trial contract (portal side).

Run:  python3 -m pytest tests/ -q
Needs: pytest starlette httpx aiosqlite uvicorn (see requirements.txt)
"""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trial_gate  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
PAY = "https://buy.stripe.com/5kQeVe8Xe9D53GZdLb1Fe06"


def contract(start=T0, days=7, **over):
    raw = {
        "trial": True,
        "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_days": days,
        "expires_at": (start + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payment_url": PAY,
        "brand": "yourAICIV",
        "reseller": "Travis Morehead",
        "model": "MiniMax-M3",
    }
    raw.update(over)
    return raw


# ---------------------------------------------------------------- pure logic

def test_absent_file_is_not_a_trial():
    s = trial_gate.compute_state(None, T0)
    assert s["trial"] is False and s["expired"] is False


def test_trial_false_is_not_a_trial():
    s = trial_gate.compute_state(contract(trial=False), T0 + timedelta(days=30))
    assert s["trial"] is False and s["expired"] is False


def test_contract_response_shape():
    s = trial_gate.compute_state(contract(), T0 + timedelta(hours=1))
    for key in ("trial", "day", "days_left", "expires_at", "expired", "payment_url"):
        assert key in s
    assert isinstance(s["day"], int) and isinstance(s["days_left"], int)


@pytest.mark.parametrize(
    "offset,day,days_left",
    [
        (timedelta(minutes=1), 1, 7),
        (timedelta(hours=23, minutes=59), 1, 7),
        (timedelta(days=1), 2, 6),
        (timedelta(days=3, hours=5), 4, 4),
        (timedelta(days=6, hours=23), 7, 1),
    ],
)
def test_day_counting(offset, day, days_left):
    s = trial_gate.compute_state(contract(), T0 + offset)
    assert (s["day"], s["days_left"], s["expired"]) == (day, days_left, False)


def test_expiry_boundary():
    s = trial_gate.compute_state(contract(), T0 + timedelta(days=7))
    assert s["expired"] is True and s["days_left"] == 0 and s["day"] == 7
    assert s["payment_url"] == PAY
    assert s["expires_at"] == "2026-10-04T12:00:00Z"


def test_expires_derived_from_started_when_missing():
    raw = contract()
    del raw["expires_at"]
    assert trial_gate.compute_state(raw, T0 + timedelta(days=8))["expired"] is True


def test_trial_without_dates_fails_closed():
    raw = contract()
    del raw["expires_at"], raw["started_at"]
    s = trial_gate.compute_state(raw, T0)
    assert s["trial"] is True and s["expired"] is True and s["config_error"] is True
    assert s["payment_url"].startswith("https://")


def test_non_https_payment_url_is_replaced():
    s = trial_gate.compute_state(contract(payment_url="javascript:alert(1)"), T0)
    assert s["payment_url"].startswith("https://")


# ------------------------------------------------------------- file reading

def test_reads_file_from_civ_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trial.json").write_text(json.dumps(contract()))
    monkeypatch.delenv("TRIAL_CONFIG_PATH", raising=False)
    monkeypatch.setenv("CIV_ROOT", str(tmp_path))
    trial_gate._cache.update(path=None)
    assert trial_gate.trial_state(T0 + timedelta(days=2))["day"] == 3


def _use_record(monkeypatch, path, civ_root):
    monkeypatch.setenv("TRIAL_CONFIG_PATH", str(path))
    monkeypatch.setenv("CIV_ROOT", str(civ_root))
    trial_gate._cache.update(path=None)


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"trial": "false"}', '{"started_at": "2026-09-27T12:00:00Z"}', ""],
)
def test_corrupt_record_fails_closed(tmp_path, monkeypatch, content):
    f = tmp_path / "op" / "trial.json"
    f.parent.mkdir()
    f.write_text(content)
    _use_record(monkeypatch, f, tmp_path / "civ")
    s = trial_gate.trial_state(T0)
    assert (s["trial"], s["expired"], s.get("config_error")) == (True, True, True)
    assert trial_gate.is_expired_trial() is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_record_fails_closed(tmp_path, monkeypatch):
    f = tmp_path / "op" / "trial.json"
    f.parent.mkdir()
    f.write_text(json.dumps(contract(trial=False)))
    f.chmod(0)
    try:
        _use_record(monkeypatch, f, tmp_path / "civ")
        assert trial_gate.trial_state(T0)["config_error"] is True
    finally:
        f.chmod(0o644)


def test_operator_copy_wins_over_civ_copy(tmp_path, monkeypatch):
    """The civ can rewrite its own file; only the operator copy decides."""
    civ = tmp_path / "civ"
    (civ / "config").mkdir(parents=True)
    (civ / "config" / "trial.json").write_text(json.dumps(contract(trial=False)))
    op = tmp_path / "etc" / "trial.json"
    op.parent.mkdir()
    op.write_text(json.dumps(contract()))
    _use_record(monkeypatch, op, civ)
    s = trial_gate.trial_state(T0 + timedelta(days=9))
    assert s["trial"] is True and s["expired"] is True and "config_error" not in s


def test_missing_operator_copy_with_trial_marker_fails_closed(tmp_path, monkeypatch):
    civ = tmp_path / "civ"
    (civ / "config").mkdir(parents=True)
    (civ / "config" / "trial.json").write_text(json.dumps(contract(trial=False)))  # civ tried to ungate
    _use_record(monkeypatch, tmp_path / "etc" / "trial.json", civ)
    s = trial_gate.trial_state(T0)
    assert (s["trial"], s["expired"], s.get("config_error")) == (True, True, True)


def test_missing_record_without_marker_is_paid(tmp_path, monkeypatch):
    _use_record(monkeypatch, tmp_path / "etc" / "trial.json", tmp_path / "civ")
    assert trial_gate.trial_state(T0)["trial"] is False
    monkeypatch.delenv("TRIAL_CONFIG_PATH")
    trial_gate._cache.update(path=None)
    assert trial_gate.trial_state(T0)["trial"] is False


def test_startup_line_names_the_source(tmp_path, monkeypatch):
    _use_record(monkeypatch, tmp_path / "etc" / "trial.json", tmp_path)
    assert "operator copy" in trial_gate.describe_source()
    monkeypatch.delenv("TRIAL_CONFIG_PATH")
    assert "civ-writable" in trial_gate.describe_source()


# --------------------------------------------------------------- middleware

def _mini_app(state):
    async def ok(request):
        return JSONResponse({"ok": True})

    async def page(request):
        return PlainTextResponse("<html>spa</html>")

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    async def trial(request):
        return JSONResponse(state)

    return Starlette(
        routes=[
            Route("/", page),
            Route("/health", ok),
            Route("/api/status", ok),
            Route("/api/chat/send", ok, methods=["POST"]),
            Route("/api/trial", trial),
            Route("/api/panes", ok),
            Route("/api/inject/pane", ok, methods=["POST"]),
            Route("/api/resume", ok, methods=["POST"]),
            Route("/api/browser/{action}", ok),
            Route("/api/context", ok),
            WebSocketRoute("/ws/chat", ws),
            WebSocketRoute("/ws/terminal", ws),
            WebSocketRoute("/ws/browser", ws),
        ],
        middleware=[Middleware(trial_gate.TrialGateMiddleware, state_fn=lambda: state)],
    )


def test_middleware_passes_through_when_active():
    state = trial_gate.compute_state(contract(), T0 + timedelta(days=1))
    c = TestClient(_mini_app(state))
    assert c.get("/api/status").status_code == 200
    with c.websocket_connect("/ws/chat") as w:
        assert w.receive_text() == "hello"


def test_middleware_blocks_when_expired():
    state = trial_gate.compute_state(contract(), T0 + timedelta(days=9))
    c = TestClient(_mini_app(state))
    r = c.get("/api/status")
    assert r.status_code == 402
    assert r.json()["error"] == "trial_expired" and r.json()["payment_url"] == PAY
    assert c.post("/api/chat/send", json={"text": "hi"}).status_code == 402
    # still reachable: trial endpoint, SPA shell, health
    assert c.get("/api/trial").status_code == 200
    assert c.get("/").status_code == 200
    assert c.get("/health").status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/chat") as w:
            w.receive_text()
    assert exc.value.code == 4402


def test_middleware_no_gating_for_non_trial():
    state = trial_gate.compute_state(None, T0)
    c = TestClient(_mini_app(state))
    assert c.get("/api/status").status_code == 200
    # paid: operator tools untouched
    assert c.get("/api/panes").status_code == 200
    assert c.post("/api/inject/pane").status_code == 200
    assert c.get("/api/browser/screenshot").status_code == 200
    for ws in ("/ws/terminal", "/ws/browser"):
        with c.websocket_connect(ws) as w:
            assert w.receive_text() == "hello"


def _assert_operator_locked(c, http_code, ws_code):
    assert c.get("/api/panes").status_code == http_code
    assert c.post("/api/inject/pane").status_code == http_code
    assert c.post("/api/resume").status_code == http_code
    assert c.get("/api/browser/screenshot").status_code == http_code
    for ws in ("/ws/terminal", "/ws/browser"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(ws) as w:
                w.receive_text()
        assert exc.value.code == ws_code


def test_operator_tools_locked_during_active_trial():
    state = trial_gate.compute_state(contract(), T0 + timedelta(days=1))
    c = TestClient(_mini_app(state))
    _assert_operator_locked(c, 403, 4403)
    assert c.get("/api/panes").json()["error"] == "operator_tools_locked"
    # the rest of the portal is untouched
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/context").status_code == 200
    with c.websocket_connect("/ws/chat") as w:
        assert w.receive_text() == "hello"


def test_operator_tools_locked_when_expired_or_failed_closed():
    expired = trial_gate.compute_state(contract(), T0 + timedelta(days=9))
    _assert_operator_locked(TestClient(_mini_app(expired)), 402, 4402)
    _assert_operator_locked(TestClient(_mini_app(trial_gate.fail_closed_state())), 402, 4402)


# ------------------------------------------------ real portal_server wiring

@pytest.fixture()
def portal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIV_ROOT", str(home))
    monkeypatch.delenv("TRIAL_CONFIG_PATH", raising=False)
    monkeypatch.setenv("PORTAL_TOKEN_FILE", str(tmp_path / "token"))
    (tmp_path / "token").write_text("test-token")
    sys.modules.pop("portal_server", None)
    mod = importlib.import_module("portal_server")
    trial_gate._cache.update(path=None)
    return mod, home


def _write(home, raw):
    (home / "config" / "trial.json").write_text(json.dumps(raw))
    trial_gate._cache.update(path=None)


def test_portal_server_trial_endpoint_and_gate(portal):
    mod, home = portal
    c = TestClient(mod.app)
    auth = {"Authorization": "Bearer test-token"}

    # no file -> not a trial, nothing gated
    assert c.get("/api/trial").json()["trial"] is False
    assert c.get("/api/release-notes", headers=auth).status_code != 402

    # active trial
    now = datetime.now(UTC)
    _write(home, contract(start=now - timedelta(days=2, hours=1)))
    t = c.get("/api/trial").json()
    assert t["trial"] is True and t["day"] == 3 and t["expired"] is False
    assert c.get("/api/release-notes", headers=auth).status_code != 402

    # expired -> 402 everywhere under /api except /api/trial
    _write(home, contract(start=now - timedelta(days=8)))
    t = c.get("/api/trial").json()
    assert t["expired"] is True and t["payment_url"] == PAY
    r = c.get("/api/release-notes", headers=auth)
    assert r.status_code == 402 and r.json()["payment_url"] == PAY
    assert c.post("/api/chat/send", headers=auth, json={"message": "hi"}).status_code == 402
    assert c.get("/health").status_code == 200

    # conversion: operator flips trial=false -> access restored, file untouched otherwise
    _write(home, contract(start=now - timedelta(days=8), trial=False))
    assert c.get("/api/trial").json()["trial"] is False
    assert c.get("/api/release-notes", headers=auth).status_code != 402


def test_router_engine_skips_claude_signin(portal):
    """An M3 trial (router engine) must never show the Claude sign-in overlay."""
    mod, home = portal
    c = TestClient(mod.app)
    auth = {"Authorization": "Bearer test-token"}
    # no credentials, no trial file -> Claude sign-in required
    r = c.get("/api/auth/status", headers=auth).json()
    assert r["authenticated"] is False
    # trial file names a non-Claude model -> managed engine
    _write(home, contract(start=datetime.now(UTC)))
    r = c.get("/api/auth/status", headers=auth).json()
    assert r["authenticated"] is True and r["managed"] is True


def test_injection_paths_require_auth(portal):
    """Scheduled messages are typed into the AI's session: never unauthenticated."""
    mod, _home = portal
    c = TestClient(mod.app)
    assert c.post("/api/schedule-task", json={"message": "x", "fire_at": "2030-01-01T00:00:00Z"}).status_code == 401
    assert c.get("/api/scheduled-tasks").status_code == 401
    assert c.delete("/api/scheduled-tasks/abc").status_code == 401
    assert c.post("/api/reaction", json={"msg_id": "m", "emoji": "x"}).status_code == 401
    assert c.get("/api/reaction/summary").status_code == 401


def test_operator_tools_locked_in_real_portal(portal):
    """Wiring check against portal_server itself (never reaches tmux: refused first)."""
    mod, home = portal
    c = TestClient(mod.app)
    auth = {"Authorization": "Bearer test-token"}
    _write(home, contract(start=datetime.now(UTC) - timedelta(days=1)))
    assert c.get("/api/panes", headers=auth).status_code == 403
    assert c.post("/api/inject/pane", headers=auth, json={"pane_id": "%0", "message": "x"}).status_code == 403
    assert c.post("/api/resume", headers=auth).status_code == 403
    assert c.get("/api/browser/screenshot", headers=auth).status_code == 403
    for ws in ("/ws/terminal", "/ws/browser"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(f"{ws}?token=test-token") as w:
                w.receive_text()
        assert exc.value.code == 4403


def test_whatsapp_routes_removed(portal):
    mod, _home = portal
    c = TestClient(mod.app)
    auth = {"Authorization": "Bearer test-token"}
    assert c.get("/api/whatsapp/status", headers=auth).status_code == 404
    assert c.get("/api/whatsapp/qr", headers=auth).status_code == 404


def test_agent_status_is_loopback_or_token_only(portal, tmp_path, monkeypatch):
    import asyncio

    mod, _home = portal
    monkeypatch.setattr(mod, "AGENTS_DB", tmp_path / "agents.db")
    asyncio.run(mod._init_agents_db())
    body = {"agent": "helper", "status": "working", "task": "x"}

    remote = TestClient(mod.app, client=("203.0.113.9", 40000))
    assert remote.post("/api/agents/status", json=body).status_code == 401
    assert remote.post(
        "/api/agents/status", json=body, headers={"Authorization": "Bearer test-token"}
    ).status_code == 200
    assert remote.post(
        "/api/agents/status", json=body, headers={"Authorization": "Bearer wrong"}
    ).status_code == 401

    for host in ("127.0.0.1", "::1"):
        local = TestClient(mod.app, client=(host, 40000))
        assert local.post("/api/agents/status", json=body).status_code == 200

    # a reverse proxy on this host makes remote visitors look local: not trusted
    proxied = TestClient(mod.app, client=("127.0.0.1", 40000))
    for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        assert proxied.post("/api/agents/status", json=body, headers={h: "203.0.113.9"}).status_code == 401
