"""Tests of matlab_runtime.remote, using a fake MATLAB SDK (no MATLAB)."""
import _thread
import errno
import gc
import io
import os
import os.path as op
import shlex
import subprocess
import sys
import textwrap
import threading
import time
import warnings
from multiprocessing import Pipe
from unittest import mock

import pytest

np = pytest.importorskip("numpy")

from matlab_runtime import remote  # noqa: E402
from matlab_runtime.remote import (  # noqa: E402
    client, convert, protocol, worker,
)
from matlab_runtime.utils import python_argv  # noqa: E402

from ._procs import pid_alive  # noqa: E402
from .test_runtime_discovery import fake_runtime  # noqa: E402

BACKEND = "tests._fake_sdk.backend:FakeBackend"
ROOT = op.dirname(op.dirname(op.abspath(__file__)))


def shm_exists(name):
    """Whether a shared memory segment still exists (any platform)."""
    from multiprocessing.shared_memory import SharedMemory
    try:
        if sys.version_info >= (3, 13):
            shm = SharedMemory(name, track=False)
        else:
            shm = SharedMemory(name)
            if os.name == "posix":
                from multiprocessing import resource_tracker
                resource_tracker.unregister(shm._name, "shared_memory")
    except FileNotFoundError:
        return False
    shm.close()
    return True


# Records the names of the segments created by the client (exported
# arguments) and by the worker (announced in its replies).
RECORD_SHM = """
from matlab_runtime.remote import client as _client
_SHM_NAMES = set()
_export = _client.SharedArrays.export
_process = _client.Session._process

def _record_export(self, array):
    out = _export(self, array)
    if isinstance(out, _client.ShmArray):
        _SHM_NAMES.add(out.name)
    return out

def _record_process(self, reply, stale):
    _SHM_NAMES.update(reply.get("shm") or ())
    return _process(self, reply, stale)

_client.SharedArrays.export = _record_export
_client.Session._process = _record_process
"""


@pytest.fixture
def shm_names(monkeypatch):
    """Set of the names of all segments created during the test."""
    names = set()
    export = client.SharedArrays.export
    process = client.Session._process

    def record_export(self, array):
        out = export(self, array)
        if isinstance(out, protocol.ShmArray):
            names.add(out.name)
        return out

    def record_process(self, reply, stale):
        names.update(reply.get("shm") or ())
        return process(self, reply, stale)

    monkeypatch.setattr(client.SharedArrays, "export", record_export)
    monkeypatch.setattr(client.Session, "_process", record_process)
    return names


def leaked(names):
    """Names of the segments that still exist."""
    return [name for name in names if shm_exists(name)]


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv(protocol.ENV_BACKEND, BACKEND)
    monkeypatch.setenv(protocol.ENV_SHM_THRESHOLD, "4096")
    yield
    remote.shutdown()


@pytest.fixture
def pkg(fake_env):
    remote.init(use_mpython=False)
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


def fake_worker(monkeypatch, tmp_path, code):
    """Make the next session start a worker that runs `code` instead.

    The command is `python_argv(script)` (a shebang script cannot be
    executed on Windows).
    """
    script = tmp_path / "fake_worker.py"
    script.write_text(textwrap.dedent(code))

    def argv_env(*args, **kwargs):
        return python_argv(str(script)), dict(os.environ)
    monkeypatch.setattr(client, "_worker_argv_env", argv_env)


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


def test_shared_memory(pkg, shm_names):
    x = np.random.rand(100, 50)
    assert x.nbytes >= 4096
    y = pkg.ident(x)
    assert (y == x).all()
    y = pkg.big(10000)
    assert y.shape == (1, 10000)
    assert (y[0] == np.arange(10000)).all()
    # Segments were used both ways
    assert len(shm_names) >= 3
    # The client unlinks its arguments' segments when it gets the reply,
    # and the segments of the results once they are gone
    del y
    gc.collect()
    session().request("ping")
    assert leaked(shm_names) == []


def test_shared_memory_abandoned_reply(pkg, shm_names, monkeypatch):
    # The reply to an abandoned call (with segments) arrives while the
    # next call is in flight. Acknowledging it must not free the
    # segments of the next reply (slow down attach to expose it).
    attach = client.SharedArrays.attach

    def slow_attach(ref, *args):
        time.sleep(0.3)
        return attach(ref, *args)
    monkeypatch.setattr(client.SharedArrays, "attach", slow_attach)
    pkg.big(1)  # cache the method: no getattr request in between
    with pytest.raises(TimeoutError):
        pkg.slow_big(0.5, 10000, _timeout=0.1)
    for _ in range(2):
        y = pkg.big(10000)
        assert (y[0] == np.arange(10000)).all()
    assert len(shm_names) == 3
    del y
    gc.collect()
    session().request("ping")
    assert leaked(shm_names) == []


