"""
Client side of the out-of-process MATLAB runtime.

A `Session` spawns the worker, forwards its output, and exchanges one
request at a time with it. `RemoteHandle` objects proxy the objects
that live in the worker (deployed packages, MATLAB objects, ...).
"""
import collections
import itertools
import os
import os.path as op
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Listener
from multiprocessing import AuthenticationError

from . import protocol
from .convert import SharedArrays, is_ndarray, walk
from .protocol import (
    RemoteRef,
    ShmArray,
    StreamSink,
    WorkerDiedError,
)

WORKER_MODULE = "matlab_runtime.remote.worker"
DEFAULT_SHM_THRESHOLD = 1 << 20
SITE_DIRS = ("site-packages", "dist-packages")


def _worker_argv_env(executable=None, use_mpython=None,
                     variant="latest_installed"):
    """
    Command and environment used to start the worker.

    On macOS, the worker is started through `mwpython` (with the same
    environment fixes as `mpython`), unless an `executable` is given.
    """
    from ..utils import guess_arch
    module = ["-m", WORKER_MODULE]
    if use_mpython is None:
        use_mpython = executable is None and guess_arch()[:3] == "mac"
    if use_mpython:
        from ..cli import mpython_command
        return mpython_command(module, variant)
    env = dict(os.environ)
    root = op.dirname(op.dirname(op.dirname(op.abspath(__file__))))
    if executable is None and op.basename(root) not in SITE_DIRS:
        # matlab_runtime is not installed (e.g., source checkout):
        # make sure that the worker imports this very copy.
        path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([root] + ([path] if path else []))
    return [executable or sys.executable, *module], env


class RemoteHandle:
    """Proxy to an object that lives in the worker."""

    def __init__(self, session, ref):
        self._session = session
        self._ref = ref
        self._methods = set()

    def __getattr__(self, name):
        # MATLAB names never start with an underscore. This also keeps
        # python's (and IPython's) special attributes local.
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._methods:
            return RemoteMethod(self, name)
        result = self._session.request("getattr", ref=self._ref, name=name)
        if "method" in result:
            self._methods.add(name)
            return RemoteMethod(self, name)
        return result["value"]

    def __call__(self, *args, **kwargs):
        return self._session.call(self._ref, None, args, kwargs)

    def __dir__(self):
        return self._session.request("dir", ref=self._ref)

    def __reduce__(self):
        raise TypeError("RemoteHandle objects cannot be pickled or copied")

    def __repr__(self):
        return f"<RemoteHandle #{self._ref.oid}: {self._ref.type}>"

    def __del__(self):
        # Never talk to the worker from here: the oid is sent along
        # with the next request.
        try:
            self._session.release(self._ref.oid)
        except Exception:
            pass


class RemoteMethod:
    """Callable attribute of a `RemoteHandle`."""

    def __init__(self, handle, name):
        self._handle = handle
        self._name = name

    def __call__(self, *args, **kwargs):
        handle = self._handle
        return handle._session.call(handle._ref, self._name, args, kwargs)

    def __repr__(self):
        return f"<RemoteMethod {self._name} of {self._handle!r}>"


