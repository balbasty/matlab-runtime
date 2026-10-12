"""Tests of matlab_runtime.remote, using a fake MATLAB SDK (no MATLAB)."""
import _thread
import gc
import glob
import io
import os
import os.path as op
import subprocess
import sys
import textwrap
import threading
import time
from unittest import mock

import pytest

np = pytest.importorskip("numpy")

from matlab_runtime import remote  # noqa: E402
from matlab_runtime.remote import client, protocol  # noqa: E402

BACKEND = "tests._fake_sdk.backend:FakeBackend"
ROOT = op.dirname(op.dirname(op.abspath(__file__)))


def shm_segments():
    return glob.glob("/dev/shm/mlrt*")


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv(protocol.ENV_BACKEND, BACKEND)
    monkeypatch.setenv(protocol.ENV_SHM_THRESHOLD, "4096")
    yield
    remote.shutdown()


@pytest.fixture
def pkg(fake_env):
    remote.init()
    return remote.import_deployed("fake_package")


def session():
    return remote._SESSION


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def fake_python(tmp_path, code):
    """An 'interpreter' that runs `code` whatever its arguments."""
    path = tmp_path / "fake_python"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(code))
    path.chmod(0o755)
    return str(path)


# ----------------------------------------------------------------------
#   CALLS AND VALUES
# ----------------------------------------------------------------------


def test_round_trip(pkg):
    assert pkg.test_runtime() == "success"
    assert remote.is_alive()


def test_plain_values(pkg):
    value = {"a": [1, 2.5, "x", None, (True, b"b", 1j)], "b": {"c": "d"}}
    assert pkg.ident(value) == value
    assert pkg.add(1, 2) == 3
    assert pkg.add("a", "b") == "ab"
    assert pkg.ident(1, "a") == (1, "a")


@pytest.mark.parametrize("dtype", [
    "float64", "float32", "bool", "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64", "complex128", "complex64",
])
def test_small_arrays(pkg, dtype):
    x = np.arange(6).reshape(2, 3) % 2
    if dtype.startswith("complex"):
        x = x + 1j
    x = x.astype(dtype)
    y = pkg.ident(x)
    assert isinstance(y, np.ndarray)
    assert y.dtype == x.dtype
    assert y.shape == (2, 3)
    assert (y == x).all()


def test_array_layout(pkg):
    x = np.asfortranarray(np.arange(6.0).reshape(2, 3))
    assert (pkg.ident(x) == x).all()
    # MATLAB arrays have at least two dimensions
    assert pkg.ident(np.arange(3.0)).shape == (1, 3)
    # Arrays are MATLAB arrays in the worker; 0-d arrays are scalars
    assert pkg.types(np.ones(2)) == "matlab.double"
    assert pkg.types(np.ones(2, dtype=bool)) == "matlab.logical"
    assert pkg.types(np.float64(1.0)) == "numpy.float64"
    assert pkg.types(np.array(1.0)) == "builtins.float"
    # Arrays nested in containers are converted too
    out = pkg.ident({"a": [np.eye(2)]})
    assert (out["a"][0] == np.eye(2)).all()


def test_shared_memory(pkg):
    x = np.random.rand(100, 50)
    assert x.nbytes >= 4096
    y = pkg.ident(x)
    assert (y == x).all()
    y = pkg.big(10000)
    assert y.shape == (1, 10000)
    assert (y[0] == np.arange(10000)).all()
    # The worker unlinks its segments when it gets the acknowledgment
    session().request("ping")
    assert shm_segments() == []


def test_shared_memory_no_leak_warnings(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(textwrap.dedent("""
        import numpy as np
        from matlab_runtime import remote
        remote.init()
        pkg = remote.import_deployed("fake_package")
        x = np.random.rand(1000)
        assert (pkg.ident(x) == x).all()
        assert pkg.big(1000).shape == (1, 1000)
        print("done")
    """))
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONWARNINGS="always")
    env[protocol.ENV_BACKEND] = BACKEND
    env[protocol.ENV_SHM_THRESHOLD] = "100"
    p = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True,
        text=True, timeout=60,
    )
    assert p.returncode == 0, p.stderr
    assert "done" in p.stdout
    assert "Warning" not in p.stderr, p.stderr
    assert "leaked" not in p.stderr, p.stderr
    assert shm_segments() == []


def test_shared_memory_disabled(fake_env, monkeypatch):
    monkeypatch.setenv(protocol.ENV_SHM_THRESHOLD, "0")
    remote.init()
    pkg = remote.import_deployed("fake_package")
    x = np.random.rand(10000)
    assert (pkg.ident(x) == x).all()
    assert pkg.big(10000).shape == (1, 10000)


def test_nargout(pkg):
    assert pkg.twoout(3.0) == 3.0
    assert pkg.twoout(3.0, nargout=2) == (3.0, 6.0)