def test_interrupted_conversion(pkg, shm_names, monkeypatch):
    # Ctrl-C while the result is converted: its remote objects are
    # released and its segments acknowledged.
    pkg.big_and_obj(1)  # cache the method
    gc.collect()
    count = session().request("ping")

    def interrupt(ref, *args):
        raise KeyboardInterrupt
    with monkeypatch.context() as m:
        m.setattr(client.SharedArrays, "attach", interrupt)
        with pytest.raises(KeyboardInterrupt):
            pkg.big_and_obj(10000)
    assert remote.is_alive()
    assert session().request("ping") == count
    assert len(shm_names) == 1 and leaked(shm_names) == []
    y, obj = pkg.big_and_obj(10000)
    assert y.shape == (1, 10000) and obj.value() == 42


def segment_of(array):
    """The `SharedSegment` that owns the memory of `array` (or None)."""
    return convert._segment_of(array)


@pytest.fixture
def exports(monkeypatch):
    """`(output, new segments)` of each array exported by the client."""
    calls = []
    export = client.SharedArrays.export

    def spy(self, array):
        before = len(self.segments)
        out = export(self, array)
        calls.append((out, len(self.segments) - before))
        return out
    monkeypatch.setattr(client.SharedArrays, "export", spy)
    return calls


def test_results_are_views(pkg):
    y = pkg.big(10000)
    segment = segment_of(y)
    assert isinstance(segment, convert.SharedSegment) and segment.linked
    assert y.flags.writeable
    view = np.asarray(segment)
    assert np.shares_memory(y, view)
    view[0, 0] = -1
    assert y[0, 0] == -1


def test_round_trip_by_reference(pkg, exports):
    x = np.random.rand(100, 50)
    y = pkg.ident(x)
    assert len(exports) == 1 and exports[0][1] == 1
    name = segment_of(y).name
    # Passed back without a new segment
    z = pkg.ident(y)
    out, created = exports[1]
    assert isinstance(out, protocol.ShmArray) and out.name == name
    assert created == 0
    assert (z == x).all() and not np.shares_memory(y, z)
    # In-place edits are sent too
    y[0, 0] = -1
    z = pkg.ident(y)
    assert exports[2][0].name == name and z[0, 0] == -1
    assert (z == y).all()


def test_views_by_reference(pkg, exports):
    class Sub(np.ndarray):
        pass
    y = pkg.ident(np.random.rand(100, 50))
    assert y.flags.f_contiguous
    name = segment_of(y).name
    # A subclass view (as mpython does)
    s = np.ndarray.view(y, Sub)
    assert (pkg.ident(s) == y).all()
    assert exports[-1][0].name == name and exports[-1][1] == 0
    # A contiguous slice, at an offset
    assert (pkg.ident(y[:, 20:40]) == y[:, 20:40]).all()
    out, created = exports[-1]
    assert out.name == name and out.offset == 20 * 100 * 8
    assert created == 0
    # Not contiguous: copied into a new segment
    assert (pkg.ident(y[10:90]) == y[10:90]).all()
    out, created = exports[-1]
    assert out.name != name and created == 1


@pytest.mark.skipif(os.name != "posix", reason="segments have no name")
def test_result_segment_freed(pkg):
    y = pkg.big(10000)
    v = y[:, 10:]
    name = segment_of(y).name
    session().request("ping")
    assert shm_exists(name)
    del y
    gc.collect()
    assert shm_exists(name)
    del v
    gc.collect()
    assert not shm_exists(name)


def test_result_segments_unlinked_on_shutdown(pkg, exports):
    y = pkg.big(10000)
    segment = segment_of(y)
    remote.shutdown()
    assert not segment.linked
    if os.name == "posix":
        assert not shm_exists(segment.name)
    # Still usable, and sent by copy to the next worker
    assert (y[0] == np.arange(10000)).all()
    remote.init(use_mpython=False)
    pkg = remote.import_deployed("fake_package")
    assert (pkg.ident(y) == y).all()
    assert exports[-1][0].name != segment.name and exports[-1][1] == 1


