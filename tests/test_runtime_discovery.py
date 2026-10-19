"""Runtime discovery and selection, with fake installation trees."""
import os
import os.path as op
import sys
import zipfile

import pytest

from matlab_runtime import utils
from matlab_runtime.utils import (
    RuntimeNotFoundError,
    UnsupportedPythonError,
    ctf_matlab_release,
    deployed_ctf,
    find_runtime,
    find_runtime_in_prefix,
    guess_prefix as real_guess_prefix,
    guess_release,
    iter_existing_installations,
    iter_installations,
    matlab_release,
    release_key,
    resolve_runtime,
)

TEST_CTF = op.join(op.dirname(__file__), "_test_runtime", "_test_runtime.ctf")

VERSION_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<MathWorks_version_info>
  <version>{version}</version>
  <release>{release}</release>
  <description/>
  <date>Sep 18 2019</date>
  <checksum>0</checksum>
</MathWorks_version_info>
"""

VERSIONS = {
    "R2019b": "9.7.0.1190202",
    "R2022b": "9.13.0.2049777",
    "R2023b": "23.2.0.2365128",
    "R2024b": "24.2.0.2712019",
    "R2025a": "25.1.0.2943329",
    "R2026b": "26.2.0.1",
    "R2099a": "99.1.0.1",
}

SUPPORTED = {
    "R2026b": ["3.13"],
    "R2025a": ["3.11", "3.12"],
    "R2024b": ["3.9", "3.12"],
    "R2023b": ["3.9"],
    "R2022b": ["3.9"],
    "R2019b": ["3.7"],
}


def fake_runtime(path, release, mwpython=False, license=True):
    """Create a fake installation tree; return its path as str."""
    os.makedirs(str(path), exist_ok=True)
    with open(op.join(str(path), "VersionInfo.xml"), "w") as f:
        f.write(VERSION_INFO.format(version=VERSIONS[release],
                                    release=release))
    if license:
        pdf = op.join(str(path), "matlabruntime_license_agreement.pdf")
        open(pdf, "w").close()
    if mwpython:
        os.makedirs(op.join(str(path), "bin"), exist_ok=True)
        open(op.join(str(path), "bin", "mwpython"), "w").close()
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated discovery: no real installation can be found."""
    default_prefix = tmp_path / "default"
    standard = tmp_path / "standard"
    monkeypatch.setattr(utils, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(utils, "guess_prefix", lambda: str(default_prefix))
    monkeypatch.setattr(utils, "CANDIDATE_LOCATIONS_BY_OS", {"gln": [
        op.join(str(standard), "MATLAB_Runtime", "{release}"),
        op.join(str(standard), "MATLAB_{release}.app"),
    ]})
    monkeypatch.setattr(utils.shutil, "which", lambda name: None)
    monkeypatch.setattr(utils, "SUPPORTED_PYTHON_VERSIONS", dict(SUPPORTED))
    monkeypatch.delenv("MATLAB_RUNTIME_PATH", raising=False)
    monkeypatch.delenv("MATLAB_PATH", raising=False)
    return dict(tmp=tmp_path, prefix=default_prefix, standard=standard)


# --- releases -----------------------------------------------------------


def test_matlab_release_from_version():
    assert matlab_release("24.2") == "R2024b"
    assert matlab_release("24.1") == "R2024a"
    assert matlab_release((24, 2)) == "R2024b"
    assert matlab_release("9.7") == "R2019b"
    assert matlab_release("R2022a") == "R2022a"


def test_release_key_ordering():
    releases = ["R2019b", "R2024b", "R2015aSP1", "R2024a", "R2015a", "9.13"]
    assert sorted(releases, key=release_key) == [
        "R2015a", "R2015aSP1", "R2019b", "9.13", "R2024a", "R2024b"
    ]
    assert release_key("not-a-release") == (0, 0, "")


# --- latest installed ---------------------------------------------------


def test_latest_installed_orders_by_release_not_folder_name(env):
    # "v97" > "R2024b" lexicographically, but R2019b < R2024b
    fake_runtime(env["prefix"] / "v97", "R2019b")
    path = fake_runtime(env["prefix"] / "R2024b", "R2024b")
    assert resolve_runtime() == ("R2024b", path)
    assert guess_release("latest_installed") == "R2024b"
    assert [r for _, r in iter_existing_installations()] \
        == ["R2024b", "R2019b"]


def test_latest_installed_nothing_installed_raises(env, monkeypatch):
    # Must not silently fall back to querying the network
    def no_network(*a, **k):
        raise AssertionError("network queried")
    monkeypatch.setattr(utils, "guess_installer", no_network)
    with pytest.raises(RuntimeNotFoundError, match="MATLAB_RUNTIME_PATH"):
        guess_release("latest_installed")
    with pytest.raises(FileNotFoundError):  # backward compatible
        resolve_runtime()
    assert find_runtime("R2024b") is None


def test_not_found_lists_installations(env):
    fake_runtime(env["prefix"] / "R2023b", "R2023b")
    with pytest.raises(RuntimeNotFoundError) as e:
        resolve_runtime("R2024b")
    assert "R2023b" in str(e.value)
    assert "Python 3.9" in str(e.value)


def test_missing_ok(env, monkeypatch):
    assert resolve_runtime("24.2", missing_ok=True) == ("R2024b", None)

    # latest_installed + missing_ok: newest release that supports python
    # and has an installer
    def fake_installer(release, arch=None):
        if release == "R2025a":
            raise utils.VersionNotFoundError(release)
        return "https://example.com/" + release
    monkeypatch.setattr(utils, "guess_installer", fake_installer)
    fake_runtime(env["prefix"] / "R2026b", "R2026b")
    assert resolve_runtime(python_version="3.12", missing_ok=True) \
        == ("R2024b", None)


def test_full_matlab_and_which(env, monkeypatch):
    fake_runtime(env["prefix"] / "R2023b", "R2023b")
    app = fake_runtime(env["standard"] / "MATLAB_R2024b.app", "R2024b",
                       license=False)
    assert resolve_runtime() == ("R2024b", app)

    which = fake_runtime(env["tmp"] / "opt" / "matlab", "R2025a",
                         license=False)
    exe = op.join(which, "bin", "matlab")
    os.makedirs(op.dirname(exe))
    open(exe, "w").close()
    monkeypatch.setattr(utils.shutil, "which", lambda name: exe)
    installs = list(iter_installations())
    assert [(x.release, x.source) for x in installs] == [
        ("R2023b", "prefix"), ("R2024b", "standard"), ("R2025a", "which")
    ]
    assert resolve_runtime() == ("R2025a", op.realpath(which))
    assert find_runtime("R2024b") == app


def test_matlab_path(env, monkeypatch):
    path = fake_runtime(env["tmp"] / "matlab_path", "R2022b")
    monkeypatch.setenv("MATLAB_PATH", path + os.sep)
    assert find_runtime("R2022b") == path
    assert list(iter_installations())[0].source == "MATLAB_PATH"


def test_prefix_wins_ties(env):
    fake_runtime(env["standard"] / "MATLAB_Runtime" / "R2024b", "R2024b")
    custom = fake_runtime(env["tmp"] / "custom" / "R2024b", "R2024b")
    release, path = resolve_runtime(prefix=str(env["tmp"] / "custom"))
    assert (release, path) == ("R2024b", custom)


def test_duplicates_are_listed_once(env, monkeypatch):
    fake_runtime(env["standard"] / "MATLAB_Runtime" / "R2024b", "R2024b")
    monkeypatch.setattr(utils, "guess_prefix",
                        lambda: str(env["standard"] / "MATLAB_Runtime"))
    installs = list(iter_installations())
    assert [(x.release, x.source) for x in installs] == [("R2024b", "prefix")]


# --- MATLAB_RUNTIME_PATH ------------------------------------------------


def test_runtime_path_as_prefix(env, monkeypatch):
    custom = env["tmp"] / "custom"
    fake_runtime(custom / "v97", "R2019b")
    path = fake_runtime(custom / "R2023b", "R2023b")
    monkeypatch.setenv("MATLAB_RUNTIME_PATH", str(custom))
    assert resolve_runtime() == ("R2023b", path)
    assert find_runtime("R2019b") == str(custom / "v97")
    assert real_guess_prefix() == str(custom)


def test_runtime_path_as_release_folder(env, monkeypatch):
    path = fake_runtime(env["tmp"] / "custom" / "R2024b", "R2024b")
    monkeypatch.setenv("MATLAB_RUNTIME_PATH", path)
    assert resolve_runtime() == ("R2024b", path)
    assert find_runtime("R2024b") == path
    # The real release is yielded, never the variant string
    assert list(iter_existing_installations()) == [(path, "R2024b")]
    assert list(iter_existing_installations("R2024b")) == [(path, "R2024b")]
    # Installation prefix: the parent folder
    assert real_guess_prefix() == str(env["tmp"] / "custom")


def test_find_runtime_in_prefix_release_folder(tmp_path):
    path = fake_runtime(tmp_path / "R2024b", "R2024b")
    assert find_runtime_in_prefix("R2024b", path) == path
    assert find_runtime_in_prefix("R2023b", path) is None


# --- python version -----------------------------------------------------


def test_python_version_filter(env):
    new = fake_runtime(env["prefix"] / "R2026b", "R2026b")
    old = fake_runtime(env["prefix"] / "R2024b", "R2024b")
    assert resolve_runtime(python_version="3.13") == ("R2026b", new)
    assert resolve_runtime(python_version="3.12") == ("R2024b", old)
    assert resolve_runtime(python_version=(3, 9, 1)) == ("R2024b", old)
    assert resolve_runtime() == ("R2026b", new)


def test_python_version_filter_removes_everything(env):
    fake_runtime(env["prefix"] / "R2026b", "R2026b")
    fake_runtime(env["prefix"] / "R2024b", "R2024b")
    with pytest.raises(UnsupportedPythonError) as e:
        resolve_runtime(python_version="3.8")
    msg = str(e.value)
    assert "Python 3.8" in msg
    assert "R2026b" in msg and "Python 3.13" in msg
    assert "R2024b" in msg and "Python 3.9, 3.12" in msg


def test_python_version_explicit_release(env):
    fake_runtime(env["prefix"] / "R2026b", "R2026b")
    with pytest.raises(UnsupportedPythonError, match="3.13"):
        resolve_runtime("R2026b", python_version="3.9")
    with pytest.raises(UnsupportedPythonError):
        resolve_runtime("R2026b", python_version="3.9", missing_ok=True)


def test_python_version_unknown_release_is_kept(env):
    path = fake_runtime(env["prefix"] / "R2099a", "R2099a")
    fake_runtime(env["prefix"] / "R2024b", "R2024b")
    assert resolve_runtime(python_version="3.9") == ("R2099a", path)


def test_accept(env):
    fake_runtime(env["prefix"] / "R2026b", "R2026b")
    path = fake_runtime(env["prefix"] / "R2024b", "R2024b", mwpython=True)
    def has_mwpython(path):
        return op.exists(op.join(path, "bin", "mwpython"))
    assert resolve_runtime(accept=has_mwpython) == ("R2024b", path)
    with pytest.raises(RuntimeNotFoundError, match="rejected"):
        resolve_runtime("R2026b", accept=has_mwpython)


# --- CTF ----------------------------------------------------------------


def test_ctf_release_from_test_runtime():
    assert ctf_matlab_release(TEST_CTF) == "R2024b"


def test_ctf_release_fallback_and_missing(tmp_path):
    ctf = tmp_path / "pkg.ctf"
    with zipfile.ZipFile(ctf, "w") as f:
        f.writestr("metadata/mwcorePropertiesReleaseInfo.xml",
                   VERSION_INFO.format(version="9.7.0", release="R2019b"))
    assert ctf_matlab_release(str(ctf)) == "R2019b"

    ctf = tmp_path / "old.ctf"
    with zipfile.ZipFile(ctf, "w") as f:
        f.writestr(".META/manifest.xml",
                   '<manifest mcr-major-version="9" mcr-minor-version="7"/>')
    assert ctf_matlab_release(str(ctf)) == "R2019b"

    ctf = tmp_path / "empty.ctf"
    with zipfile.ZipFile(ctf, "w") as f:
        f.writestr("foo.txt", "")
    with pytest.raises(ValueError):
        ctf_matlab_release(str(ctf))


def test_deployed_ctf(tmp_path, monkeypatch):
    from . import _test_runtime
    assert op.samefile(deployed_ctf(_test_runtime), TEST_CTF)
    assert op.samefile(deployed_ctf("tests._test_runtime"), TEST_CTF)
    assert deployed_ctf("not_a_package_xyz") is None
    assert deployed_ctf("not_a_package_xyz.sub") is None

    # Not imported yet: located without importing it
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise ImportError('not importable')")
    (pkg / "compiled.ctf").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert op.samefile(deployed_ctf("mypkg"), str(pkg / "compiled.ctf"))
    assert "mypkg" not in sys.modules
