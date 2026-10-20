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
from .convert import SharedArrays, is_ndarray, unlink_owned, walk
from ..utils import MODE_ENV
from .protocol import (
    RemoteRef,
    ShmArray,
    StreamSink,
    WorkerDiedError,
)

WORKER_MODULE = "matlab_runtime.remote.worker"
DEFAULT_SHM_THRESHOLD = 1 << 20
SITE_DIRS = ("site-packages", "dist-packages")


def _uses_mpython(executable=None, use_mpython=None):
    """Whether the worker is started through `mwpython` (see below)."""
    if use_mpython is None:
        from ..utils import guess_arch
        use_mpython = executable is None and guess_arch()[:3] == "mac"
    return bool(use_mpython)


def _worker_argv_env(executable=None, use_mpython=None,
                     variant="latest_installed", prefix=None):
    """
    Command and environment used to start the worker.

    On macOS, the worker is started through `mwpython` (with the same
    environment fixes as `mpython`), unless an `executable` is given.

    Parameters
    ----------
    executable : str, optional
        Python interpreter that runs the worker.
        Default: `sys.executable`.
    use_mpython : bool, optional
        Go through `mwpython`. Default: True on macOS unless
        `executable` is given, else False.
    variant : str
        MATLAB runtime version for `mwpython`.
    prefix : str, optional
        Installation prefix (or release folder) of the runtime used by
        `mwpython`, searched first as by `matlab_runtime.init`, so that
        `mwpython` comes from the runtime that the worker loads.

    Returns
    -------
    argv : list[str]
        Command to run.
    env : dict[str, str]
        Environment to run it in: a copy of `os.environ`, with
        `MATLAB_RUNTIME_MODE=inprocess`. When
        `matlab_runtime` is not installed in a `site-packages`
        directory (source checkout), its parent directory is prepended
        to `PYTHONPATH` so that the worker imports this very copy.
    """
    module = ["-m", WORKER_MODULE]
    if _uses_mpython(executable, use_mpython):
        from ..cli import _mpython_command
        argv, env = _mpython_command(module, variant, prefix=prefix)
    else:
        env = dict(os.environ)
        root = op.dirname(op.dirname(op.dirname(op.abspath(__file__))))
        if executable is None and op.basename(root) not in SITE_DIRS:
            # matlab_runtime is not installed (e.g., source checkout):
            # make sure that the worker imports this very copy.
            path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                [root] + ([path] if path else [])
            )
        if executable:
            argv = [executable, *module]
        else:
            from ..utils import python_argv
            argv = python_argv(*module)
    # The worker runs the MATLAB SDK in-process: it must never start a
    # worker of its own (see `matlab_runtime.init(mode=...)`).
    env[MODE_ENV] = "inprocess"
    return argv, env


class RemoteHandle:
    """
    Proxy to an object that lives in the worker.

    Public attributes are fetched from the worker on access: values are
    converted back to python objects and methods are returned as
    `RemoteMethod`. Calling the handle calls the remote object.
    Names starting with an underscore are never forwarded. The remote
    object is released (with the next request) when the handle is
    garbage collected. Handles cannot be pickled or copied.
    """

    def __init__(self, session, ref):
        """
        Parameters
        ----------
        session : Session
            Session that owns the remote object.
        ref : RemoteRef
            Reference to the remote object.
        """
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
    """
    Callable attribute of a `RemoteHandle`.

    Calling it calls the method of the same name on the remote object.
    Keyword arguments `stdout` and `stderr`, if file-like objects, get
    the output of the call, and `_timeout` (float) bounds the time (in
    seconds) to wait for the reply (`TimeoutError`).
    """

    def __init__(self, handle, name):
        """
        Parameters
        ----------
        handle : RemoteHandle
            Handle of the object that owns the method.
        name : str
            Name of the method.
        """
        self._handle = handle
        self._name = name

    def __call__(self, *args, **kwargs):
        handle = self._handle
        return handle._session.call(handle._ref, self._name, args, kwargs)

    def __repr__(self):
        return f"<RemoteMethod {self._name} of {self._handle!r}>"


