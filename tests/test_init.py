"""`init` / `import_deployed` with a mocked MATLAB SDK."""
import os
import sys
import types
import warnings

import pytest

from matlab_runtime import impl, utils
from matlab_runtime.utils import UnsupportedPythonError

from .test_runtime_discovery import fake_runtime


def current_python():
    return "{}.{}".format(*sys.version_info[:2])


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """Fake installations + fake SDK modules; restore global state."""
    prefix = tmp_path / "prefix"
    fake_runtime(prefix / "R2024b", "R2024b")
    fake_runtime(prefix / "R2023b", "R2023b")

    monkeypatch.setattr(utils, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(impl, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(utils, "guess_prefix", lambda: str(prefix))
    monkeypatch.setattr(utils, "CANDIDATE_LOCATIONS_BY_OS", {"gln": []})
    monkeypatch.setattr(utils.shutil, "which", lambda name: None)
    monkeypatch.delenv("MATLAB_RUNTIME_PATH", raising=False)
    monkeypatch.delenv("MATLAB_PATH", raising=False)
    monkeypatch.setattr(utils, "SUPPORTED_PYTHON_VERSIONS", {
        "R2024b": [current_python()],
        "R2023b": [current_python()],
    })

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(impl, "_INITIALIZED", {"SDK": False, "RUNTIME": False})
    # In-process, also on macOS outside mwpython (see test_mode.py)
    monkeypatch.setattr(impl, "_MODE", {"mode": None})
    monkeypatch.setenv(utils.MODE_ENV, "inprocess")
    monkeypatch.setattr(impl, "_DEPLOYED_MODULES", {})

    state = dict(imports=[], prefix=prefix, version="24.2.0")

    class Handle:
        def __init__(self, init, name, file):
            self.name = name

        def initialize(self):
            pass

        def terminate(self):
            pass

    def import_module(name):
        state["imports"].append(name)
        return types.SimpleNamespace(DeployablePackage=Handle)

    monkeypatch.setattr(impl, "importlib",
                        types.SimpleNamespace(import_module=import_module))
    monkeypatch.setattr(impl, "guess_pymatlab_version",
                        lambda matlab: state["version"])
    return state


def test_init_same_release_returns(sdk):
    impl.init("R2024b")
    assert impl._INITIALIZED["SDK"] == "R2024b"
    nb_imports = len(sdk["imports"])
    assert nb_imports
    impl.init("R2024b")
    impl.init("24.2")
    impl.init()  # latest_installed: whatever is already initialized
    assert len(sdk["imports"]) == nb_imports


def test_init_different_release_raises(sdk):
    impl.init()
    assert impl._INITIALIZED["SDK"] == "R2024b"
    with pytest.raises(ValueError, match="R2023b"):
        impl.init("R2023b")


def test_init_python_unsupported(sdk, monkeypatch):
    monkeypatch.setattr(utils, "SUPPORTED_PYTHON_VERSIONS", {
        "R2024b": ["2.0"], "R2023b": [current_python()],
    })
    with pytest.raises(UnsupportedPythonError):
        impl.init("R2024b")
    assert not sdk["imports"]
    # latest_installed skips releases that do not support this python
    sdk["version"] = "23.2.0"
    impl.init()
    assert impl._INITIALIZED["SDK"] == "R2023b"


def test_init_python_unknown_warns(sdk, monkeypatch):
    monkeypatch.setattr(utils, "SUPPORTED_PYTHON_VERSIONS", {})
    with pytest.warns(UserWarning, match="unknown"):
        impl.init("R2024b")


def test_init_install_if_missing(sdk, monkeypatch):
    installed = []

    def install(release, prefix, auto_answer):
        installed.append(release)
        fake_runtime(sdk["prefix"] / release, release)
    monkeypatch.setattr(impl, "install", install)
    monkeypatch.setitem(utils.SUPPORTED_PYTHON_VERSIONS, "R2022b",
                        [current_python()])
    sdk["version"] = "9.13.0"
    with pytest.raises(FileNotFoundError):
        impl.init("R2022b")
    impl.init("R2022b", install_if_missing=True)
    assert installed == ["R2022b"]
    assert impl._INITIALIZED["SDK"] == "R2022b"


def test_import_deployed_infers_release_from_ctf(sdk, monkeypatch):
    calls = []
    real_init = impl.init

    def init(version="latest_installed", *a, **k):
        calls.append(version)
        return real_init(version, *a, **k)
    monkeypatch.setattr(impl, "init", init)
    monkeypatch.setattr(impl, "init_runtime", lambda options=(): None)

    # R2024b is not the most recent installed release
    fake_runtime(sdk["prefix"] / "R2025a", "R2025a")
    from . import _test_runtime
    handle = impl.import_deployed(_test_runtime)
    assert calls == ["R2024b"]
    assert impl._INITIALIZED["SDK"] == "R2024b"
    assert handle.name == _test_runtime.__name__


def test_import_deployed_release_mismatch(sdk, monkeypatch):
    monkeypatch.setattr(impl, "init_runtime", lambda options=(): None)
    sdk["version"] = "23.2.0"
    impl.init("R2023b")
    from . import _test_runtime
    with pytest.raises(ValueError) as e:
        impl.import_deployed(_test_runtime)
    assert "R2023b" in str(e.value) and "R2024b" in str(e.value)


def test_import_deployed_without_ctf(sdk, monkeypatch, tmp_path):
    monkeypatch.setattr(impl, "init_runtime", lambda options=(): None)
    module = types.ModuleType("nopkg")
    module.__file__ = str(tmp_path / "nopkg" / "__init__.py")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        impl.import_deployed(module)
    assert impl._INITIALIZED["SDK"] == "R2024b"