def test_sessions_own_their_segments(pkg, exports):
    # Closing one session does not unlink the results of another, and
    # a session does not send another's segments by reference.
    other = client.Session(use_mpython=False)
    other.start()
    try:
        pkg2 = other.request("import", name="fake_package", file=None,
                             option_list=[])
        y = pkg.big(10000)
        y2 = pkg2.big(10000)
        segment, segment2 = segment_of(y), segment_of(y2)
        assert (pkg2.ident(y) == y).all()
        assert exports[-1][0].name != segment.name
        other.close()
        assert not segment2.linked and segment.linked
        if os.name == "posix":
            assert shm_exists(segment.name)
            assert not shm_exists(segment2.name)
        assert (pkg.ident(y) == y).all()
        assert exports[-1][0].name == segment.name
        assert (y2[0] == np.arange(10000)).all()
    finally:
        other.close()


def test_reference_at_offset():
    # A `ShmArray` with an offset, then a slice of it by reference
    owner = object()
    data = np.arange(3000, dtype=np.float64)
    sender = convert.SharedArrays(4096)
    seg = sender.export(data)
    ref = protocol.ShmArray(seg.name, data.dtype, (1000,), "C", 1000 * 8)
    try:
        y = convert.SharedArrays.attach(ref, owner)
        assert (y == data[1000:2000]).all()
        shared = convert.SharedArrays(4096, owner=owner)
        out = shared.export(y[200:])
        assert isinstance(out, protocol.ShmArray) and out.name == seg.name
        assert out.offset == 1200 * 8
        z = convert.SharedArrays.read(out, np.array)
        assert (z == data[1200:2000]).all()
        del y
        gc.collect()
    finally:
        sender.release(keep=[seg.name])
        convert.unlink_owned(owner)


def test_worker_reads_shared_memory():
    # matlab.* constructors copy: the worker can close the segment
    # (which the client owns) right after converting it.
    from tests._fake_sdk.backend import matlab
    x = np.random.rand(100, 50)
    shared = convert.SharedArrays(4096)
    ref = shared.export(x)
    try:
        y = worker.Worker(None, None).from_wire([ref])[0]
        assert isinstance(y, matlab.double)
        view = np.ndarray(x.shape, x.dtype, shared.segments[0].buf)
        view[...] = 0
        del view
        assert (np.asarray(y) == x).all()
    finally:
        shared.release()


def test_worker_reads_shared_memory_no_copy(monkeypatch):
    # A constructor that keeps the initializer gets a private copy
    from tests._fake_sdk.backend import matlab

    def init(self, initializer=None, size=None, is_complex=False):
        self.initializer = initializer

    def array(self, dtype=None, copy=None):
        return np.asarray(self.initializer, dtype)

    # Not a subclass: when the real SDK was imported earlier in the
    # session, `matlab` is the real module and its types are final.
    borrowing = type("double", (), {
        "__init__": init, "__array__": array, "__module__": "matlab",
    })
    monkeypatch.setattr(matlab, "double", borrowing)
    monkeypatch.setattr(convert, "_COPIES", {})
    x = np.random.rand(100, 50)
    shared = convert.SharedArrays(4096)
    ref = shared.export(x)
    try:
        y = worker.Worker(None, None).from_wire([ref])[0]
        assert isinstance(y, borrowing)
        view = np.ndarray(x.shape, x.dtype, shared.segments[0].buf)
        assert not np.shares_memory(y.initializer, view)
        view[...] = 0
        del view
        assert (np.asarray(y) == x).all()
    finally:
        shared.release()


def _serve(conn):
    from tests._fake_sdk.backend import FakeBackend, FakePackage
    w = worker.Worker(conn, FakeBackend())
    w.shm_threshold = 4096
    ref = w._store(FakePackage())
    thread = threading.Thread(target=w.serve, daemon=True)
    thread.start()
    return ref, thread


def _big_request(rid, ref):
    return {"id": rid, "op": "call", "ref": ref, "name": "big",
            "args": protocol.dumps(([10000], {})), "release": []}


def test_worker_frees_segments_on_exit(shm_names):
    # Replies that were never acknowledged
    here, there = Pipe()
    ref, thread = _serve(there)
    for rid in (1, 2):
        protocol.send(here, _big_request(rid, ref))
        assert protocol.recv(here)["ok"]
    assert len(shm_names) == 2 and len(leaked(shm_names)) == 2
    here.close()
    thread.join(5)
    assert not thread.is_alive()
    assert leaked(shm_names) == []


def test_worker_frees_segments_reply_not_sent(shm_names):
    # The client is gone when the reply is sent
    here, there = Pipe()

    class Broken:
        def recv_bytes(self):
            return there.recv_bytes()

        def send_bytes(self, data):
            raise BrokenPipeError

        def close(self):
            there.close()
    ref, thread = _serve(Broken())
    protocol.send(here, _big_request(1, ref))
    thread.join(5)
    assert not thread.is_alive()
    assert len(shm_names) == 1 and leaked(shm_names) == []
    here.close()


