"""Termination of the in-process runtime at the end of a pytest session.

A pytest session is run in a subprocess, with a fake MATLAB runtime
whose extension module behaves like the real one where it matters:

* `initializeApplication` installs its own SIGSEGV handler (as the JVM
  of the real runtime does);
* `terminateApplication` "crashes" (exit code 139) if that handler was
  removed in the meantime, as pytest's faulthandler plugin does in
  `pytest_unconfigure` (see `tests/conftest.py`), or if it is called
  twice; packages must be terminated once, before the runtime.
"""
import os
import os.path as op
import subprocess
import sys
import textwrap

import pytest

from matlab_runtime.utils import guess_arch

from .test_runtime_discovery import fake_runtime

ROOT = op.dirname(op.dirname(op.abspath(__file__)))

CPPEXT = '''
"""Fake `matlabruntimeforpython_abi3` (logs calls)."""
import ctypes
import os
import signal

_libc = ctypes.CDLL(None)
_libc.signal.restype = ctypes.c_void_p
_libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
_SIG_IGN = 1        # stand-in for the runtime's own handler
STATE = {"app": None}


def log(msg):
    with open(os.environ["FAKE_RUNTIME_LOG"], "a") as f:
        f.write(msg + "\\n")


def crash(msg):
    log("CRASH " + msg)
    os._exit(139)


def _handler():
    old = _libc.signal(signal.SIGSEGV, _SIG_IGN)
    _libc.signal(signal.SIGSEGV, old)
    return old


def initializeApplication(options):
    if STATE["app"] is not None:
        crash("initializeApplication: " + STATE["app"])
    _libc.signal(signal.SIGSEGV, _SIG_IGN)
    STATE["app"] = "running"
    log("initializeApplication")


def terminateApplication():
    if STATE["app"] != "running":
        crash("terminateApplication: " + str(STATE["app"]))
    if _handler() != _SIG_IGN:
        crash("terminateApplication: SIGSEGV handler was removed")
    _libc.signal(signal.SIGSEGV, None)      # SIG_DFL
    STATE["app"] = "terminated"
    log("terminateApplication")


def check(what):
    if STATE["app"] != "running":
        crash(what + ": runtime " + str(STATE["app"]))
'''

SDK = '''
import matlabruntimeforpython_abi3 as _cpp


class DeployablePackage:
    def __init__(self, path_initializer, name, file):
        self.name, self.state = name, None

    def initialize(self):
        _cpp.check("initialize")
        self.state = "initialized"
        _cpp.log("initialize " + self.name)

    def terminate(self):
        _cpp.check("terminate " + self.name)
        if self.state != "initialized":
            _cpp.crash("terminate %s: %s" % (self.name, self.state))
        self.state = "terminated"
        _cpp.log("terminate " + self.name)

    def test_runtime(self):
        _cpp.check("call")
        return "success"
'''

MATLAB = '''
import os


def get_arch_filename():
    return os.path.abspath(__file__)
'''

INNER_TEST = '''
import os
import sys
import types

from matlab_runtime import impl, utils

KEEP = []


def test_inprocess():
    python = "{}.{}".format(*sys.version_info[:2])
    utils.SUPPORTED_PYTHON_VERSIONS["R2024b"] = [python]
    impl.init("R2024b", prefix=os.environ["FAKE_RUNTIME_PREFIX"],
              mode="inprocess")
    module = types.ModuleType("fakepkg")
    module.__file__ = os.path.join(os.getcwd(), "fakepkg", "__init__.py")
    pkg = impl.import_deployed(module)
    assert pkg.test_runtime() == "success"
    # Two handles: one still referenced at exit, one dropped
    KEEP.append(pkg)
    module = types.ModuleType("otherpkg")
    module.__file__ = os.path.join(os.getcwd(), "otherpkg", "__init__.py")
    impl.import_deployed(module)
'''


def make_runtime(path):
    arch = guess_arch()
    fake_runtime(path, "R2024b")
    sdk = op.join(path, "toolbox", "compiler_sdk", "pysdk_py")
    files = {
        op.join(path, "extern", "bin", arch,
                "matlabruntimeforpython_abi3.py"): CPPEXT,
        op.join(sdk, "matlab_pysdk", "__init__.py"): "",
        op.join(sdk, "matlab_pysdk", "runtime", "__init__.py"): SDK,
        op.join(sdk, "matlab_mod_dist", "matlab", "__init__.py"): MATLAB,
    }
    os.makedirs(op.join(path, "bin", arch), exist_ok=True)
    for name, text in files.items():
        os.makedirs(op.dirname(name), exist_ok=True)
        with open(name, "w") as f:
            f.write(text)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("hook", [True, False])
def test_runtime_terminated_before_unconfigure(tmp_path, hook):
    prefix = tmp_path / "prefix"
    make_runtime(str(prefix / "R2024b"))
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "test_inner.py").write_text(textwrap.dedent(INNER_TEST))
    log = tmp_path / "runtime.log"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["FAKE_RUNTIME_LOG"] = str(log)
    env["FAKE_RUNTIME_PREFIX"] = str(prefix)
    env.pop("MATLAB_RUNTIME_MODE", None)
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--rootdir", str(inner), "test_inner.py"]
    if hook:
        argv[3:3] = ["-p", "tests.conftest"]
    proc = subprocess.run(
        argv, cwd=str(inner), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=120,
    )
    out = proc.stdout.decode(errors="replace")
    calls = log.read_text().splitlines()
    assert "1 passed" in out, out
    if not hook:
        # What happened on CI: the runtime is terminated by `atexit`,
        # after pytest has removed its signal handlers.
        assert proc.returncode == 139, out
        assert calls[-1] == (
            "CRASH terminateApplication: SIGSEGV handler was removed"
        )
        return
    assert proc.returncode == 0, (out, calls)
    # Exactly once, in order: packages, then runtime (not again at exit)
    assert calls == [
        "initializeApplication",
        "initialize fakepkg",
        "initialize otherpkg",
        "terminate fakepkg",
        "terminateApplication",
    ], out
