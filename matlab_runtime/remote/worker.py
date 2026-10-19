"""
Worker process of the out-of-process MATLAB runtime.

Started by `matlab_runtime.remote` with
`python -m matlab_runtime.remote.worker` (through `mwpython` on macOS).
It connects back to the client, owns the MATLAB SDK, and serves one
request at a time. Objects that cannot be sent back (deployed packages,
MATLAB objects, ...) are kept in a table and referred to by id.
"""
import importlib
import importlib.util
import io
import os
import signal
import sys
from multiprocessing.connection import Client

from . import protocol
from .convert import (
    PLAIN_TYPES,
    SharedArrays,
    is_matlab_array,
    is_ndarray,
    is_numpy_scalar,
    matlab_to_numpy,
    numpy_to_matlab,
    untrack_shared_memory,
    walk,
)
from .protocol import RemoteRef, ShmArray, StreamSink
from ..utils import MODE_ENV

_WORKER = None


def _type_name(obj):
    """
    Qualified name of the type of `obj`, sent with its `RemoteRef`.

    Instances of `matlab.object` (MATLAB objects and function handles)
    are always named "matlab.object" (see `remote.is_matlab_object`).
    """
    matlab = sys.modules.get("matlab")
    matlab_object = getattr(matlab, "object", None)
    if isinstance(matlab_object, type) and isinstance(obj, matlab_object):
        return "matlab.object"
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


class Backend:
    """
    What the worker needs from the MATLAB SDK.

    Subclass it to replace the SDK (e.g., in tests) and select the
    subclass with the environment variable
    `MATLAB_RUNTIME_WORKER_BACKEND` (`module:Class`, see
    `load_backend`).
    """

    def init(self, **kwargs):
        """
        Initialize the SDK.

        Parameters
        ----------
        **kwargs
            Keyword arguments of the `init` request, i.e. `version`
            and `prefix` (see `matlab_runtime.remote.init`).
        """
        raise NotImplementedError

    def init_runtime(self, option_list=(), error_if_already_init=False):
        """
        Initialize the MATLAB runtime (see `matlab_runtime.init_runtime`).

        Parameters
        ----------
        option_list : list[str]
            Options passed to MATLAB.
        error_if_already_init : bool
            Raise a ValueError if the runtime is already initialized.
        """
        raise NotImplementedError

    def import_package(self, name, file, option_list=()):
        """
        Import a compiled MATLAB package.

        Parameters
        ----------
        name : str
            Name of the python package.
        file : str | None
            Path to its `__init__.py`, used if `name` cannot be
            imported by the worker.
        option_list : list[str]
            Options passed to MATLAB when the runtime is initialized.

        Returns
        -------
        object
            The imported package (kept in the worker and returned to
            the client as a `RemoteHandle`).
        """
        raise NotImplementedError


class SdkBackend(Backend):
    """Backend that uses the real MATLAB SDK through `matlab_runtime.impl`.

    The runtime is terminated by `impl`'s own atexit handler when the
    worker exits.
    """

    def init(self, **kwargs):
        """Initialize the SDK with `matlab_runtime.impl.init`."""
        from .. import impl
        impl.init(**kwargs)

    def init_runtime(self, option_list=(), error_if_already_init=False):
        """Initialize the runtime with `matlab_runtime.impl.init_runtime`."""
        from .. import impl
        impl.init_runtime(tuple(option_list), error_if_already_init)

    def import_package(self, name, file, option_list=()):
        """
        Import a package and initialize it with `impl.import_deployed`.

        If `name` cannot be imported, the package is loaded from `file`
        (and registered in `sys.modules`). The ImportError is raised
        again if `file` is empty.
        """
        from .. import impl
        try:
            module = importlib.import_module(name)
        except ImportError:
            if not file:
                raise
            locations = None
            if os.path.basename(file) == "__init__.py":
                locations = [os.path.dirname(file)]
            spec = importlib.util.spec_from_file_location(
                name, file, submodule_search_locations=locations
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return impl.import_deployed(module, option_list=tuple(option_list))


def load_backend():
    """
    Instantiate the backend selected by the environment.

    Returns
    -------
    Backend
        An instance of the class named by the environment variable
        `MATLAB_RUNTIME_WORKER_BACKEND` (`"module:Class"`), or
        `SdkBackend` if it is unset or empty.
    """
    spec = os.environ.get(protocol.ENV_BACKEND, "")
    if not spec:
        return SdkBackend()
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)()