def test_shared_memory_no_leak_warnings(tmp_path):
    script = tmp_path / "script.py"
    script.write_text(RECORD_SHM + textwrap.dedent("""
        import numpy as np
        from matlab_runtime import remote
        remote.init(use_mpython=False)
        pkg = remote.import_deployed("fake_package")
        x = np.random.rand(1000)
        assert (pkg.ident(x) == x).all()
        assert pkg.big(1000).shape == (1, 1000)
        # Still alive at exit: unlinked by the atexit handler
        y = pkg.big(1000)
        assert (pkg.ident(y) == y).all()
        assert len(_SHM_NAMES) == 5
        print("names:", *_SHM_NAMES)
        print("done")
    """))
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONWARNINGS="always")
    env[protocol.ENV_BACKEND] = BACKEND
    env[protocol.ENV_SHM_THRESHOLD] = "100"
    p = subprocess.run(
        python_argv(str(script)), env=env, capture_output=True,
        # Generous: under mwpython, both this script and its worker
        # start mwpython (slow on macOS Intel runners)
        text=True, timeout=300,
    )
    assert p.returncode == 0, p.stderr
    assert "done" in p.stdout
    assert "Warning" not in p.stderr, p.stderr
    assert "leaked" not in p.stderr, p.stderr
    assert "Exception ignored" not in p.stderr, p.stderr
    names = [line for line in p.stdout.splitlines() if line[:6] == "names:"]
    assert leaked(names[0].split()[1:]) == []


def test_shared_memory_disabled(fake_env, monkeypatch):
    monkeypatch.setenv(protocol.ENV_SHM_THRESHOLD, "0")
    remote.init(use_mpython=False)
    pkg = remote.import_deployed("fake_package")
    x = np.random.rand(10000)
    assert (pkg.ident(x) == x).all()
    assert pkg.big(10000).shape == (1, 10000)


def test_shared_memory_no_space(pkg, shm_names, monkeypatch):
    # Without room for a segment, arrays are sent inline
    created = []

    def no_space(shm):
        created.append(shm.name)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(convert, "_reserve", no_space)
    shared = convert.SharedArrays(4096)
    x = np.random.rand(10000)
    assert shared.export(x) is x
    assert shared.segments == []
    assert len(created) == 1 and leaked(created) == []
    assert (pkg.ident(x) == x).all()
    assert len(created) == 2 and leaked(created) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux")
def test_shared_memory_no_space_linux(monkeypatch):
    def enospc(*args):
        raise OSError(errno.ENOSPC, "No space left on device")

    x = np.random.rand(10000)
    shared = convert.SharedArrays(4096)
    monkeypatch.setattr(os, "posix_fallocate", enospc)
    assert shared.export(x) is x
    assert shared.segments == []
    # Fallback to the free space of /dev/shm
    monkeypatch.delattr(os, "posix_fallocate")
    monkeypatch.setattr(os, "statvfs", lambda path: mock.Mock(
        f_bavail=0, f_frsize=4096))
    assert shared.export(x) is x
    assert shared.segments == []


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
    remote.init(use_mpython=False)
    pkg = remote.import_deployed("fake_package")
    assert pkg.test_runtime() == "success"


def test_shutdown(pkg):
    s = session()
    tmpdir, proc = s.tmpdir, s.proc
    if sys.platform == "win32":
        assert tmpdir is None  # named pipe: no socket directory
    else:
        assert op.isdir(tmpdir)
    obj = pkg.make_obj()
    remote.shutdown()
    assert proc.returncode == 0
    assert tmpdir is None or not op.exists(tmpdir)
    assert not remote.is_alive()
    remote.shutdown()
    del obj  # does not fail
    with pytest.raises(remote.WorkerDiedError):
        pkg.test_runtime()


def test_kill(pkg):
    proc = session().proc
    child = pkg.spawn()
    assert pid_alive(child)
    remote.kill()
    assert proc.returncode is not None
    assert not remote.is_alive()
    # The processes started by the worker are stopped too
    assert wait_for(lambda: not pid_alive(child))


def test_init_twice(pkg):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="already initialized"):
            remote.init(use_mpython=False)
    # Process options cannot change while the worker runs
    with pytest.warns(UserWarning, match="ignoring shm_threshold"):
        with pytest.raises(ValueError, match="already initialized"):
            remote.init(use_mpython=False, shm_threshold=1)


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
    # The calls are serialized. Allow for the resolution of the clock
    # (~16 ms on Windows).
    assert time.monotonic() - tic >= 0.6 - 0.05
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