class Session:
    """Connection to one worker process."""

    def __init__(self, executable=None, use_mpython=None,
                 variant="latest_installed", start_timeout=600,
                 shm_threshold=None):
        if shm_threshold is None:
            shm_threshold = int(os.environ.get(
                protocol.ENV_SHM_THRESHOLD, DEFAULT_SHM_THRESHOLD
            ))
        self.executable = executable
        self.use_mpython = use_mpython
        self.variant = variant
        self.start_timeout = start_timeout
        self.shm_threshold = shm_threshold
        self.lock = threading.RLock()
        self.ids = itertools.count(1)
        self.released = collections.deque()
        self.shm_out = {}       # request id -> SharedArrays
        self.stderr_tail = collections.deque(maxlen=200)
        self.proc = self.conn = self.tmpdir = None
        self.pumps = []
        self.alive = False
        self.closed = False

    # --- startup ------------------------------------------------------

    def start(self):
        authkey = secrets.token_bytes(32)
        if sys.platform == "win32":
            address = r"\\.\pipe\matlab_runtime-" + secrets.token_hex(16)
        else:
            # mkdtemp creates a directory that only we can access
            self.tmpdir = tempfile.mkdtemp(prefix="mlrt-")
            address = op.join(self.tmpdir, "sock")
        listener = Listener(address, authkey=authkey)
        try:
            argv, env = _worker_argv_env(
                self.executable, self.use_mpython, self.variant
            )
            env[protocol.ENV_ADDR] = address
            env[protocol.ENV_KEY] = authkey.hex()
            opt = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env)
            if sys.platform == "win32":
                opt["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                # Keep Ctrl-C (and Jupyter interrupts) away from the worker
                opt["start_new_session"] = True
            self.proc = subprocess.Popen(argv, **opt)
            self.pumps = [
                self._pump(self.proc.stdout, "stdout"),
                self._pump(self.proc.stderr, "stderr"),
            ]
            self.conn = self._accept(listener)
        except BaseException:
            listener.close()
            self.kill()
            raise
        listener.close()
        self.alive = True
        try:
            self.request(
                "hello",
                sys_path=list(sys.path),
                pid=os.getpid(),
                protocol=protocol.PROTOCOL_VERSION,
                shm_threshold=self.shm_threshold,
            )
        except BaseException:
            self.kill()
            raise

    def _pump(self, pipe, name):
        def run():
            for line in iter(pipe.readline, b""):
                text = line.decode("utf-8", errors="replace")
                if name == "stderr":
                    self.stderr_tail.append(text)
                # Resolve the stream at write time (e.g. ipykernel)
                stream = getattr(sys, name)
                try:
                    stream.write(text)
                    stream.flush()
                except Exception:
                    pass
            pipe.close()
        thread = threading.Thread(
            target=run, daemon=True, name=f"matlab_runtime-{name}"
        )
        thread.start()
        return thread

    def _accept(self, listener):
        # Listener.accept() cannot time out: run it in a thread and
        # watch the worker meanwhile.
        result = {}

        def run():
            while True:
                try:
                    result["conn"] = listener.accept()
                    return
                except (AuthenticationError, EOFError, ConnectionError):
                    continue    # not our worker
                except BaseException as e:
                    result["error"] = e
                    return

        thread = threading.Thread(
            target=run, daemon=True, name="matlab_runtime-accept"
        )
        thread.start()
        deadline = time.monotonic() + self.start_timeout
        while thread.is_alive():
            thread.join(0.1)
            if self.proc.poll() is not None:
                thread.join(0.5)
                if "conn" in result:
                    break
                raise self._died("failed to start")
            if time.monotonic() > deadline:
                # The accept thread stays blocked; it is a daemon thread
                # and its socket is removed with the session directory.
                raise TimeoutError(
                    f"MATLAB runtime worker did not start within "
                    f"{self.start_timeout} s."
                )
        if "error" in result:
            raise result["error"]
        return result["conn"]

    def _died(self, what="died"):
        self.alive = False
        rc = self.proc.poll() if self.proc else None
        if rc is None and self.proc:
            try:
                rc = self.proc.wait(1)
            except subprocess.TimeoutExpired:
                pass
        for thread in self.pumps:
            thread.join(1)
        tail = "".join(self.stderr_tail).rstrip()
        msg = f"MATLAB runtime worker {what} (exit code {rc})"
        if tail:
            msg += "\n" + tail
        return WorkerDiedError(msg)

    # --- requests -----------------------------------------------------

    def release(self, oid):
        if self.alive:
            self.released.append(oid)

    def _send(self, msg):
        data = protocol.dumps(msg)
        try:
            self.conn.send_bytes(data)
        except (OSError, EOFError):
            self._cleanup()
            raise self._died()
        except BaseException:
            # Interrupted in the middle of a message: the stream is lost
            self.kill()
            raise

    def _recv(self, rid, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self.conn.poll(0.2):
                if self.proc.poll() is not None:
                    self._cleanup()
                    raise self._died()
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError(f"No reply after {timeout} s.")
                continue
            try:
                data = self.conn.recv_bytes()
            except (OSError, EOFError):
                self._cleanup()
                raise self._died()
            except BaseException:
                self.kill()
                raise
            try:
                reply = protocol.loads(data)
            except ModuleNotFoundError as e:
                if e.name != "numpy":
                    raise
                raise ImportError(
                    "numpy is required to receive arrays from the MATLAB "
                    "runtime worker."
                ) from e
            self._process(reply, stale=reply.get("id") != rid)
            if reply.get("id") == rid:
                return reply

    def _process(self, reply, stale):
        """Free/attach shared memory; release objects of stale replies."""
        out = self.shm_out.pop(reply.get("id"), None)
        if out is not None:
            out.release()
        if not reply.get("ok"):
            return
        try:
            if stale:
                def leaf(x):
                    if isinstance(x, RemoteRef):
                        self.released.append(x.oid)
                walk(reply["result"], leaf)
            else:
                def leaf(x):
                    if isinstance(x, ShmArray):
                        return SharedArrays.attach(x)
                    if isinstance(x, RemoteRef):
                        return RemoteHandle(self, x)
                    return x
                reply["result"] = walk(reply["result"], leaf)
        finally:
            if reply.get("shm"):
                self._send({"id": None, "op": "ack", "release": []})

    def _exchange(self, op, shm=None, timeout=None, **fields):
        with self.lock:
            if not self.alive:
                raise WorkerDiedError(
                    "MATLAB runtime worker is not running. "
                    "Call matlab_runtime.remote.shutdown() and init() "
                    "to restart it."
                )
            rid = next(self.ids)
            released = []
            while self.released:
                released.append(self.released.popleft())
            msg = {"id": rid, "op": op, "release": released, **fields}
            if shm is not None and shm.segments:
                self.shm_out[rid] = shm
            try:
                self._send(msg)
            except BaseException:
                self.shm_out.pop(rid, None)
                if shm is not None:
                    shm.release()
                raise
            return self._recv(rid, timeout)

    @staticmethod
    def _result(reply):
        if not reply["ok"]:
            protocol.raise_remote(reply["error"])
        return reply["result"]

    def request(self, op, **fields):
        return self._result(self._exchange(op, **fields))

    def call(self, ref, name, args, kwargs):
        timeout = kwargs.pop("_timeout", None)
        streams = {}
        for key in ("stdout", "stderr"):
            if hasattr(kwargs.get(key), "write"):
                streams[key] = kwargs[key]
                kwargs[key] = StreamSink(key)
        shm = SharedArrays(self.shm_threshold)

        def leaf(x):
            if isinstance(x, RemoteHandle):
                if x._session is not self:
                    raise ValueError(f"{x!r} belongs to a closed session")
                return x._ref
            if is_ndarray(x):
                return shm.export(x)
            return x

        try:
            payload = protocol.dumps(walk((list(args), kwargs), leaf))
        except BaseException:
            shm.release()
            raise
        reply = self._exchange(
            "call", shm, timeout, ref=ref, name=name, args=payload
        )
        for key, stream in streams.items():
            stream.write(reply.get(key, ""))
        return self._result(reply)

    # --- shutdown -----------------------------------------------------

    def close(self, timeout=10):
        """Ask the worker to exit, then make sure it does."""
        if self.closed:
            return
        if self.alive and self.lock.acquire(timeout=timeout):
            try:
                self._send({"id": None, "op": "shutdown", "release": []})
                self.conn.poll(timeout)
            except BaseException:
                pass
            finally:
                self.lock.release()
        self.kill(timeout)

    def kill(self, timeout=0):
        """Stop the worker (after `timeout` seconds) and free resources."""
        self.alive = False
        self.closed = True
        if self.proc is not None:
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        for thread in self.pumps:
            thread.join(1)
        self._cleanup()

    def _cleanup(self):
        self.alive = False
        if self.conn is not None:
            self.conn.close()
        for shm in self.shm_out.values():
            shm.release()
        self.shm_out.clear()
        self.released.clear()
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None