class Worker:
    """
    Serves the requests of the client, one at a time.

    Objects that cannot be sent back (anything but plain values and
    arrays) are kept in a table and replaced by a `RemoteRef`; the
    client frees them through the `release` field of its requests.
    Requests are dictionaries with at least the fields `id` and `op`;
    an `op_<op>` method handles each operation (`msg` is the request,
    the return value is the `result` of the reply). Exceptions are
    sent back to the client. Shared memory segments of a reply are kept
    until the client acknowledges that reply (`ack` message with the
    reply's id), and all of them are freed when the worker stops.
    """

    def __init__(self, conn, backend):
        """
        Parameters
        ----------
        conn : multiprocessing.connection.Connection
            Connection to the client.
        backend : Backend
            Interface to the MATLAB SDK.
        """
        self.conn = conn
        self.backend = backend
        self.objects = {}
        self.next_oid = 1
        self.shm_threshold = 0
        # Segments of the replies not yet acknowledged, by reply id
        self.pending = {}

    # --- conversion ---------------------------------------------------

    def _store(self, obj):
        oid, self.next_oid = self.next_oid, self.next_oid + 1
        self.objects[oid] = obj
        return RemoteRef(oid, _type_name(obj))

    def _lookup(self, ref):
        try:
            return self.objects[ref.oid]
        except KeyError:
            raise ValueError(f"Remote object #{ref.oid} has been released")

    def from_wire(self, obj):
        """
        Convert the arguments of a request.

        `RemoteRef` become the objects they refer to, numpy arrays
        (also those in shared memory) become `matlab.*` arrays.
        """
        def leaf(x):
            if isinstance(x, ShmArray):
                x = SharedArrays.attach(x)
            if isinstance(x, RemoteRef):
                return self._lookup(x)
            if is_ndarray(x):
                return numpy_to_matlab(x)
            return x
        return walk(obj, leaf)

    def to_wire(self, obj, shm):
        """
        Convert a result to send it to the client.

        `matlab.*` arrays become numpy arrays, which are exported
        through `shm` (a `SharedArrays`) if large. Plain values pass
        through, anything else is stored and replaced by a `RemoteRef`.
        """
        def leaf(x):
            if is_matlab_array(x):
                x = matlab_to_numpy(x)
            if is_ndarray(x):
                return shm.export(x)
            if type(x) in PLAIN_TYPES or is_numpy_scalar(x):
                return x
            return self._store(x)
        return walk(obj, leaf)

    # --- ops ----------------------------------------------------------

    def op_hello(self, msg):
        """
        Handshake. Fields: `protocol`, `sys_path`, `shm_threshold`.

        Checks the protocol version, appends the client's `sys.path`
        entries to ours, and sets the shared memory threshold.
        Returns `{"pid", "python"}` of the worker.

        Raises
        ------
        RuntimeError
            On protocol mismatch.
        """
        if msg["protocol"] != protocol.PROTOCOL_VERSION:
            raise RuntimeError(
                f"Protocol mismatch: client {msg['protocol']}, "
                f"worker {protocol.PROTOCOL_VERSION}"
            )
        for path in msg["sys_path"]:
            if path not in sys.path:
                sys.path.append(path)
        self.shm_threshold = msg["shm_threshold"]
        return {"pid": os.getpid(), "python": sys.version}

    def op_init(self, msg):
        """Initialize the SDK. Field: `kwargs` (`version`, `prefix`)."""
        self.backend.init(**msg["kwargs"])

    def op_init_runtime(self, msg):
        """
        Initialize the runtime. Fields: `option_list`,
        `error_if_already_init`.
        """
        self.backend.init_runtime(
            msg["option_list"], msg["error_if_already_init"]
        )

    def op_import(self, msg):
        """
        Import a compiled package. Fields: `name`, `file`, `option_list`.

        Returns a `RemoteRef` to the imported module.
        """
        handle = self.backend.import_package(
            msg["name"], msg["file"], msg["option_list"]
        )
        return self._store(handle)

    def op_getattr(self, msg):
        """
        Get an attribute. Fields: `ref`, `name`.

        Returns `{"method": name}` if the attribute is callable, else
        `{"value": value}` (converted by `to_wire`).
        """
        value = getattr(self._lookup(msg["ref"]), msg["name"])
        if callable(value):
            return {"method": msg["name"]}
        return {"value": value}

    def op_call(self, msg):
        """
        Call an object or a method. Fields: `ref`, `name`, `args`.

        `name` is None to call the object itself. `args` is the pickled
        `(args, kwargs)`. `StreamSink` keyword arguments are replaced
        by string buffers whose content is sent back in the reply, as
        `stdout` / `stderr`. Returns the result of the call.
        """
        obj = self._lookup(msg["ref"])
        func = obj if msg["name"] is None else getattr(obj, msg["name"])
        args, kwargs = self.from_wire(protocol.loads(msg["args"]))
        streams = {}
        for key, value in kwargs.items():
            if isinstance(value, StreamSink):
                kwargs[key] = streams[key] = io.StringIO()
        try:
            return func(*args, **kwargs)
        finally:
            self.streams = {k: v.getvalue() for k, v in streams.items()}

    def op_dir(self, msg):
        """List the attributes of an object. Field: `ref`."""
        return dir(self._lookup(msg["ref"]))

    def op_ping(self, msg):
        """Return the number of objects held (for tests/diagnostics)."""
        return len(self.objects)

    def op_release(self, msg):
        """Do nothing: objects are released by `serve` for any op."""
        pass

    def release_pending(self, rid):
        """
        Free the shared memory of the reply `rid` (if any).

        An acknowledgment only ever frees the segments of the reply that
        it names; unknown ids are ignored.
        """
        shm = self.pending.pop(rid, None)
        if shm is not None:
            shm.release()

    def release_all_pending(self):
        """Free the shared memory of all the replies."""
        for rid in list(self.pending):
            self.release_pending(rid)

    # --- loop ---------------------------------------------------------

    def handle(self, msg):
        """
        Execute a request and send the reply to the client.

        The reply is a dictionary with the fields `id`, `ok`, then
        `result` and `shm` (names of the shared memory segments to
        free, see `ack`) or `error` (see `protocol.encode_exception`),
        and the captured `stdout` / `stderr`, if any.
        """
        self.streams = {}
        shm = SharedArrays(self.shm_threshold, track=False)
        try:
            result = getattr(self, "op_" + msg["op"])(msg)
            if msg["op"] == "getattr" and "value" in result:
                result["value"] = self.to_wire(result["value"], shm)
            elif msg["op"] == "call":
                result = self.to_wire(result, shm)
            reply = {"id": msg["id"], "ok": True, "result": result}
            reply["shm"] = shm.names
        except Exception as e:
            shm.release()
            reply = {"id": msg["id"], "ok": False}
            reply["error"] = protocol.encode_exception(e)
        reply.update(self.streams)
        try:
            data = protocol.dumps(reply)
        except Exception as e:
            shm.release()
            data = protocol.dumps({
                "id": msg["id"],
                "ok": False,
                "error": protocol.encode_exception(e),
            })
        if shm.segments:
            # Registered before sending, so that they are freed when
            # the worker stops even if the reply cannot be sent.
            self.pending[msg["id"]] = shm
        self.conn.send_bytes(data)

    def serve(self):
        """
        Receive and handle requests until the client goes away.

        Every message first frees the objects listed in its `release`
        field. An `ack` message (field `ack`: id of a reply) frees the
        shared memory of that reply and needs no reply; `shutdown` is
        answered, then stops the loop, as does a closed connection.
        All the shared memory still held is freed on the way out
        (including on `SystemExit`, see `main`).
        """
        try:
            while True:
                try:
                    msg = protocol.recv(self.conn)
                except (EOFError, OSError):
                    break
                for oid in msg.get("release", ()):
                    self.objects.pop(oid, None)
                if msg["op"] == "ack":
                    self.release_pending(msg["ack"])
                    continue
                try:
                    if msg["op"] == "shutdown":
                        protocol.send(
                            self.conn, {"id": msg["id"], "ok": True}
                        )
                        break
                    self.handle(msg)
                except (EOFError, OSError):
                    break   # the client is gone
        finally:
            self.release_all_pending()
            self.conn.close()


