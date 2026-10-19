"""Runtime mode of `init` / `import_deployed` ("inprocess", "remote", "auto").

The remote tests use the fake MATLAB SDK of test_remote.py.
"""
import sys
import types

import pytest

from matlab_runtime import impl, utils
from matlab_runtime.utils import MODE_ENV

np = pytest.importorskip("numpy")

from matlab_runtime import remote  # noqa: E402
from matlab_runtime.remote import worker  # noqa: E402

from . import test_remote  # noqa: E402
from .test_runtime_discovery import fake_runtime  # noqa: E402


# Fixtures shared with test_remote.py
fake_env = test_remote.fake_env
isolated_runtimes = test_remote.isolated_runtimes


class _FakeSys(types.ModuleType):
    """`sys`, as seen by `impl`, with another `platform`."""

    def __init__(self, platform):
        super().__init__("sys")
        self.platform = platform

    def __getattr__(self, name):
        return getattr(sys, name)


@pytest.fixture
def mode_state(monkeypatch):
    """Fresh mode and SDK state; no MATLAB_RUNTIME_MODE."""
    monkeypatch.setattr(impl, "_MODE", {"mode": None})
    monkeypatch.setattr(impl, "_INITIALIZED", {"SDK": False, "RUNTIME": False})
    monkeypatch.delenv(MODE_ENV, raising=False)


def platform(monkeypatch, name, mwpython=False):
    # Only `impl` sees the fake platform (the client code that starts
    # and stops the worker must see the real one, e.g. on Windows).
    monkeypatch.setattr(impl, "sys", _FakeSys(name))
    monkeypatch.setattr(impl, "running_under_mwpython", lambda: mwpython)


@pytest.mark.parametrize("name,mwpython,expected", [
    ("darwin", False, "remote"),
    ("darwin", True, "inprocess"),
    ("linux", False, "inprocess"),
    ("win32", False, "inprocess"),
])
def test_auto(mode_state, monkeypatch, name, mwpython, expected):
    platform(monkeypatch, name, mwpython)
    assert impl._resolve_mode() == expected
    assert impl._resolve_mode("auto") == expected
    assert impl._resolve_mode("AUTO") == expected
    assert impl._resolve_mode("inprocess") == "inprocess"
    assert impl._resolve_mode("remote") == "remote"


def test_precedence(mode_state, monkeypatch):
    platform(monkeypatch, "linux")
    monkeypatch.setenv(MODE_ENV, "remote")
    assert impl._resolve_mode() == "remote"
    assert impl._resolve_mode("inprocess") == "inprocess"
    monkeypatch.setenv(MODE_ENV, "auto")
    assert impl._resolve_mode() == "inprocess"
    monkeypatch.setenv(MODE_ENV, "")
    assert impl._resolve_mode() == "inprocess"
    monkeypatch.setenv(MODE_ENV, "elsewhere")
    with pytest.raises(ValueError, match=MODE_ENV):
        impl._resolve_mode()
    with pytest.raises(ValueError, match="elsewhere"):
        impl._resolve_mode("elsewhere")


def test_mode_is_fixed(mode_state, monkeypatch):
    platform(monkeypatch, "darwin")
    assert impl._use_mode("inprocess") == "inprocess"
    # Later calls follow (also "auto", and the environment is ignored)
    monkeypatch.setenv(MODE_ENV, "remote")
    assert impl._use_mode() == "inprocess"
    assert impl._use_mode("auto") == "inprocess"
    with pytest.raises(RuntimeError, match="'inprocess' mode"):
        impl._use_mode("remote")
    with pytest.raises(RuntimeError, match="'inprocess' mode"):
        impl.init(mode="remote")
    with pytest.raises(RuntimeError, match="'inprocess' mode"):
        impl.import_deployed("fake_package", mode="remote")
    with pytest.raises(RuntimeError, match="'inprocess' mode"):
        impl.init_runtime(mode="remote")


def test_remote_after_inprocess_sdk(mode_state):
    # The in-process SDK was initialized (by older code paths)
    impl._INITIALIZED["SDK"] = "R2024b"
    with pytest.raises(RuntimeError, match="already been initialized"):
        impl.init(mode="remote")
    assert impl._MODE["mode"] is None