def test_exceptions(pkg):
    with pytest.raises(ValueError, match="boom") as info:
        pkg.fail("boom")
    assert isinstance(info.value.__cause__, remote.RemoteTraceback)
    assert "fail" in str(info.value.__cause__)
    with pytest.raises(remote.RemoteError, match="MatlabError") as info:
        pkg.fail_matlab()
    assert info.value.type == "tests._fake_sdk.backend.MatlabError"
    with pytest.raises(AttributeError):
        pkg.does_not_exist
    with pytest.raises(AttributeError):
        pkg._private
    # The session is still usable
    assert pkg.add(1, 2) == 3


def test_unpicklable_argument(pkg):
    with pytest.raises(Exception):
        pkg.ident(threading.Lock())
    assert pkg.add(1, 2) == 3


def test_matlab_to_numpy_fallback():
    from matlab_runtime.remote.convert import matlab_to_numpy

    class double:  # no buffer protocol, like older releases
        __module__ = "matlab"
        size = (2, 3)
        _data = [0.0, 3.0, 1.0, 4.0, 2.0, 5.0]
        __iter__ = None

    y = matlab_to_numpy(double())
    assert y.dtype == np.float64
    assert (y == np.arange(6.0).reshape(2, 3)).all()

    class single(double):
        _is_complex = True
        _real = [0.0, 3.0, 1.0, 4.0, 2.0, 5.0]
        _imag = [1.0] * 6

    y = matlab_to_numpy(single())
    assert y.dtype == np.complex64
    assert (y == np.arange(6.0).reshape(2, 3) + 1j).all()


# ----------------------------------------------------------------------
#   HANDLES
# ----------------------------------------------------------------------


def test_handles(pkg):
    obj = pkg.make_obj()
    assert isinstance(obj, remote.RemoteHandle)
    assert obj.value() == 42
    assert obj.size == 7
    assert obj(1) == 2
    assert "value" in dir(obj)
    assert "Obj" in repr(obj)
    # Handles can be passed back to the worker
    assert pkg.types(obj).endswith("backend.Obj")
    assert pkg.ident([obj])[0].value() == 42


def test_release(pkg):
    gc.collect()
    count = session().request("ping")
    obj = pkg.make_obj()
    assert session().request("ping") == count + 1
    del obj
    gc.collect()
    assert session().request("ping") == count


# ----------------------------------------------------------------------
#   OUTPUT
# ----------------------------------------------------------------------


def test_streams(pkg, capsys):
    out, err = io.StringIO(), io.StringIO()
    assert pkg.echo_streams(stdout=out, stderr=err) == 0.0
    assert out.getvalue() == "to stdout\n"
    assert err.getvalue() == "to stderr\n"

    captured = {"out": "", "err": ""}

    def printed():
        c = capsys.readouterr()
        captured["out"] += c.out
        captured["err"] += c.err
        return (
            "printed by worker" in captured["out"]
            and "printed on stderr" in captured["err"]
        )
    assert wait_for(printed), captured


# ----------------------------------------------------------------------
#   LIFECYCLE
# ----------------------------------------------------------------------


def test_crash_and_restart(pkg):
    with pytest.raises(remote.WorkerDiedError, match="exit code 3"):
        pkg.crash()
    assert not remote.is_alive()
    with pytest.raises(remote.WorkerDiedError):
        pkg.test_runtime()
    with pytest.raises(remote.WorkerDiedError):
        remote.import_deployed("fake_package")
    remote.shutdown()
    remote.init()
    pkg = remote.import_deployed("fake_package")
    assert pkg.test_runtime() == "success"


def test_shutdown(pkg):
    s = session()
    tmpdir, proc = s.tmpdir, s.proc
    assert op.isdir(tmpdir)
    obj = pkg.make_obj()
    remote.shutdown()
    assert proc.returncode == 0
    assert not op.exists(tmpdir)
    assert not remote.is_alive()
    remote.shutdown()
    del obj  # does not fail
    with pytest.raises(remote.WorkerDiedError):
        pkg.test_runtime()


def test_kill(pkg):
    proc = session().proc
    remote.kill()
    assert proc.returncode is not None
    assert not remote.is_alive()


def test_init_twice(pkg):
    with pytest.raises(ValueError, match="already initialized"):
        remote.init()


def test_import_without_init(fake_env):
    pkg = remote.import_deployed("fake_package")
    assert pkg.test_runtime() == "success"
    a, b = remote.import_deployed("a", "b")
    assert a.add(1, 2) == b.add(1, 2) == 3


def test_secrets_not_inherited(pkg):
    assert pkg.getenv(protocol.ENV_KEY) is None
    assert pkg.getenv(protocol.ENV_ADDR) is None
    assert protocol.ENV_KEY not in os.environ
    assert all(protocol.ENV_KEY not in arg for arg in session().proc.args)