def _terminate(signum, frame):
    sys.exit(128 + signum)


def main(argv=None):
    """
    Entry point of the worker process.

    Connects to the client at the address given by the environment
    variables `MATLAB_RUNTIME_WORKER_ADDR` and
    `MATLAB_RUNTIME_WORKER_KEY` (both removed from the environment),
    then serves requests until the client shuts it down.

    Parameters
    ----------
    argv : list[str], optional
        Ignored.

    Returns
    -------
    int
        Exit code (0).
    """
    # Extra arguments (e.g. `-mwpythonver X.Y` appended by mwpython)
    # are ignored.
    try:
        # The client handles Ctrl-C; never interrupt a MATLAB call.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # Exit through `serve`'s cleanup (shared memory) when the
        # client terminates us.
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):
        pass  # not on the main thread (mwpython)
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    untrack_shared_memory()
    # `matlab_runtime.init` / `import_deployed` run in-process here,
    # also in code that does not go through the backend.
    os.environ[MODE_ENV] = "inprocess"

    address = os.environ.pop(protocol.ENV_ADDR)
    authkey = bytes.fromhex(os.environ.pop(protocol.ENV_KEY))
    conn = Client(address, authkey=authkey)
    global _WORKER
    # Keep the object table alive until exit, so that impl's atexit
    # handler terminates the deployed packages as in-process.
    _WORKER = Worker(conn, load_backend())
    _WORKER.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
