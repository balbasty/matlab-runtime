"""
Conversion of values to and from their wire representation.

* `walk` maps a function over the leaves of (nested) lists, tuples and
  dicts, which is how both sides convert arguments and results.
* `SharedArrays` moves large numpy arrays through shared memory.
* `matlab_to_numpy` / `numpy_to_matlab` are only used in the worker.

numpy is imported lazily so that the client does not depend on it.
"""
import os
import secrets
import sys

from .protocol import ShmArray

_MATLAB_TO_NUMPY = {
    "double": "float64",
    "single": "float32",
    "logical": "bool",
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "int64": "int64",
    "uint8": "uint8",
    "uint16": "uint16",
    "uint32": "uint32",
    "uint64": "uint64",
}

_NUMPY_TO_MATLAB = {
    "float64": "double",
    "float32": "single",
    "complex128": "double",
    "complex64": "single",
    "bool": "logical",
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "int64": "int64",
    "uint8": "uint8",
    "uint16": "uint16",
    "uint32": "uint32",
    "uint64": "uint64",
}

PLAIN_TYPES = (str, bytes, int, float, complex, bool, type(None))


def walk(obj, leaf):
    """
    Apply `leaf` to all non-container elements of `obj`.

    Parameters
    ----------
    obj : object
        Value, possibly nested in exact `list`, `tuple` and `dict`
        containers (subclasses are leaves).
    leaf : callable
        Function applied to each leaf. Its return values replace them.

    Returns
    -------
    object
        A copy of `obj` with the same structure and converted leaves.
    """
    if type(obj) is list:
        return [walk(x, leaf) for x in obj]
    if type(obj) is tuple:
        return tuple(walk(x, leaf) for x in obj)
    if type(obj) is dict:
        return {key: walk(value, leaf) for key, value in obj.items()}
    return leaf(obj)


def import_numpy(where="worker"):
    """
    Import and return numpy.

    Parameters
    ----------
    where : str
        Process named in the error message ("worker" or "client").

    Raises
    ------
    ImportError
        With an explicit message if numpy is not installed.
    """
    try:
        import numpy
    except ImportError as e:
        raise ImportError(
            f"numpy is required in the {where} to exchange MATLAB arrays."
        ) from e
    return numpy


def is_ndarray(obj):
    """Whether `obj` is a numpy array (False if numpy is not loaded)."""
    # Do not import numpy if nobody else did
    np = sys.modules.get("numpy")
    return np is not None and isinstance(obj, np.ndarray)


def is_numpy_scalar(obj):
    """Whether `obj` is a numpy scalar (False if numpy is not loaded)."""
    np = sys.modules.get("numpy")
    return np is not None and isinstance(obj, np.generic)


# ----------------------------------------------------------------------
#   SHARED MEMORY
# ----------------------------------------------------------------------