def make_mwpython(prefix, arch, release="R2024b"):
    root = fake_runtime(prefix / release, release, mwpython=True)
    app = prefix / release / "bin" / arch / "mwpython.app" / "Contents"
    app = app / "MacOS"
    app.mkdir(parents=True)
    (app / "mwpython").touch()
    return root, str(app / "mwpython")


@pytest.fixture
def isolated_runtimes(tmp_path, monkeypatch):
    """Runtime discovery only sees `tmp_path/prefix` (default prefix)
    and `tmp_path/other/<release>` (standard location)."""
    from matlab_runtime import utils
    prefix, other = tmp_path / "prefix", tmp_path / "other"
    prefix.mkdir()
    other.mkdir()
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    monkeypatch.setattr(utils, "guess_prefix", lambda: str(prefix))
    monkeypatch.setattr(utils, "user_prefix", lambda: str(tmp_path / "user"))
    monkeypatch.setattr(utils, "CANDIDATE_LOCATIONS_BY_OS", {
        key: [str(other / "{release}")] for key in ("mac", "gln", "win")
    })
    monkeypatch.setattr(utils.shutil, "which", lambda name: None)
    monkeypatch.delenv("MATLAB_RUNTIME_PATH", raising=False)
    monkeypatch.delenv("MATLAB_PATH", raising=False)
    monkeypatch.setattr(utils, "SUPPORTED_PYTHON_VERSIONS", {
        "R2024b": [pyver], "R2025a": [pyver],
    })
    return {"prefix": prefix, "other": other, "pyver": pyver}


@pytest.fixture
def macos(isolated_runtimes, monkeypatch):
    """Simulate macOS with runtimes R2024b in the default prefix and
    R2025a (more recent) in a standard location."""
    from matlab_runtime import cli, utils
    monkeypatch.setattr(utils, "guess_arch", lambda: "maca64")
    monkeypatch.setattr(cli, "guess_arch", lambda: "maca64")
    prefix = isolated_runtimes["prefix"]
    root, app = make_mwpython(prefix, "maca64")
    other_root, other_app = make_mwpython(
        isolated_runtimes["other"], "maca64", "R2025a"
    )
    return {"prefix": str(prefix), "root": root, "app": app,
            "other_root": other_root, "other_app": other_app,
            "pyver": isolated_runtimes["pyver"]}


def test_worker_argv_env_macos(macos):
    pyver = macos["pyver"]
    environ = dict(os.environ)
    # Discovery as in matlab_runtime.init (utils.resolve_runtime)
    for variant, root, app in (
        ("R2024b", macos["root"], macos["app"]),
        ("latest_installed", macos["other_root"], macos["other_app"]),
    ):
        argv, env = client._worker_argv_env(variant=variant)
        # mwpython paths are built with "/" (also when simulating
        # macOS on Windows)
        assert [op.normpath(argv[0]), *argv[1:]] == [
            op.normpath(app),
            "-m", "matlab_runtime.remote.worker", "-mwpythonver", pyver,
        ]
        assert f"{root}/bin/maca64" in env["DYLD_FALLBACK_LIBRARY_PATH"]
        assert env["PYTHONHOME"] == sys.prefix
        assert env["PYTHONPATH"].split(os.pathsep)[:len(sys.path)] \
            == sys.path
        assert env["MATLAB_RUNTIME_MODE"] == "inprocess"
    assert dict(os.environ) == environ
    # An explicit interpreter bypasses mwpython
    argv, env = client._worker_argv_env(executable="/my/python")
    assert argv == ["/my/python", "-m", "matlab_runtime.remote.worker"]
    assert env["MATLAB_RUNTIME_MODE"] == "inprocess"


def test_worker_argv_env_macos_prefix(macos, tmp_path):
    from matlab_runtime import cli
    from matlab_runtime.utils import RuntimeNotFoundError
    # A release folder as prefix pins the installation
    argv, env = client._worker_argv_env(prefix=macos["root"])
    assert op.normpath(argv[0]) == op.normpath(macos["app"])
    assert "other" not in env["DYLD_FALLBACK_LIBRARY_PATH"]
    with pytest.raises(RuntimeNotFoundError):
        client._worker_argv_env(variant="R2012a", prefix=macos["prefix"])
    # mpython: most recent installation
    argv, env = cli._mpython_command(["-c", "pass"])
    assert op.normpath(argv[0]) == op.normpath(macos["other_app"])


