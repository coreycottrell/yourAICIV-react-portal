"""The portal keeps its birth settings across any restart path.

The birth writes PORTAL_PUBLIC_URL and TRIAL_CONFIG_PATH to ~/.env and to the
portal's process env. A watchdog restart through start.sh used to drop both.
Now the portal (portal_server, trial_gate, start.sh) reads a key from ~/.env
when the process env does not have it; the process env still wins.

Run:  python3 -m pytest tests/ -q
"""
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import env_file  # noqa: E402

KEYS = ("PORTAL_PUBLIC_URL", "TRIAL_CONFIG_PATH")
URL = "https://nova-travis.ai-civ.com"


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """A fake home, and the two keys restored exactly as they were afterwards.

    The code under test writes os.environ directly, which monkeypatch does not
    undo for keys that were absent, so restore them by hand.
    """
    saved = {k: os.environ.get(k) for k in KEYS}
    for k in KEYS:
        os.environ.pop(k, None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIV_ROOT", str(home))
    yield home
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ------------------------------------------------------------- the parser

def test_parse_handles_comments_quotes_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=yes\n"
        'DQ="with space # not a comment"\n'
        "SQ='single'\n"
        "INLINE=bare  # trailing comment\n"
        "HASHY=a#b\n"
        "EMPTY=\n"
        "  SPACED = padded  \n"
        "not a line\n"
        "DUP=first\n"
        "DUP=second\n"
    )
    got = env_file.parse_env_file(f)
    assert got["PLAIN"] == "value"
    assert got["EXPORTED"] == "yes"
    assert got["DQ"] == "with space # not a comment"
    assert got["SQ"] == "single"
    assert got["INLINE"] == "bare"
    assert got["HASHY"] == "a#b"
    assert got["EMPTY"] == ""
    assert got["SPACED"] == "padded"
    assert got["DUP"] == "second"
    assert "not a line" not in got


def test_missing_file_is_not_an_error(tmp_path):
    assert env_file.parse_env_file(tmp_path / "nope" / ".env") == {}
    env = {"HOME": str(tmp_path / "nope")}
    assert env_file.load_env_defaults(environ=env) == {}
    assert "PORTAL_PUBLIC_URL" not in env and "TRIAL_CONFIG_PATH" not in env


# ------------------------------------------------- precedence (pure mapping)

def test_value_only_in_home_env_is_used(tmp_path):
    (tmp_path / ".env").write_text(f'PORTAL_PUBLIC_URL="{URL}"\nTRIAL_CONFIG_PATH=/etc/aiciv/trial.json\n')
    env = {"HOME": str(tmp_path)}
    loaded = env_file.load_env_defaults(environ=env)
    assert loaded == {"PORTAL_PUBLIC_URL": URL, "TRIAL_CONFIG_PATH": "/etc/aiciv/trial.json"}
    assert env["PORTAL_PUBLIC_URL"] == URL
    assert env["TRIAL_CONFIG_PATH"] == "/etc/aiciv/trial.json"


def test_process_env_overrides_env_file(tmp_path):
    (tmp_path / ".env").write_text(f"PORTAL_PUBLIC_URL={URL}\nTRIAL_CONFIG_PATH=/from/file.json\n")
    env = {"HOME": str(tmp_path), "PORTAL_PUBLIC_URL": "https://from-process.example",
           "TRIAL_CONFIG_PATH": "/from/process.json"}
    assert env_file.load_env_defaults(environ=env) == {}
    assert env["PORTAL_PUBLIC_URL"] == "https://from-process.example"
    assert env["TRIAL_CONFIG_PATH"] == "/from/process.json"


def test_blank_process_value_counts_as_missing(tmp_path):
    (tmp_path / ".env").write_text(f"PORTAL_PUBLIC_URL={URL}\n")
    env = {"HOME": str(tmp_path), "PORTAL_PUBLIC_URL": "  "}
    env_file.load_env_defaults(environ=env)
    assert env["PORTAL_PUBLIC_URL"] == URL


def test_civ_root_env_is_read_after_home_env(tmp_path):
    home, civ = tmp_path / "home", tmp_path / "civ"
    home.mkdir(), civ.mkdir()
    (home / ".env").write_text(f"PORTAL_PUBLIC_URL={URL}\n")
    (civ / ".env").write_text("PORTAL_PUBLIC_URL=https://loser.example\nTRIAL_CONFIG_PATH=/civ/trial.json\n")
    env = {"HOME": str(home), "CIV_ROOT": str(civ)}
    env_file.load_env_defaults(environ=env)
    assert env["PORTAL_PUBLIC_URL"] == URL                 # ~/.env first
    assert env["TRIAL_CONFIG_PATH"] == "/civ/trial.json"   # only in $CIV_ROOT/.env


def test_only_persisted_keys_are_loaded(tmp_path):
    (tmp_path / ".env").write_text("PORT=1\nSECRET_KEY=x\n")
    env = {"HOME": str(tmp_path)}
    assert env_file.load_env_defaults(environ=env) == {}
    assert "PORT" not in env and "SECRET_KEY" not in env


# --------------------------------------------- the real modules at startup