class SharedArrays:
    """
    Shared memory segments created by one side for one message.

    The sender keeps its segments open until the peer is done with them
    (on Windows, a segment disappears with its last handle), and then
    calls `release()`: the client when it gets the reply to its
    request, the worker when the client acknowledges that reply (`ack`
    message naming the reply's id). The receiver copies the data out
    with `attach()`.
    """

    def __init__(self, threshold, track=True):
        """
        Parameters
        ----------
        threshold : int
            Arrays of at least this many bytes go through shared
            memory. 0 (or a negative value) disables it.
        track : bool
            Let python's resource tracker follow the segments. Only
            effective on python >= 3.13; the worker passes False.
        """
        self.threshold = threshold
        self.track = track
        self.segments = []

    @property
    def names(self):
        """list[str]: Names of the segments currently held."""
        return [shm.name for shm in self.segments]

    def export(self, array):
        """
        Return `array` or, if it is large, a `ShmArray` describing it.

        The array is copied into a new segment, which is kept until
        `release()` is called. Arrays below the threshold, and arrays
        of objects, are returned unchanged (and are then pickled).

        Parameters
        ----------
        array : numpy.ndarray
            Array to export.

        Returns
        -------
        numpy.ndarray | ShmArray
        """
        if (
            not self.threshold or self.threshold < 0
            or array.nbytes < self.threshold
            or array.dtype.hasobject
        ):
            return array
        np = import_numpy()
        from multiprocessing.shared_memory import SharedMemory
        kwargs = {}
        if not self.track and sys.version_info >= (3, 13):
            kwargs["track"] = False
        # POSIX names are limited to 31 characters on macOS
        name = "mlrt" + secrets.token_hex(8)
        shm = SharedMemory(name, create=True, size=array.nbytes, **kwargs)
        self.segments.append(shm)
        order = "C"
        if array.flags.f_contiguous and not array.flags.c_contiguous:
            order = "F"
        view = np.ndarray(array.shape, array.dtype, shm.buf, order=order)
        view[...] = array
        del view
        return ShmArray(shm.name, array.dtype, array.shape, order)

    def release(self):
        """Close and unlink all segments (errors are ignored)."""
        segments, self.segments = self.segments, []
        for shm in segments:
            try:
                shm.close()
                shm.unlink()
            except (OSError, BufferError):
                pass

    @staticmethod
    def attach(ref):
        """
        Copy the array described by a `ShmArray` out of shared memory.

        Parameters
        ----------
        ref : ShmArray
            Description of the array and its segment.

        Returns
        -------
        numpy.ndarray
            A private copy; the segment is closed but not unlinked
            (its creator does that).
        """
        np = import_numpy()
        from multiprocessing.shared_memory import SharedMemory
        if sys.version_info >= (3, 13):
            shm = SharedMemory(ref.name, track=False)
        else:
            shm = SharedMemory(ref.name)
            if os.name == "posix":
                # Attaching registers the segment with the resource
                # tracker, which would unlink it (and warn) at exit.
                from multiprocessing import resource_tracker
                resource_tracker.unregister(shm._name, "shared_memory")
        try:
            view = np.ndarray(ref.shape, ref.dtype, shm.buf, order=ref.order)
            array = view.copy(order="K")
            del view
        finally:
            shm.close()
        return array


def untrack_shared_memory():
    """
    Stop the resource tracker from following our segments (worker only).

    Python < 3.13 has no `track=False` option, and its tracker would
    unlink segments that the client is still reading.
    """
    if sys.version_info < (3, 13):
        from multiprocessing import resource_tracker

        def _noop(*args, **kwargs):
            pass

        resource_tracker.register = _noop
        resource_tracker.unregister = _noop


# ----------------------------------------------------------------------
#   MATLAB <-> NUMPY (worker only)
# ----------------------------------------------------------------------


def is_matlab_array(obj):
    """Whether `obj` is a `matlab.double`, `matlab.int32`, ... array."""
    cls = type(obj)
    return (
        cls.__module__.split(".")[0] == "matlab"
        and cls.__name__ in _MATLAB_TO_NUMPY
    )


def matlab_to_numpy(obj):
    """
    Convert a `matlab.double`, `matlab.int32`, ... to a numpy array.

    Parameters
    ----------
    obj : matlab.double | matlab.int32 | ...
        See `is_matlab_array`.

    Returns
    -------
    numpy.ndarray
        Array with the dtype matching the MATLAB class (complex if the
        MATLAB array is complex).
    """
    np = import_numpy()
    dtype = _MATLAB_TO_NUMPY[type(obj).__name__]
    try:
        array = np.asarray(obj)
        if array.dtype.hasobject:
            raise TypeError
    except Exception:
        # Older releases do not implement the buffer protocol
        if getattr(obj, "_is_complex", False):
            array = np.asarray(obj._real) + 1j * np.asarray(obj._imag)
        else:
            array = np.asarray(obj._data, dtype=dtype)
        array = array.reshape(tuple(obj.size), order="F")
    if array.dtype.kind == "c":
        dtype = np.result_type(dtype, np.complex64)
    return array.astype(dtype, copy=False)


def numpy_to_matlab(array):
    """
    Convert a numpy array to the corresponding `matlab.*` array.

    Parameters
    ----------
    array : numpy.ndarray

    Returns
    -------
    matlab.double | matlab.int32 | ... | list | object
        A `matlab.*` array. Arrays of unsupported dtypes are converted
        to nested lists and 0-d arrays to python scalars.
    """
    kind = _NUMPY_TO_MATLAB.get(array.dtype.name)
    if kind is None:
        return array.tolist()
    if array.ndim == 0:
        return array.item()
    import matlab
    cls = getattr(matlab, kind)
    # `matlab.logical` has no `is_complex` argument: only pass it when
    # needed (complex arrays are always `double` or `single`).
    kwargs = {"is_complex": True} if array.dtype.kind == "c" else {}
    try:
        return cls(array, **kwargs)
    except Exception:
        # Older releases only accept (nested) sequences
        return cls(array.tolist(), **kwargs)