@pytest.mark.parametrize("version,key", [
    ("R2024b", "root"), ("latest_installed", "other_root"),
])
def test_init_prefix_macos(fake_env, macos, monkeypatch, version, key):
    # The client resolves the installation once, and uses it both to
    # start mwpython and to initialize the SDK.
    calls = []
    argv_env = client._worker_argv_env

    def record(executable, use_mpython, variant, prefix):
        calls.append((use_mpython, variant, prefix))
        return argv_env(variant=variant, use_mpython=False)
    monkeypatch.setattr(client, "_worker_argv_env", record)
    remote.init(version, prefix=macos["prefix"])
    release = "R2024b" if key == "root" else "R2025a"
    assert calls == [(None, release, macos[key])]
    pkg = remote.import_deployed("fake_package")
    assert pkg.init_kwargs() == {"version": release, "prefix": macos[key]}


def test_init_install_if_missing(fake_env, isolated_runtimes, monkeypatch):
    # The runtime is installed by the client, then used by the worker
    from matlab_runtime import impl
    installed = []

    def install(release, prefix, auto_answer, default_scope=None, **kwargs):
        installed.append((release, prefix, auto_answer, default_scope))
        fake_runtime(isolated_runtimes["prefix"] / release, release)
    monkeypatch.setattr(impl, "install", install)
    remote.init("R2024b", install_if_missing=True, auto_answer=True,
                default_scope="user", use_mpython=False)
    assert installed == [("R2024b", None, True, "user")]
    pkg = remote.import_deployed("fake_package")
    root = str(isolated_runtimes["prefix"] / "R2024b")
    assert pkg.init_kwargs() == {"version": "R2024b", "prefix": root}


def test_import_deployed_install_if_missing(fake_env, isolated_runtimes,
                                            monkeypatch):
    # The release of the CTF is installed by the client, with only the
    # products the package needs, then used by the worker.
    from matlab_runtime import impl, utils
    installed = []

    def install(release, prefix, auto_answer, **kwargs):
        installed.append((release, kwargs["ctf"]))
        fake_runtime(isolated_runtimes["prefix"] / release, release)
    monkeypatch.setattr(impl, "install", install)
    real_init = remote.init

    def init(*args, **kwargs):
        return real_init(*args, use_mpython=False, **kwargs)
    monkeypatch.setattr(remote, "init", init)
    from . import _test_runtime
    pkg = remote.import_deployed(
        _test_runtime, install_if_missing=True, auto_answer=True,
        only_needed_products=True,
    )
    assert installed == [("R2024b", [utils.deployed_ctf(_test_runtime)])]
    root = str(isolated_runtimes["prefix"] / "R2024b")
    assert pkg.init_kwargs() == {"version": "R2024b", "prefix": root}


def test_import_deployed_missing_products(fake_env, isolated_runtimes,
                                          monkeypatch):
    # R2024b was installed with Numerics only: the Python product that
    # the package needs is added (here) before the worker starts.
    from matlab_runtime import impl, utils
    root = fake_runtime(isolated_runtimes["prefix"] / "R2024b", "R2024b")
    utils.record_installed_products(
        root, {"35010": "MATLAB Runtime - Numerics"}
    )
    installed = []

    def install(release, prefix, auto_answer, **kwargs):
        installed.append((release, prefix))
        utils.record_installed_products(root)
    monkeypatch.setattr(impl, "install", install)
    real_init = remote.init

    def init(*args, **kwargs):
        return real_init(*args, use_mpython=False, **kwargs)
    monkeypatch.setattr(remote, "init", init)
    from . import _test_runtime
    pkg = remote.import_deployed(_test_runtime, install_if_missing=True)
    assert installed == [("R2024b", str(isolated_runtimes["prefix"]))]
    assert pkg.init_kwargs() == {"version": "R2024b", "prefix": root}


def test_init_not_found(fake_env, isolated_runtimes):
    # Through mwpython, the runtime is resolved in the client: the error
    # is raised here and no worker is started.
    from matlab_runtime import utils
    from matlab_runtime.utils import RuntimeNotFoundError
    with mock.patch.object(utils, "guess_arch", lambda: "maca64"):
        with pytest.raises(RuntimeNotFoundError):
            remote.init("R2024b")
    assert not remote.is_alive()


def test_import_deployed_infers_release(fake_env, isolated_runtimes):
    # Without init, the release is read from the package's CTF here
    # (R2024b), although R2025a is more recent.
    fake_runtime(isolated_runtimes["prefix"] / "R2024b", "R2024b")
    fake_runtime(isolated_runtimes["other"] / "R2025a", "R2025a")
    calls = []
    real_init = remote.init

    def init(version="latest_installed", *args, **kwargs):
        calls.append(version)
        return real_init(version, *args, use_mpython=False, **kwargs)
    with mock.patch.object(remote, "init", init):
        from . import _test_runtime
        pkg = remote.import_deployed(_test_runtime)
    assert calls == ["R2024b"]
    assert pkg.init_kwargs()["version"] == "R2024b"


