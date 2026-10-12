"""
Wire protocol shared by the client and the worker.

Messages are dictionaries pickled with protocol 5 and exchanged over an
authenticated `multiprocessing.connection` channel. Only plain python
objects, numpy arrays and the small classes below cross the wire.
"""
import builtins
import pickle
import traceback

PROTOCOL_VERSION = 1

ENV_ADDR = "MATLAB_RUNTIME_WORKER_ADDR"
ENV_KEY = "MATLAB_RUNTIME_WORKER_KEY"
ENV_BACKEND = "MATLAB_RUNTIME_WORKER_BACKEND"
ENV_SHM_THRESHOLD = "MATLAB_RUNTIME_SHM_THRESHOLD"


# ----------------------------------------------------------------------
#   EXCEPTIONS
# ----------------------------------------------------------------------


class RemoteError(RuntimeError):
    """An exception raised in the worker whose type is not a builtin."""

    def __init__(self, message, type=None):
        super().__init__(message)
        self.type = type


class RemoteTraceback(Exception):
    """Holds the formatted traceback of an exception raised in the worker."""

    def __str__(self):
        return "\n" + self.args[0]


class WorkerDiedError(RuntimeError):
    """The worker process exited or the connection to it was lost."""


# ----------------------------------------------------------------------
#   WIRE OBJECTS
# ----------------------------------------------------------------------


class RemoteRef:
    """Reference to an object that lives in the worker's object table."""

    __slots__ = ("oid", "type")

    def __init__(self, oid, type=""):
        self.oid = oid
        self.type = type

    def __reduce__(self):
        return (RemoteRef, (self.oid, self.type))


class ShmArray:
    """Numpy array stored in a shared memory segment."""

    __slots__ = ("name", "dtype", "shape", "order")

    def __init__(self, name, dtype, shape, order="C"):
        self.name = name
        self.dtype = dtype
        self.shape = shape
        self.order = order

    def __reduce__(self):
        return (ShmArray, (self.name, self.dtype, self.shape, self.order))


class StreamSink:
    """Placeholder for a `stdout=`/`stderr=` keyword argument."""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __reduce__(self):
        return (StreamSink, (self.name,))


# ----------------------------------------------------------------------
#   FRAMING
# ----------------------------------------------------------------------


def dumps(obj):
    return pickle.dumps(obj, protocol=5)


def loads(data):
    return pickle.loads(data)


def send(conn, msg):
    conn.send_bytes(dumps(msg))


def recv(conn):
    return loads(conn.recv_bytes())


# ----------------------------------------------------------------------
#   ERRORS
# ----------------------------------------------------------------------


_PLAIN_ARGS = (str, int, float, bool, type(None))


def encode_exception(exc):
    """Convert an exception into a picklable dictionary."""
    args = exc.args
    if not all(isinstance(arg, _PLAIN_ARGS) for arg in args):
        args = (str(exc),)
    return {
        "type": type(exc).__qualname__,
        "module": type(exc).__module__,
        "message": str(exc),
        "args": args,
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }


def raise_remote(error):
    """Raise the client-side counterpart of an encoded worker exception."""
    exc = None
    if error["module"] == "builtins":
        cls = getattr(builtins, error["type"], None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            try:
                exc = cls(*error["args"])
            except Exception:
                exc = None
    if exc is None:
        name = f"{error['module']}.{error['type']}"
        exc = RemoteError(f"{name}: {error['message']}", name)
    raise exc from RemoteTraceback(error["traceback"])
