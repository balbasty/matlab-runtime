"""`install`: `if_missing`, and the questions asked to the user."""
import os
import os.path as op
import zipfile

import pytest

from matlab_runtime import cli, impl, utils
from matlab_runtime.utils import (
    LICENSE_URL,
    UnsupportedPythonError,
    UserInterruptionError,
)
from .test_runtime_discovery import (  # noqa: F401 (fixture)
    current_python,
    env,
    fake_runtime,
)

URL = "https://example.com/MATLAB_Runtime_R2025b_Update_7_glnxa64.zip"


def answers(monkeypatch, *replies):
    """Make `input()` return `replies` in order; return the prompts."""
    replies, prompts = list(replies), []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not replies:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return replies.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


@pytest.fixture
def stub(env, monkeypatch):  # noqa: F811
    """Isolated discovery; `_install` records its calls."""
    monkeypatch.setitem(utils.SUPPORTED_PYTHON_VERSIONS, "R2025b",
                        [current_python()])
    calls = []

    def _install(release, prefix, auto_answer, **kwargs):
        calls.append((release, prefix, kwargs))
        return release, fake_runtime(env["user"] / release, release)
    monkeypatch.setattr(impl, "_install", _install)
    return dict(env, calls=calls)


# --- if_missing ---------------------------------------------------------


def test_if_missing_found_is_silent(stub, monkeypatch, capsys):
    answers(monkeypatch)  # input() must not be called
    path = fake_runtime(stub["prefix"] / "R2025b", "R2025b")
    assert impl.install("R2025b", if_missing=True) == ("R2025b", path)
    assert not stub["calls"]
    assert capsys.readouterr().out == ""


def test_if_missing_installs(stub):
    release, path = impl.install("R2025b", if_missing=True,
                                 default_scope="user", ctf="a.ctf")
    assert (release, path) == ("R2025b", str(stub["user"] / "R2025b"))
    assert stub["calls"] == [("R2025b", None, dict(
        patch=None, products=None, ctf="a.ctf", default_scope="user",
        missing=True,
    ))]


def test_if_missing_latest_installed(stub):
    # Default version with if_missing: any installed release will do
    path = fake_runtime(stub["prefix"] / "R2024b", "R2024b")
    assert impl.install(if_missing=True, python_version="3.9") == (
        "R2024b", path
    )
    assert not stub["calls"]


def test_if_missing_mwpython(stub):
    # Installations without mwpython do not count
    fake_runtime(stub["prefix"] / "R2025b", "R2025b")
    assert impl.install("R2025b", if_missing=True)[1]
    assert not stub["calls"]
    impl.install("R2025b", if_missing="mwpython")
    assert [call[0] for call in stub["calls"]] == ["R2025b"]


def test_if_missing_python_version(stub):
    with pytest.raises(UnsupportedPythonError):
        impl.install("R2025b", if_missing=True, python_version="2.0")
    # False: the python version is not checked
    impl.install("R2025b", if_missing=True, python_version=False)
    assert [call[0] for call in stub["calls"]] == ["R2025b"]


def test_if_missing_invalid(stub):
    with pytest.raises(ValueError):
        impl.install("R2025b", if_missing="yes")


def test_if_missing_several_versions(stub, monkeypatch):
    asked = []
    monkeypatch.setattr(impl, "_default_prefix",
                        lambda *a: asked.append(a) or "/chosen")
    found = fake_runtime(stub["prefix"] / "R2024b", "R2024b")
    # All found: the location is not asked
    assert impl.install(["R2024b"], if_missing=True,
                        python_version=False) == [("R2024b", found)]
    assert not asked and not stub["calls"]
    # One missing: the location is asked once
    out = impl.install(["R2024b", "R2025b"], if_missing=True,
                       python_version=False, default_scope="user")
    assert out == [("R2024b", found),
                   ("R2025b", str(stub["user"] / "R2025b"))]
    assert asked == [("user", False)]
    assert [call[:2] for call in stub["calls"]] == [("R2025b", "/chosen")]


# --- command line -------------------------------------------------------


@pytest.mark.parametrize("argv,value", [
    ([], False),
    (["--if-missing"], True),
    (["--if-missing", "mwpython"], "mwpython"),
])
def test_cli_if_missing(monkeypatch, argv, value):
    calls = []
    monkeypatch.setattr(cli, "install", lambda *a, **k: calls.append(k))
    cli.main(argv)
    assert calls[0]["if_missing"] == value


