"""The resume button launches Claude with the civ's own launch model, never a hardcoded name.

A fixed claude-sonnet-4-6 here made the M3 trial's "no frontier model reachable" check fail
(Witness ticket 3350). The template's launchers read config/launch_model.txt; so does the portal.
"""
import importlib
import sys

import pytest


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
    return importlib.import_module("portal_server"), home


def test_trial_pin_is_used(portal):
    mod, home = portal
    (home / "config" / "launch_model.txt").write_text("MiniMax-M3\n")
    assert mod._launch_model_flag() == " --model MiniMax-M3"


def test_no_pin_means_no_model_flag(portal):
    mod, _ = portal
    assert mod._launch_model_flag() == ""


def test_unsafe_value_is_ignored(portal):
    mod, home = portal
    (home / "config" / "launch_model.txt").write_text("x; rm -rf /\n")
    assert mod._launch_model_flag() == ""


def test_no_hardcoded_model_in_resume():
    src = open("portal_server.py").read()
    assert "claude --model claude-" not in src