# ----------------------------------------------------------------------
#   CONCURRENCY / INTERRUPTION
# ----------------------------------------------------------------------


def test_threads(pkg):
    results = []

    def run(x):
        results.append(pkg.sleep(x))

    thread = threading.Thread(target=run, args=(0.0,))
    thread.start()
    thread.join()
    assert results == [0.0]

    tic = time.monotonic()
    threads = [threading.Thread(target=run, args=(0.3,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert time.monotonic() - tic >= 0.6
    assert results == [0.0, 0.3, 0.3]


def test_interrupt_discards_stale_reply(pkg):
    timer = threading.Timer(0.3, _thread.interrupt_main)
    tic = time.monotonic()
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        pkg.sleep(1.5)
    assert time.monotonic() - tic < 1.0
    # The reply to `sleep` is discarded
    assert pkg.add(1, 2) == 3
    assert pkg.ident("x") == "x"


def test_call_timeout(pkg):
    with pytest.raises(TimeoutError):
        pkg.sleep(1.0, _timeout=0.2)
    assert pkg.add(1, 2) == 3


# ----------------------------------------------------------------------
#   STARTUP
# ----------------------------------------------------------------------


def make_mwpython(tmp_path, arch):
    root = tmp_path / "R2024b"
    app = root / "bin" / arch / "mwpython.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "mwpython").touch()
    (root / "bin" / "mwpython").touch()
    return str(root), str(app / "mwpython")


def test_worker_argv_env_macos(tmp_path):
    from matlab_runtime import cli
    root, app = make_mwpython(tmp_path, "maca64")
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    environ = dict(os.environ)
    with mock.patch("matlab_runtime.utils.guess_arch", lambda: "maca64"), \
         mock.patch.object(cli, "guess_arch", lambda: "maca64"), \
         mock.patch.object(cli, "iter_existing_installations",
                           lambda variant: iter([(root, "R2024b")])), \
         mock.patch.dict(cli.SUPPORTED_PYTHON_VERSIONS, {"R2024b": [pyver]}):
        argv, env = client._worker_argv_env(variant="R2024b")
        assert argv == [
            app, "-m", "matlab_runtime.remote.worker", "-mwpythonver", pyver
        ]
        assert f"{root}/bin/maca64" in env["DYLD_FALLBACK_LIBRARY_PATH"]
        assert env["PYTHONHOME"] == sys.prefix
        assert env["PYTHONPATH"].split(os.pathsep)[:len(sys.path)] == sys.path
        assert dict(os.environ) == environ
        # An explicit interpreter bypasses mwpython
        argv, env = client._worker_argv_env(executable="/my/python")
        assert argv == ["/my/python", "-m", "matlab_runtime.remote.worker"]


def test_worker_argv_env_other():
    with mock.patch("matlab_runtime.utils.guess_arch", lambda: "glnxa64"):
        argv, env = client._worker_argv_env()
    assert argv == [sys.executable, "-m", "matlab_runtime.remote.worker"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == ROOT
    assert "DYLD_FALLBACK_LIBRARY_PATH" not in env or (
        env["DYLD_FALLBACK_LIBRARY_PATH"]
        == os.environ["DYLD_FALLBACK_LIBRARY_PATH"]
    )


def test_wrong_authkey_refused(fake_env, tmp_path):
    log = tmp_path / "log"
    executable = fake_python(tmp_path, f"""
        import os, sys
        from multiprocessing.connection import Client
        address = os.environ["{protocol.ENV_ADDR}"]
        try:
            Client(address, authkey=b"wrong key")
            result = "accepted"
        except Exception as e:
            result = "refused: " + type(e).__name__
        open({str(log)!r}, "w").write(result)
        sys.exit(7)
    """)
    with pytest.raises(remote.WorkerDiedError, match="exit code 7"):
        remote.init(executable=executable)
    assert log.read_text() == "refused: AuthenticationError"
    assert not remote.is_alive()


def test_startup_timeout(fake_env, tmp_path):
    executable = fake_python(tmp_path, """
        import time
        time.sleep(60)
    """)
    tic = time.monotonic()
    with pytest.raises(TimeoutError):
        remote.init(executable=executable, start_timeout=1)
    assert time.monotonic() - tic < 10
    assert not remote.is_alive()


def test_startup_failure(fake_env, tmp_path):
    executable = fake_python(tmp_path, """
        import sys
        print("something went wrong", file=sys.stderr)
        sys.exit(5)
    """)
    with pytest.raises(remote.WorkerDiedError, match="something went wrong"):
        remote.init(executable=executable)


def test_lazy_import():
    code = (
        "import sys, matlab_runtime\n"
        "print('matlab_runtime.remote' in sys.modules)\n"
        "print(matlab_runtime.remote.__name__)\n"
    )
    p = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert p.stdout.split() == ["False", "matlab_runtime.remote"]