def test_cli_if_missing_invalid(monkeypatch):
    monkeypatch.setattr(cli, "install", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        cli.main(["--if-missing", "always"])


# --- questions ----------------------------------------------------------


@pytest.fixture
def installer(env, monkeypatch, tmp_path):  # noqa: F811
    """Fake download and installer (Linux)."""
    state = dict(env, downloads=[], runs=[])
    monkeypatch.setattr(impl, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(impl, "guess_release", lambda version, arch: version)
    monkeypatch.setattr(impl, "guess_installer", lambda release, arch: URL)

    def url_download(url, out):
        state["downloads"].append(url)
        out = op.join(out, op.basename(url))
        with zipfile.ZipFile(out, "w") as f:
            f.writestr("install", "#!/bin/sh\n")
        return out
    monkeypatch.setattr(impl, "url_download", url_download)

    class Done:
        returncode = 0
        stdout = stderr = b""

    def run(call, **kwargs):
        state["runs"].append(call)
        if "-inputFile" in call:
            # Product selection: options in an input file
            with open(call[call.index("-inputFile") + 1]) as f:
                options = dict(line.strip().split("=", 1) for line in f
                               if "=" in line)
            prefix = options["destinationFolder"]
            with open(options["outputFile"], "w") as f:
                f.write("End - Successful\n")
        else:
            prefix = call[call.index("-destinationFolder") + 1]
        fake_runtime(op.join(prefix, "R2025b"), "R2025b")
        return Done()
    monkeypatch.setattr(impl.subprocess, "run", run)
    return state


def test_questions(installer, monkeypatch, capsys):
    prompts = answers(monkeypatch, "", "")
    user = str(installer["user"])
    assert impl.install("R2025b", default_scope="user") == (
        "R2025b", op.join(user, "R2025b")
    )
    # Two questions: where, and the license (which starts the install)
    assert prompts == [
        "Location (user/system/custom) [user]: ",
        "Accept the license and install? [Y/n] ",
    ]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Where should the MATLAB Runtime be installed?",
        f"  1. user    {user:<{len(str(installer['prefix']))}}"
        f"  (no admin rights needed)",
        f"  2. system  {installer['prefix']}  (may need admin rights)",
        "  3. custom  another folder",
        "",
        "Ready to install MATLAB Runtime R2025b (Update 7)",
        f"  location:  {op.join(user, 'R2025b')}",
        "  products:  all",
        f"  license:   {LICENSE_URL}",
        "Installing the MATLAB Runtime means that you accept the terms of "
        "its license.",
        "",
        "Downloading MATLAB_Runtime_R2025b_Update_7_glnxa64.zip",
        "Extracting the installer",
        "Installing (this can take several minutes)",
        f"MATLAB Runtime R2025b installed in {op.join(user, 'R2025b')}",
        "License agreement: "
        f"{op.join(user, 'R2025b', 'matlabruntime_license_agreement.pdf')}",
    ]
    assert installer["downloads"] == [URL]


def test_questions_if_missing(installer, monkeypatch, capsys):
    answers(monkeypatch, "", "")
    with pytest.warns(UserWarning, match="product metadata"):
        impl.install("R2025b", if_missing=True, python_version=False,
                     default_scope="user", products="Numerics")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "MATLAB Runtime R2025b is not installed."
    assert "  products:  Numerics" in out


def test_decline_license(installer, monkeypatch):
    answers(monkeypatch, "system", "no")
    with pytest.raises(UserInterruptionError):
        impl.install("R2025b")
    assert not installer["downloads"]
    assert not installer["runs"]


def test_auto_answer_asks_nothing(installer, monkeypatch, capsys):
    answers(monkeypatch)  # input() must not be called
    impl.install("R2025b", prefix=str(installer["tmp"] / "here"),
                 auto_answer=True)
    assert len(installer["runs"]) == 1
    out = capsys.readouterr().out
    assert "Where should" not in out
    assert "installed in" in out


def test_already_installed(installer, monkeypatch, capsys):
    path = fake_runtime(installer["user"] / "R2025b", "R2025b")
    prompts = answers(monkeypatch, "user", "")
    assert impl.install("R2025b") == ("R2025b", path)
    assert prompts[-1] == "Reinstall it? [y/N] "
    assert not installer["downloads"]


def test_installed_elsewhere(installer, monkeypatch):
    path = fake_runtime(installer["prefix"] / "R2025b", "R2025b")
    prompts = answers(monkeypatch, "user", "")
    assert impl.install("R2025b") == ("R2025b", path)
    assert prompts[-1].startswith("Install another copy in ")
    assert not installer["downloads"]


def test_tilde(monkeypatch):
    if os.name == "nt":
        pytest.skip("No ~ on Windows")
    monkeypatch.setenv("HOME", "/home/me")
    assert utils.tilde("/home/me/Applications") == "~/Applications"
    assert utils.tilde("/home/me") == "~"
    assert utils.tilde("/home/meself") == "/home/meself"
    assert utils.tilde("/opt/mcr") == "/opt/mcr"
