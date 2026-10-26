"""Default installation prefixes, and choice of the install location."""
import os.path as op

import pytest

from matlab_runtime import cli, impl, utils
from matlab_runtime.utils import askprefix, user_prefix


@pytest.fixture
def prefixes(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(utils, "guess_prefix", lambda: "/sys/prefix")
    monkeypatch.setattr(utils, "user_prefix", lambda: "/home/me/prefix")
    monkeypatch.delenv("MATLAB_RUNTIME_PATH", raising=False)
    return {"user": "/home/me/prefix", "system": "/sys/prefix"}


def answers(monkeypatch, *replies):
    """Make `input()` return `replies` in order; return the prompts."""
    replies, prompts = list(replies), []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return replies.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


# --- user_prefix --------------------------------------------------------


def set_home(monkeypatch, home):
    # `~` is $HOME on unix, but %USERPROFILE% on Windows
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_user_prefix_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "guess_arch", lambda: "glnxa64")
    set_home(monkeypatch, tmp_path / "me")
    default = op.join(str(tmp_path / "me"), ".local", "share", "MATLAB",
                      "MATLAB_Runtime")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert user_prefix() == default
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert user_prefix() == op.join(str(tmp_path / "data"), "MATLAB",
                                    "MATLAB_Runtime")
    # Relative paths are invalid and ignored (XDG specification)
    monkeypatch.setenv("XDG_DATA_HOME", "data")
    assert user_prefix() == default


def test_user_prefix_mac(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "guess_arch", lambda: "maca64")
    set_home(monkeypatch, tmp_path / "me")
    assert user_prefix() == op.join(
        str(tmp_path / "me"), "Applications", "MATLAB", "MATLAB_Runtime"
    )


def test_user_prefix_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "guess_arch", lambda: "win64")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert user_prefix() == op.join(
        str(tmp_path / "appdata"), "Programs", "MATLAB", "MATLAB Runtime"
    )
    monkeypatch.delenv("LOCALAPPDATA")
    set_home(monkeypatch, tmp_path / "me")
    assert user_prefix() == op.join(
        str(tmp_path / "me"), "AppData", "Local", "Programs", "MATLAB",
        "MATLAB Runtime"
    )


# --- askprefix ----------------------------------------------------------


@pytest.mark.parametrize("default", ["user", "system"])
def test_askprefix_enter_uses_default(prefixes, monkeypatch, default):
    prompts = answers(monkeypatch, "")
    assert askprefix(default) == prefixes[default]
    assert f"[{default}]" in prompts[0]


@pytest.mark.parametrize("reply,scope", [
    ("user", "user"), ("U", "user"), ("1", "user"), ("local", "user"),
    ("system", "system"), ("s", "system"), ("2", "system"),
    ("global", "system"),
])
def test_askprefix_choices(prefixes, monkeypatch, reply, scope):
    answers(monkeypatch, reply)
    assert askprefix("custom") == prefixes[scope]


def test_askprefix_custom(prefixes, monkeypatch):
    answers(monkeypatch, "3", "", "~/mcr")
    assert askprefix() == op.expanduser("~/mcr")
    answers(monkeypatch, "", "/opt/mcr")
    assert askprefix("custom") == "/opt/mcr"


def test_askprefix_asks_again(prefixes, monkeypatch):
    prompts = answers(monkeypatch, "maybe", "4", "user")
    assert askprefix() == prefixes["user"]
    assert len(prompts) == 3


def test_askprefix_auto_answer(prefixes, monkeypatch):
    answers(monkeypatch)  # input() must not be called
    assert askprefix(None, auto_answer=True) == prefixes["system"]
    assert askprefix("user", auto_answer=True) == prefixes["user"]
    with pytest.raises(ValueError):
        askprefix("custom", auto_answer=True)


def test_askprefix_invalid_default(prefixes):
    with pytest.raises(ValueError):
        askprefix("everywhere")


# --- install / uninstall ------------------------------------------------


def test_runtime_path_skips_question(prefixes, monkeypatch):
    answers(monkeypatch)  # input() must not be called
    monkeypatch.setenv("MATLAB_RUNTIME_PATH", "/env/prefix")
    monkeypatch.setattr(impl, "guess_prefix", lambda: "/env/prefix")
    assert impl._default_prefix("user") == "/env/prefix"


def test_install_several_versions_asks_once(prefixes, monkeypatch):
    calls = []
    monkeypatch.setattr(impl, "askprefix",
                        lambda *a: calls.append(a) or "/chosen")
    monkeypatch.setattr(impl, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(impl, "guess_release", lambda *a: a[0])
    monkeypatch.setattr(impl, "guess_installer", lambda *a: "url")
    seen = []
    monkeypatch.setattr(
        impl, "find_runtime_in_prefix",
        lambda version, prefix: seen.append(prefix) or prefix,
    )
    # The runtime exists: answer "no" to "Reinstall?"
    monkeypatch.setattr(impl, "askuser", lambda *a: False)
    impl.install(["R2024b", "R2025a"], default_scope="user")
    assert calls == [("user", False)]
    assert seen == [op.realpath("/chosen")] * 2


def test_cli_default_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "uninstall", lambda *a, **k: calls.append((a, k))
    )
    cli.main(["-u", "-v", "R2024b", "--default-scope", "user"])
    (args, kwargs), = calls
    assert args == (["R2024b"], None, False, "user")
    with pytest.raises(SystemExit):
        cli.main(["--default-scope", "everywhere"])
