"""
Wire protocol shared by the client and the worker.

Messages are dictionaries pickled with protocol 5 and exchanged over an
authenticated `multiprocessing.connection` channel. Only plain python
objects, numpy arrays and the small classes below cross the wire.
"""
import builtins
import importlib
import pickle
import traceback

PROTOCOL_VERSION = 3

ENV_ADDR = "MATLAB_RUNTIME_WORKER_ADDR"
ENV_KEY = "MATLAB_RUNTIME_WORKER_KEY"
ENV_BACKEND = "MATLAB_RUNTIME_WORKER_BACKEND"
ENV_SHM_THRESHOLD = "MATLAB_RUNTIME_SHM_THRESHOLD"


# ----------------------------------------------------------------------
#   EXCEPTIONS
# ----------------------------------------------------------------------


class RemoteError(RuntimeError):
    """
    An exception raised in the worker whose type is not a builtin.

    Exceptions whose type is a builtin are re-raised as such in the
    client. Others (e.g., `matlab.engine` errors) are raised as this
    class, with the worker traceback as `__cause__`.
    """

    def __init__(self, message, type=None):
        """
        Parameters
        ----------
        message : str
            Message, prefixed with the qualified name of the exception.
        type : str, optional
            Qualified name (`module.Class`) of the original exception.
        """
        super().__init__(message)
        self.type = type


class RemoteTraceback(Exception):
    """
    Holds the formatted traceback of an exception raised in the worker.

    Used as the `__cause__` of the exception raised in the client. Its
    single argument is the traceback, as a string.
    """

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
        """
        Parameters
        ----------
        oid : int
            Key of the object in the worker's table.
        type : str
            Qualified name of the object's type (informative only).
        """
        self.oid = oid
        self.type = type

    def __reduce__(self):
        return (RemoteRef, (self.oid, self.type))


class ShmArray:
    """Numpy array stored in a shared memory segment."""

    __slots__ = ("name", "dtype", "shape", "order")

    def __init__(self, name, dtype, shape, order="C"):
        """
        Parameters
        ----------
        name : str
            Name of the shared memory segment.
        dtype : numpy.dtype
            Data type of the array.
        shape : tuple[int]
            Shape of the array.
        order : {"C", "F"}
            Memory layout of the data in the segment.
        """
        self.name = name
        self.dtype = dtype
        self.shape = shape
        self.order = order

    def __reduce__(self):
        return (ShmArray, (self.name, self.dtype, self.shape, self.order))


class StreamSink:
    """
    Placeholder for a `stdout=`/`stderr=` keyword argument.

    File-like objects cannot cross the wire: the client replaces them
    with a sink, the worker collects the output in a string, and sends
    it back in the reply for the client to write.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        """
        Parameters
        ----------
        name : {"stdout", "stderr"}
            Keyword argument that the sink replaces.
        """
        self.name = name

    def __reduce__(self):
        return (StreamSink, (self.name,))


# ----------------------------------------------------------------------
#   FRAMING
# ----------------------------------------------------------------------


def dumps(obj):
    """Serialize `obj` to `bytes` (pickle protocol 5)."""
    return pickle.dumps(obj, protocol=5)


def loads(data):
    """Deserialize `bytes` produced by `dumps`."""
    return pickle.loads(data)


def send(conn, msg):
    """Send the message `msg` (a dict) through connection `conn`."""
    conn.send_bytes(dumps(msg))


def recv(conn):
    """
    Receive a message from connection `conn`.

    Raises `EOFError` if the peer closed the connection.
    """
    return loads(conn.recv_bytes())


# ----------------------------------------------------------------------
#   ERRORS
# ----------------------------------------------------------------------


_PLAIN_ARGS = (str, int, float, bool, type(None))


def encode_exception(exc):
    """
    Convert an exception into a picklable dictionary.

    Parameters
    ----------
    exc : BaseException
        The exception, with its traceback.

    Returns
    -------
    error : dict
        Keys `type` (qualified name), `module`, `message`, `args`
        (replaced by `(str(exc),)` if they are not plain values) and
        `traceback` (formatted string).
    """
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


_OWN_MODULES = ("matlab_runtime.utils",)


def raise_remote(error):
    """
    Raise the client-side counterpart of an encoded worker exception.

    Parameters
    ----------
    error : dict
        Output of `encode_exception`.

    Raises
    ------
    BaseException
        An instance of the same exception class if it is a builtin or
        one of `matlab_runtime.utils`'s (and it can be built from the
        arguments), else `RemoteError`.
        The worker traceback is attached as a `RemoteTraceback` cause.
    """
    exc = None
    cls = None
    if error["module"] == "builtins":
        cls = getattr(builtins, error["type"], None)
    elif error["module"] in _OWN_MODULES:
        # Our own exceptions (e.g. `RuntimeNotFoundError`)
        module = importlib.import_module(error["module"])
        cls = getattr(module, error["type"], None)
    if cls is not None:
        if isinstance(cls, type) and issubclass(cls, BaseException):
            try:
                exc = cls(*error["args"])
            except Exception:
                exc = None
    if exc is None:
        name = f"{error['module']}.{error['type']}"
        exc = RemoteError(f"{name}: {error['message']}", name)
    raise exc from RemoteTraceback(error["traceback"])