def test_trial_gate_uses_trial_config_path_from_home_env(clean_env):
    (clean_env / ".env").write_text("TRIAL_CONFIG_PATH=/etc/aiciv/trial.json\n")
    import trial_gate
    try:
        importlib.reload(trial_gate)
        assert trial_gate.trial_config_path() == Path("/etc/aiciv/trial.json")
        assert "operator copy" in trial_gate.describe_source()
    finally:
        os.environ.pop("TRIAL_CONFIG_PATH", None)
        importlib.reload(trial_gate)


def test_trial_gate_process_env_wins(clean_env):
    (clean_env / ".env").write_text("TRIAL_CONFIG_PATH=/from/file.json\n")
    os.environ["TRIAL_CONFIG_PATH"] = "/from/process.json"
    import trial_gate
    try:
        importlib.reload(trial_gate)
        assert trial_gate.trial_config_path() == Path("/from/process.json")
    finally:
        os.environ.pop("TRIAL_CONFIG_PATH", None)
        importlib.reload(trial_gate)


def test_trial_gate_without_env_file_falls_back_quietly(clean_env):
    import trial_gate
    importlib.reload(trial_gate)
    assert trial_gate.trial_config_path() == clean_env / "config" / "trial.json"


def _import_portal(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_TOKEN_FILE", str(tmp_path / "token"))
    (tmp_path / "token").write_text("test-token")
    sys.modules.pop("portal_server", None)
    return importlib.import_module("portal_server")


def test_portal_server_loads_both_keys_from_home_env(clean_env, tmp_path, monkeypatch):
    (clean_env / ".env").write_text(
        f"# written at birth\nexport PORTAL_PUBLIC_URL='{URL}'\nTRIAL_CONFIG_PATH=\"/etc/aiciv/trial.json\"\n"
    )
    import trial_gate
    try:
        mod = _import_portal(tmp_path, monkeypatch)
        assert os.environ["PORTAL_PUBLIC_URL"] == URL
        assert os.environ["TRIAL_CONFIG_PATH"] == "/etc/aiciv/trial.json"
        assert set(mod._ENV_FILE_LOADED) == set(KEYS)
        assert trial_gate.trial_config_path() == Path("/etc/aiciv/trial.json")
    finally:
        os.environ.pop("TRIAL_CONFIG_PATH", None)
        trial_gate._cache.update(path=None)


def test_portal_server_process_env_wins(clean_env, tmp_path, monkeypatch):
    (clean_env / ".env").write_text("PORTAL_PUBLIC_URL=https://from-file.example\n")
    os.environ["PORTAL_PUBLIC_URL"] = URL
    mod = _import_portal(tmp_path, monkeypatch)
    assert os.environ["PORTAL_PUBLIC_URL"] == URL
    assert mod._ENV_FILE_LOADED == {}


def test_portal_server_without_env_file_starts(clean_env, tmp_path, monkeypatch):
    mod = _import_portal(tmp_path, monkeypatch)
    assert mod._ENV_FILE_LOADED == {}
    assert "PORTAL_PUBLIC_URL" not in os.environ


# ------------------------------------------------------------------ start.sh

def _run_start_sh(tmp_path, home, extra_env=None):
    """Run the real start.sh against a stub portal_server that prints its env."""
    app = tmp_path / "app"
    (app / "react-portal" / "dist").mkdir(parents=True)
    (app / "react-portal" / "dist" / "index.html").write_text("<html></html>")
    shutil.copy(ROOT / "start.sh", app / "start.sh")
    (app / "portal_server.py").write_text(
        "import os\n"
        "for k in ('PORTAL_PUBLIC_URL', 'TRIAL_CONFIG_PATH'):\n"
        "    print(f'{k}=[{os.environ.get(k, \"<unset>\")}]')\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(HOME=str(home), CIV_ROOT=str(home))
    env.update(extra_env or {})
    out = subprocess.run(["bash", str(app / "start.sh"), "9999"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_start_sh_exports_values_only_in_home_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "# birth\n"
        f'export PORTAL_PUBLIC_URL="{URL}"  \n'
        "TRIAL_CONFIG_PATH=/etc/aiciv/trial.json  # operator copy\n"
    )
    out = _run_start_sh(tmp_path, home)
    assert f"PORTAL_PUBLIC_URL=[{URL}]" in out
    assert "TRIAL_CONFIG_PATH=[/etc/aiciv/trial.json]" in out


def test_start_sh_process_env_wins(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("PORTAL_PUBLIC_URL=https://from-file.example\nTRIAL_CONFIG_PATH=/from/file.json\n")
    out = _run_start_sh(tmp_path, home, {"PORTAL_PUBLIC_URL": URL,
                                         "TRIAL_CONFIG_PATH": "/from/process.json"})
    assert f"PORTAL_PUBLIC_URL=[{URL}]" in out
    assert "TRIAL_CONFIG_PATH=[/from/process.json]" in out


def test_start_sh_without_env_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = _run_start_sh(tmp_path, home)
    assert "PORTAL_PUBLIC_URL=[<unset>]" in out
    assert "TRIAL_CONFIG_PATH=[<unset>]" in out