def test_worker_error_types(pkg):
    # Exceptions of matlab_runtime.utils keep their type
    from matlab_runtime.utils import RuntimeNotFoundError
    with pytest.raises(RuntimeNotFoundError, match="nothing here"):
        pkg.fail_not_found("nothing here")


def test_worker_argv_env_other():
    with mock.patch("matlab_runtime.utils.guess_arch", lambda: "glnxa64"):
        argv, env = client._worker_argv_env()
    assert argv == python_argv("-m", "matlab_runtime.remote.worker")
    root = op.dirname(op.dirname(op.dirname(op.abspath(client.__file__))))
    path = env.get("PYTHONPATH", "").split(os.pathsep)
    if op.basename(root) in client.SITE_DIRS:
        # Installed package: the environment is unchanged
        assert env.get("PYTHONPATH") == os.environ.get("PYTHONPATH")
    else:
        # Source checkout: the worker imports this very copy
        assert path[0] == root
    assert "DYLD_FALLBACK_LIBRARY_PATH" not in env or (
        env["DYLD_FALLBACK_LIBRARY_PATH"]
        == os.environ["DYLD_FALLBACK_LIBRARY_PATH"]
    )


def test_python_argv(monkeypatch):
    from matlab_runtime import utils
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    for exe in (
        "/Applications/MATLAB/MATLAB_Runtime/R2024b/bin/maca64/"
        "mwpython.app/Contents/MacOS/mwpython",
        "/opt/runtime/bin/maca64/mwpython3.12.app/Contents/MacOS/"
        "mwpython3.12",
        "/some/where/mwpython",
    ):
        monkeypatch.setattr(sys, "executable", exe)
        assert utils.running_under_mwpython()
        assert utils.python_argv("-c", "pass") == [
            exe, "-c", "pass", "-mwpythonver", pyver
        ]
    for exe in ("/usr/bin/python3", r"C:\Python312\python.exe", ""):
        monkeypatch.setattr(sys, "executable", exe)
        assert not utils.running_under_mwpython()
        assert utils.python_argv("-c", "pass") == [exe, "-c", "pass"]


FAKE_MWPYTHON = """#!/bin/sh
# Mimics MathWorks' mwpython: requires a trailing `-mwpythonver X.Y`.
n=$#
if [ "$n" -lt 2 ]; then
    echo "mwpython: unexpected arguments: $*" >&2; exit 252
fi
eval "flag=\\${{$((n - 1))}}"
if [ "$flag" != "-mwpythonver" ]; then
    echo "mwpython: unexpected arguments: $*" >&2; exit 252
fi
i=0
for arg do
    i=$((i + 1))
    if [ "$i" -le $((n - 2)) ]; then set -- "$@" "$arg"; fi
    shift
done
exec {python} "$@"{suffix}
"""


@pytest.fixture
def fake_mwpython(monkeypatch, tmp_path):
    """Run the client as if under MathWorks' `mwpython`."""
    exe = tmp_path / "mwpython"
    # If the real interpreter is itself mwpython (e.g., macOS CI runs the
    # tests under `mwpython2`), hand the trailing argument back to it.
    suffix = "".join(
        " " + shlex.quote(a) for a in python_argv()[1:]
    )
    exe.write_text(FAKE_MWPYTHON.format(
        python=shlex.quote(sys.executable), suffix=suffix
    ))
    exe.chmod(0o755)
    real = sys.executable
    # Without the trailing argument, the fake mwpython refuses to start
    p = subprocess.run([str(exe), "-c", "pass"], capture_output=True)
    assert p.returncode == 252
    p = subprocess.run(
        [str(exe), "-c", "import sys; print(sys.argv[1:])", "a b", "c",
         "-mwpythonver", "3.x"], capture_output=True, text=True,
    )
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "['a b', 'c']"
    monkeypatch.setattr(sys, "executable", str(exe))
    yield exe
    monkeypatch.setattr(sys, "executable", real)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell script")
def test_worker_under_mwpython(fake_env, fake_mwpython):
    # The client runs under mwpython (e.g. a script started with
    # `mpython`): the worker is started with `-mwpythonver X.Y`.
    argv, _ = client._worker_argv_env(use_mpython=False)
    assert argv[0] == str(fake_mwpython)
    assert argv[-2] == "-mwpythonver"
    remote.init(use_mpython=False)
    pkg = remote.import_deployed("fake_package")
    np = pytest.importorskip("numpy")
    x = np.arange(10.0)
    assert (pkg.ident(x) == x).all()