def test_worker_never_recurses(mode_state, monkeypatch):
    # The worker calls impl in-process, even if the client asked for
    # remote mode through the environment, on macOS without mwpython.
    platform(monkeypatch, "darwin")
    monkeypatch.setenv(MODE_ENV, "remote")
    calls = []
    monkeypatch.setattr(impl, "_INITIALIZED", {"SDK": True, "RUNTIME": True})

    class Handle:
        def __init__(self, *args):
            pass

        def initialize(self):
            pass

    def import_module(name):
        calls.append(name)
        return types.SimpleNamespace(DeployablePackage=Handle)
    monkeypatch.setattr(impl, "importlib",
                        types.SimpleNamespace(import_module=import_module))
    monkeypatch.setattr(impl, "_DEPLOYED_MODULES", {})
    monkeypatch.setattr(impl, "_deployed_release", lambda packages: None)

    def no_remote(*args, **kwargs):
        raise AssertionError("the worker must not start a worker")
    monkeypatch.setattr(remote, "init", no_remote)
    monkeypatch.setattr(remote, "import_deployed", no_remote)
    monkeypatch.setattr(remote, "_init_runtime", no_remote)

    backend = worker.SdkBackend()
    backend.init(version="latest_installed")  # already initialized
    backend.init_runtime(["-nojvm"])
    module = types.ModuleType("pkg")
    module.__file__ = "pkg/__init__.py"
    monkeypatch.setitem(sys.modules, "pkg", module)
    backend.import_package("pkg", None)
    assert impl._MODE["mode"] == "inprocess"
    assert "matlab_pysdk.runtime" in calls


def test_worker_environment(fake_env, mode_state, monkeypatch):
    # The worker is started with MATLAB_RUNTIME_MODE=inprocess
    monkeypatch.setenv(MODE_ENV, "remote")
    remote.init(use_mpython=False)
    pkg = remote.import_deployed("fake_package")
    assert pkg.getenv(MODE_ENV) == "inprocess"


@pytest.fixture
def macos_client(fake_env, isolated_runtimes, mode_state, monkeypatch):
    """A plain interpreter on macOS (no mwpython).

    The worker itself is started without mwpython (no runtime here):
    `guess_arch` is left to a non-macOS value.
    """
    monkeypatch.setattr(utils, "guess_arch", lambda: "glnxa64")
    platform(monkeypatch, "darwin")
    return isolated_runtimes


@pytest.mark.parametrize("installed", [True, False])
def test_spm_runtime_pattern(macos_client, monkeypatch, installed):
    # As spm-runtime does, unchanged:
    #   matlab_runtime.init(__matlab_release__, install_if_missing=True)
    #   pkg = matlab_runtime.import_deployed(_spm)
    #   pkg.mpython_endpoint(fn, *args, nargout=...)
    import matlab_runtime
    prefix = macos_client["prefix"]
    installs = []
    if installed:
        fake_runtime(prefix / "R2024b", "R2024b")

    def install(release, prefix_, auto_answer):
        installs.append(release)
        fake_runtime(prefix / release, release)
    monkeypatch.setattr(impl, "install", install)

    matlab_runtime.init("R2024b", install_if_missing=True)
    assert installs == ([] if installed else ["R2024b"])
    assert impl._MODE["mode"] == "remote"
    assert not impl._INITIALIZED["SDK"]  # nothing loaded here
    assert remote.is_alive()

    pkg = matlab_runtime.import_deployed("fake_package")
    assert isinstance(pkg, remote.RemoteHandle)
    assert pkg.init_kwargs() == {
        "version": "R2024b", "prefix": str(prefix / "R2024b")
    }
    out = pkg.mpython_endpoint("spm", np.ones([2, 3]), "x", nargout=1)
    assert out["f"] == "spm" and out["nargout"] == 1
    # numpy arrays reached the worker as matlab arrays
    assert out["types"] == ["matlab.double", "builtins.str"]
    assert pkg.getenv(MODE_ENV) == "inprocess"


def test_remote_runtime_functions(macos_client):
    import matlab_runtime
    matlab_runtime.init_runtime(["-nojvm"])
    assert impl._MODE["mode"] == "remote"
    pkg = matlab_runtime.import_deployed("fake_package")
    assert pkg.runtime_options() == ["-nojvm"]
    with pytest.raises(ValueError, match="already initialized"):
        matlab_runtime.init_runtime(error_if_already_init=True)
    # terminate_runtime stops the worker; the next import restarts one
    matlab_runtime.terminate_runtime()
    assert not remote.is_alive()
    pkg = matlab_runtime.import_deployed("fake_package")
    assert pkg.runtime_options() == []
    assert impl._MODE["mode"] == "remote"
