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

_WORKER = None


class Backend:
    """What the worker needs from the MATLAB SDK."""

    def init(self, **kwargs):
        raise NotImplementedError

    def import_package(self, name, file, option_list=()):
        raise NotImplementedError


class SdkBackend(Backend):
    """Backend that uses the real MATLAB SDK through `matlab_runtime.impl`.

    The runtime is terminated by `impl`'s own atexit handler when the
    worker exits.
    """

    def init(self, **kwargs):
        from .. import impl
        impl.init(**kwargs)

    def import_package(self, name, file, option_list=()):
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
    spec = os.environ.get(protocol.ENV_BACKEND, "")
    if not spec:
        return SdkBackend()
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)()


class Worker:

    def __init__(self, conn, backend):
        self.conn = conn
        self.backend = backend
        self.objects = {}
        self.next_oid = 1
        self.shm_threshold = 0
        # Segments of the last reply, released when the client answers
        self.pending = SharedArrays(0, track=False)

    # --- conversion ---------------------------------------------------

    def _store(self, obj):
        oid, self.next_oid = self.next_oid, self.next_oid + 1
        self.objects[oid] = obj
        cls = type(obj)
        return RemoteRef(oid, f"{cls.__module__}.{cls.__qualname__}")

    def _lookup(self, ref):
        try:
            return self.objects[ref.oid]
        except KeyError:
            raise ValueError(f"Remote object #{ref.oid} has been released")

    def from_wire(self, obj):
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
        self.backend.init(**msg["kwargs"])

    def op_import(self, msg):
        handle = self.backend.import_package(
            msg["name"], msg["file"], msg["option_list"]
        )
        return self._store(handle)

    def op_getattr(self, msg):
        value = getattr(self._lookup(msg["ref"]), msg["name"])
        if callable(value):
            return {"method": msg["name"]}
        return {"value": value}

    def op_call(self, msg):
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
        return dir(self._lookup(msg["ref"]))

    def op_ping(self, msg):
        return len(self.objects)

    def op_release(self, msg):
        pass

    # --- loop ---------------------------------------------------------

    def handle(self, msg):
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
        self.conn.send_bytes(data)
        self.pending = shm

    def serve(self):
        while True:
            try:
                msg = protocol.recv(self.conn)
            except (EOFError, OSError):
                break
            # Any message from the client means that it is done with
            # the shared memory of our previous reply.
            self.pending.release()
            for oid in msg.get("release", ()):
                self.objects.pop(oid, None)
            if msg["op"] == "ack":
                continue
            if msg["op"] == "shutdown":
                protocol.send(self.conn, {"id": msg["id"], "ok": True})
                break
            self.handle(msg)
        self.pending.release()
        self.conn.close()


def main(argv=None):
    # Extra arguments (e.g. `-mwpythonver X.Y` appended by mwpython)
    # are ignored.
    try:
        # The client handles Ctrl-C; never interrupt a MATLAB call.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass  # not on the main thread (mwpython)
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    untrack_shared_memory()

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