class Session:
    """
    Connection to one worker process.

    Lifecycle: `start()` spawns the worker, accepts its connection and
    performs the handshake; `close()` or `kill()` end it. A session is
    single use: once closed or dead, a new `Session` must be created.

    Threading: it is safe to share a session between threads. A lock
    allows a single request in flight, and the others wait their turn.
    Replies that no longer match the current request (e.g. after a
    `TimeoutError`) are discarded, and the remote objects they carry
    are released. Releasing a `RemoteHandle` never talks to the worker:
    object ids are queued and piggyback on the next request. Shared
    memory segments sent to the worker (including those passed by
    reference) are kept until it replies. Segments in its reply are
    taken over: arrays are returned as views of them, and they are
    unlinked when the last view is gone (or when the session ends).
    The reply is acknowledged (`ack`, with its id and the names of the
    segments taken over) so that the worker unlinks the others, e.g.
    those of a stale reply.
    """

    def __init__(self, executable=None, use_mpython=None,
                 variant="latest_installed", start_timeout=600,
                 shm_threshold=None, prefix=None):
        """
        Create a session. The worker is not started until `start()`.

        Parameters
        ----------
        executable : str, optional
            Python interpreter used to run the worker.
            Default: this interpreter (through `mwpython` on macOS).
        use_mpython : bool, optional
            Start the worker through `mwpython`. Default: True on
            macOS unless `executable` is given.
        variant : str, default="latest_installed"
            MATLAB runtime version used by `mwpython`.
        start_timeout : float, default=600
            Maximum time (in seconds) given to the worker to connect.
        shm_threshold : int, optional
            Arrays of at least this many bytes are exchanged through
            shared memory. 0 disables shared memory. Default:
            environment variable `MATLAB_RUNTIME_SHM_THRESHOLD`, else
            1 MiB (`DEFAULT_SHM_THRESHOLD`).
        prefix : str, optional
            Installation prefix of the runtime used by `mwpython`.

        Attributes
        ----------
        alive : bool
            Whether the worker is believed to be running.
        closed : bool
            Whether `close` or `kill` has been called.
        stderr_tail : collections.deque[str]
            Last lines written by the worker on stderr, reported in
            `WorkerDiedError` messages.
        """
        if shm_threshold is None:
            shm_threshold = int(os.environ.get(
                protocol.ENV_SHM_THRESHOLD, DEFAULT_SHM_THRESHOLD
            ))
        self.executable = executable
        self.use_mpython = use_mpython
        self.variant = variant
        self.prefix = prefix
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
        """
        Spawn the worker, wait for it to connect, and handshake.

        Listens on a private unix socket (a named pipe on Windows)
        protected by a random key, passed to the worker through the
        `MATLAB_RUNTIME_WORKER_ADDR` and `MATLAB_RUNTIME_WORKER_KEY`
        environment variables. The worker's stdout and stderr are
        forwarded to the current `sys.stdout` and `sys.stderr`. It runs
        in its own session/process group, away from Ctrl-C.

        Raises
        ------
        WorkerDiedError
            If the worker exits before connecting.
        TimeoutError
            If it does not connect within `start_timeout` seconds.
        RuntimeError
            If the handshake fails (e.g., protocol mismatch).

        On any failure, the worker is killed and resources are freed.
        """
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
                self.executable, self.use_mpython, self.variant, self.prefix
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
        """
        Schedule the release of a remote object.

        Never blocks or talks to the worker (it is called from
        `__del__`): the id is sent along with the next request.

        Parameters
        ----------
        oid : int
            Id of the object (`RemoteRef.oid`). Ignored if the worker
            is not alive.
        """
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
            ready = False
            try:
                ready = self.conn.poll(0.2)
                data = self.conn.recv_bytes() if ready else None
            except (OSError, EOFError):
                self._cleanup()
                raise self._died()
            except BaseException:
                if ready:
                    # Interrupted in the middle of a message
                    self.kill()
                raise
            if not ready:
                if self.proc.poll() is not None:
                    self._cleanup()
                    raise self._died()
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError(f"No reply after {timeout} s.")
                continue
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
        """
        Free/attach shared memory; release objects of stale replies.

        The shared memory of the reply is acknowledged (with the reply's
        own id, so that the worker frees exactly these segments) even if
        the reply is stale or its conversion is interrupted, listing the
        segments taken over (the worker unlinks the others). Remote
        objects of a reply that is not returned are queued for release.
        """
        rid = reply.get("id")
        taken = []
        out = self.shm_out.pop(rid, None)
        if out is not None:
            out.release()
        try:
            if not reply.get("ok"):
                return
            result = reply["result"]

            def release(x):
                if isinstance(x, RemoteRef):
                    self.released.append(x.oid)
            if stale:
                walk(result, release)
                return

            def leaf(x):
                if isinstance(x, ShmArray):
                    array = SharedArrays.attach(x)
                    taken.append(x.name)
                    return array
                if isinstance(x, RemoteRef):
                    return RemoteHandle(self, x)
                return x
            try:
                reply["result"] = walk(result, leaf)
            except BaseException:
                # Handles already built release their object again when
                # collected, which the worker ignores.
                walk(result, release)
                raise
        finally:
            if reply.get("shm"):
                self._send({"id": None, "op": "ack", "ack": rid,
                            "keep": taken, "release": []})

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
            if shm is not None and (shm.segments or shm.borrowed):
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
        """
        Send a request to the worker and return its result.

        Waits for the lock if another request is in flight. Remote
        objects in the result are converted to `RemoteHandle`.

        Parameters
        ----------
        op : str
            Operation: `hello`, `init`, `import`, `getattr`, `dir`,
            `ping`, ... (see `matlab_runtime.remote.worker.Worker`).
        **fields
            Fields of the request message.

        Returns
        -------
        object
            The `result` field of the reply.

        Raises
        ------
        WorkerDiedError
            If the worker is not running, or dies.
        Exception
            The exception raised by the worker (see
            `protocol.raise_remote`).
        """
        return self._result(self._exchange(op, **fields))

    def call(self, ref, name, args, kwargs):
        """
        Call a remote object, or one of its methods.

        Numpy arrays in the arguments are exported (through shared
        memory if large; views of the shared memory of earlier results
        are passed by reference), `RemoteHandle` objects are replaced by their
        reference. File-like `stdout`/`stderr` keyword arguments
        receive the output printed by MATLAB during the call.

        Parameters
        ----------
        ref : RemoteRef
            The remote object.
        name : str | None
            Name of the method, or None to call the object itself.
        args : tuple
            Positional arguments.
        kwargs : dict
            Keyword arguments. `_timeout` (float, in seconds) is
            removed from it and bounds the wait for the reply.

        Returns
        -------
        object
            The result. Remote objects are returned as `RemoteHandle`.

        Raises
        ------
        TimeoutError
            If there is no reply within `_timeout`. The worker keeps
            running the call; its reply will be discarded.
        ValueError
            If an argument is a handle from another session.
        WorkerDiedError
            If the worker is not running, or dies.
        Exception
            The exception raised by the worker.
        """
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
        """
        Ask the worker to exit, then make sure it does.

        Waits up to `timeout` seconds for a pending request to finish
        and for the worker to acknowledge, then calls `kill`. Does
        nothing if the session is already closed.

        Parameters
        ----------
        timeout : float, default=10
            Seconds given to the worker to exit gracefully.
        """
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
        """
        Stop the worker (after `timeout` seconds) and free resources.

        The process is given `timeout` seconds to exit by itself, then
        terminated, then killed, together with the processes that it
        started (its process group on POSIX, its process tree on
        Windows). Output pumps are joined, the connection is closed,
        shared memory is released (the segments of the results are
        unlinked, see `convert.unlink_owned`) and the socket directory
        is removed.
        Safe to call several times.

        Parameters
        ----------
        timeout : float, default=0
            Seconds to wait for the worker to exit before terminating
            it.
        """
        self.alive = False
        self.closed = True
        if self.proc is not None:
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self._signal(False)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._signal(True)
                    self.proc.wait()
        for thread in self.pumps:
            thread.join(1)
        if sys.platform != "win32" and any(t.is_alive() for t in self.pumps):
            # The worker is gone but processes of its group still hold
            # our pipes (which also keeps its process group id taken).
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            for thread in self.pumps:
                thread.join(1)
        self._cleanup()

    def _signal(self, force):
        # Terminate (or kill) the worker and the processes it started
        # (e.g. MATLAB helpers), which would otherwise keep our pipes.
        proc = self.proc
        if sys.platform == "win32":
            # taskkill needs the parent to be alive to find the tree
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            import signal
            # The worker leads its own session (start_new_session)
            try:
                os.killpg(proc.pid, signal.SIGKILL if force
                          else signal.SIGTERM)
                return
            except OSError:
                pass
        if proc.poll() is None:
            if force:
                proc.kill()
            else:
                proc.terminate()

    def _cleanup(self):
        self.alive = False
        if self.conn is not None:
            self.conn.close()
        for shm in self.shm_out.values():
            shm.release()
        self.shm_out.clear()
        unlink_owned()
        self.released.clear()
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None