def test_wrong_authkey_refused(fake_env, monkeypatch, tmp_path):
    log = tmp_path / "log"
    fake_worker(monkeypatch, tmp_path, f"""
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
        remote.init(use_mpython=False)
    assert log.read_text() == "refused: AuthenticationError"
    assert not remote.is_alive()


def test_startup_timeout(fake_env, monkeypatch, tmp_path):
    fake_worker(monkeypatch, tmp_path, """
        import time
        time.sleep(60)
    """)
    tic = time.monotonic()
    with pytest.raises(TimeoutError):
        remote.init(use_mpython=False, start_timeout=1)
    assert time.monotonic() - tic < 10
    assert not remote.is_alive()


def test_startup_failure(fake_env, monkeypatch, tmp_path):
    fake_worker(monkeypatch, tmp_path, """
        import sys
        print("something went wrong", file=sys.stderr)
        sys.exit(5)
    """)
    with pytest.raises(remote.WorkerDiedError, match="something went wrong"):
        remote.init(use_mpython=False)


def test_lazy_import():
    code = (
        "import sys, matlab_runtime\n"
        "print('matlab_runtime.remote' in sys.modules)\n"
        "print(matlab_runtime.remote.__name__)\n"
    )
    p = subprocess.run(
        python_argv("-c", code), capture_output=True, text=True, cwd=ROOT
    )
    assert p.stdout.split() == ["False", "matlab_runtime.remote"]


# ----------------------------------------------------------------------
#   MPYTHON SUPPORT
# ----------------------------------------------------------------------


def test_matlab_object_handles(pkg):
    # A MATLAB function handle stays in the worker...
    handle = pkg.make_handle(10)
    assert isinstance(handle, remote.RemoteHandle)
    assert remote.is_matlab_object(handle)
    assert not remote.is_matlab_object(pkg)
    assert not remote.is_matlab_object(1.0)
    assert not remote.is_matlab_object(np.ones(3))
    # ... and is the original object when passed back
    assert pkg.is_handle(handle)
    assert pkg.mpython_endpoint(handle, 5.0) == 15.0
    assert handle(1.0) == 11.0


def test_is_matlab_object_inprocess(monkeypatch):
    # In-process `matlab.object` instances (when the SDK is loaded)
    monkeypatch.delitem(sys.modules, "matlab", raising=False)
    assert not remote.is_matlab_object(object())
    from ._fake_sdk import matlab
    monkeypatch.setitem(sys.modules, "matlab", matlab)
    assert remote.is_matlab_object(matlab.object(abs))
    assert not remote.is_matlab_object(matlab.double([1.0]))
    assert not remote.is_matlab_object(object())


def assert_cell(x, size, data0):
    assert type(x) is dict and set(x) == {"type__", "size__", "data__"}
    assert x["type__"] == "cell"
    assert type(x["size__"]) is np.ndarray
    assert x["size__"].dtype == size.dtype
    assert (x["size__"] == size).all() and x["size__"].shape == size.shape
    assert type(x["data__"]) is list and len(x["data__"]) == 2
    assert (x["data__"][0] == data0).all()
    assert x["data__"][1] == "a"


def test_tagged_dicts_round_trip(pkg):
    # mpython's tagged dicts (cells, structs, ...) are plain dicts of
    # numpy arrays: they cross the wire both ways unchanged.
    size = np.array([[1, 2]], dtype=np.int64)
    data0 = np.array([[1.0, 2.0]])
    cell = {"type__": "cell", "size__": size, "data__": [data0, "a"]}
    out = pkg.ident(cell)
    assert_cell(out, size, data0)
    assert_cell(cell, size, data0)  # not modified
    # The worker gets a dict, whose arrays are matlab arrays
    assert pkg.mpython_endpoint("f", cell)["types"] == ["builtins.dict"]
    assert pkg.types(cell["size__"]) == "matlab.int64"
    assert pkg.types(cell["data__"][0]) == "matlab.double"
    # Built in the worker (with matlab arrays)
    assert_cell(pkg.cell(), np.array([[1.0, 2.0]]), data0)
    # Nested
    out = pkg.ident({"type__": "struct", "x": [cell, {"y": cell}]})
    assert out["type__"] == "struct"
    assert_cell(out["x"][0], size, data0)
    assert_cell(out["x"][1]["y"], size, data0)
