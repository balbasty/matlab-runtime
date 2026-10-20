"""
Conversion of values to and from their wire representation.

* `walk` maps a function over the leaves of (nested) lists, tuples and
  dicts, which is how both sides convert arguments and results.
* `SharedArrays` moves large numpy arrays through shared memory. The
  client keeps the segments of the worker's replies (`SharedSegment`)
  and sends them back by reference.
* `matlab_to_numpy` / `numpy_to_matlab` are only used in the worker.

numpy is imported lazily so that the client does not depend on it.
"""
import atexit
import errno
import os
import secrets
import sys
import weakref

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


def _reserve(shm):
    """
    Allocate the pages of a new segment, or raise `OSError`.

    On Linux, segments live in the tmpfs `/dev/shm` and are only
    truncated to size: writing past its free space raises SIGBUS.
    """
    if not sys.platform.startswith("linux"):
        return
    fd = getattr(shm, "_fd", -1)
    if isinstance(fd, int) and fd >= 0 and hasattr(os, "posix_fallocate"):
        os.posix_fallocate(fd, 0, shm.size)
        return
    st = os.statvfs("/dev/shm")
    if st.f_bavail * st.f_frsize < shm.size:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), "/dev/shm")


_Segment = None


def _open(name):
    """
    Open an existing segment, which the resource tracker does not follow.

    The segment is a `SharedMemory` whose `__del__` tolerates views
    that are still alive (the mapping is then left to the process).
    """
    global _Segment
    if _Segment is None:
        from multiprocessing.shared_memory import SharedMemory

        class _Segment(SharedMemory):
            def __del__(self):
                try:
                    self.close()
                except (OSError, BufferError):
                    pass
    if sys.version_info >= (3, 13):
        shm = _Segment(name, track=False)
    else:
        shm = _Segment(name)
        if os.name == "posix":
            # Attaching registers the segment with the resource tracker,
            # which would unlink it (and warn) at exit.
            from multiprocessing import resource_tracker
            resource_tracker.unregister(shm._name, "shared_memory")
    fd = getattr(shm, "_fd", -1)
    if isinstance(fd, int) and fd >= 0:
        # The mapping does not need it (`close`/`unlink` use the name)
        os.close(fd)
        shm._fd = -1
    return shm


def _unlink(shm):
    """Unlink a segment opened by `_open` (errors are ignored)."""
    if os.name != "posix":
        return  # Windows: it goes away with its last handle
    try:
        if sys.version_info >= (3, 13):
            shm.unlink()
        else:
            # `unlink()` would unregister it from the resource tracker,
            # which does not know it.
            from multiprocessing.shared_memory import _posixshmem
            _posixshmem.shm_unlink(shm._name)
    except OSError:
        pass  # e.g. already unlinked by the worker


# Segments owned by this process and not unlinked yet:
# name -> (SharedMemory, pid, owner)
_OWNED = {}
_ATEXIT = False


def _free(shm, holder, pid):
    # Finalizer of `SharedSegment`: drop the last export of the
    # mapping, close it, and unlink it (not from a forked child).
    holder.clear()
    try:
        shm.close()
    except (OSError, BufferError):
        pass
    if os.getpid() == pid and _OWNED.pop(shm.name, None) is not None:
        _unlink(shm)


def unlink_owned(owner):
    """
    Unlink the segments of `owner` (a session token).

    Called when the session ends; all the remaining segments are
    unlinked at exit. The arrays that use them stay valid, but are sent
    to the worker by copy from then on. If the client is killed hard,
    its segments leak.
    """
    for name, entry in list(_OWNED.items()):
        if entry[2] is owner and _OWNED.pop(name, None) is not None:
            if entry[1] == os.getpid():
                _unlink(entry[0])


def _unlink_all():
    for name in list(_OWNED):
        entry = _OWNED.pop(name, None)
        if entry is not None and entry[1] == os.getpid():
            _unlink(entry[0])


class SharedSegment:
    """
    Owner of a shared memory segment received from the worker.

    Arrays returned by `SharedArrays.attach` are views of it: it is at
    the end of their `.base` chain. Once it is garbage collected, i.e.
    when the last view is gone, the segment is closed and unlinked.

    Attributes
    ----------
    name : str
        Name of the segment.
    nbytes : int
        Size of its data.
    offset : int
        Offset of its data in the segment.
    owner : object
        Token of the session that owns it.
    linked : bool
        Whether it can still be opened by name (see `unlink_owned`).
    """

    def __init__(self, shm, ref, owner=None):
        """
        Parameters
        ----------
        shm : multiprocessing.shared_memory.SharedMemory
            Segment (opened with `_open`), closed with this object.
        ref : ShmArray
            Description of the array that it holds.
        owner : object
            Token of the session that owns it.
        """
        global _ATEXIT
        np = import_numpy("client")
        array = np.ndarray(
            ref.shape, ref.dtype, shm.buf, ref.offset, order=ref.order
        )
        self.name = shm.name
        self.nbytes = array.nbytes
        self.offset = ref.offset
        self.owner = owner
        # Views get the address of the data from here: they keep this
        # object alive, not `array`, which holds the only export of the
        # mapping (so that the finalizer can close it).
        self.__array_interface__ = array.__array_interface__
        self._shm = shm
        pid = os.getpid()
        weakref.finalize(self, _free, shm, [array], pid).atexit = False
        _OWNED[self.name] = (shm, pid, owner)
        if not _ATEXIT:
            atexit.register(_unlink_all)
            _ATEXIT = True

    @property
    def linked(self):
        entry = _OWNED.get(self.name)
        return entry is not None and entry[0] is self._shm


def _segment_of(array):
    """The `SharedSegment` at the end of the `.base` chain, or None."""
    np = import_numpy()
    base = array
    while base is not None:
        if isinstance(base, SharedSegment):
            return base
        if isinstance(base, memoryview):
            base = base.obj
        elif isinstance(base, np.ndarray):
            base = base.base
        else:
            return None
    return None


class SharedArrays:
    """
    Shared memory segments used by one side for one message.

    The sender keeps the segments that it creates open until the peer
    is done with them (on Windows, a segment disappears with its last
    handle), and then calls `release()`: the client when it gets the
    reply to its request, the worker when the client acknowledges that
    reply (`ack` message naming the reply's id). The client takes
    the segments of a reply over (`attach`): it returns views of them,
    and sends them back by reference (`export`); the worker copies
    the data out (`read`).
    """

    def __init__(self, threshold, track=True, owner=None):
        """
        Parameters
        ----------
        threshold : int
            Arrays of at least this many bytes go through shared
            memory. 0 (or a negative value) disables it.
        track : bool
            Let python's resource tracker follow the segments. Only
            effective on python >= 3.13; the worker passes False.
        owner : object
            Token of the session: only its segments are sent by
            reference.
        """
        self.threshold = threshold
        self.track = track
        self.owner = owner
        self.segments = []
        # Owned segments sent by reference, kept alive until `release`
        self.borrowed = []

    @property
    def names(self):
        """list[str]: Names of the segments currently held."""
        return [shm.name for shm in self.segments]

    def export(self, array):
        """
        Return `array` or, if it is large, a `ShmArray` describing it.

        A (C or F) contiguous view of a segment owned by this process
        (see `attach`) is sent by reference. Otherwise, the array is
        copied into a new segment, which is kept until `release()` is
        called. Arrays below the threshold, arrays of objects, and
        arrays for which no segment can be allocated (e.g. `/dev/shm`
        is full) are returned unchanged (and are then pickled).

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
        ref = self._reference(array)
        if ref is not None:
            return ref
        np = import_numpy()
        from multiprocessing.shared_memory import SharedMemory
        kwargs = {}
        if not self.track and sys.version_info >= (3, 13):
            kwargs["track"] = False
        # POSIX names are limited to 31 characters on macOS
        name = "mlrt" + secrets.token_hex(8)
        try:
            shm = SharedMemory(name, create=True, size=array.nbytes, **kwargs)
        except OSError:
            return array
        try:
            _reserve(shm)
        except OSError:
            # Not enough room (e.g. the small /dev/shm of a container):
            # send the array inline rather than die of SIGBUS.
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
            return array
        self.segments.append(shm)
        order = "C"
        if array.flags.f_contiguous and not array.flags.c_contiguous:
            order = "F"
        view = np.ndarray(array.shape, array.dtype, shm.buf, order=order)
        view[...] = array
        del view
        return ShmArray(shm.name, array.dtype, array.shape, order)

    def _reference(self, array):
        # A `ShmArray` pointing into the owned segment that holds
        # `array`, or None.
        segment = _segment_of(array)
        if (
            segment is None or segment.owner is not self.owner
            or not segment.linked
        ):
            return None
        if array.flags.c_contiguous:
            order = "C"
        elif array.flags.f_contiguous:
            order = "F"
        else:
            return None
        delta = (array.__array_interface__["data"][0]
                 - segment.__array_interface__["data"][0])
        if delta < 0 or delta + array.nbytes > segment.nbytes:
            return None
        offset = segment.offset + delta
        self.borrowed.append(segment)
        return ShmArray(segment.name, array.dtype, array.shape, order, offset)

    def release(self, keep=()):
        """
        Close the segments and unlink them (errors are ignored).

        Parameters
        ----------
        keep : collection[str]
            Names of the segments not to unlink (taken over by the
            client).
        """
        segments, self.segments = self.segments, []
        self.borrowed = []
        for shm in segments:
            try:
                shm.close()
            except (OSError, BufferError):
                pass
            if shm.name not in keep:
                try:
                    shm.unlink()
                except OSError:
                    pass

    @staticmethod
    def attach(ref, owner=None):
        """
        Take over the segment of a `ShmArray` (client).

        Parameters
        ----------
        ref : ShmArray
            Description of the array and its segment.
        owner : object
            Token of the session that takes it over.

        Returns
        -------
        numpy.ndarray
            A view of the segment, which is now owned by a
            `SharedSegment` (see there) at the end of its `.base`
            chain. The worker must be told not to unlink it.
        """
        np = import_numpy("client")
        shm = _open(ref.name)
        try:
            segment = SharedSegment(shm, ref, owner)
        except BaseException:
            try:
                shm.close()
            except BufferError:
                pass
            raise
        return np.asarray(segment)

    @staticmethod
    def read(ref, convert):
        """
        Convert the array of a `ShmArray` without owning it (worker).

        Parameters
        ----------
        ref : ShmArray
            Description of the array and its segment.
        convert : callable
            Applied to a numpy view of the segment. Its result must not
            refer to the view, as the segment is then closed (but not
            unlinked: its owner does that).

        Returns
        -------
        object
            The output of `convert`.
        """
        np = import_numpy()
        shm = _open(ref.name)
        try:
            view = np.ndarray(
                ref.shape, ref.dtype, shm.buf, ref.offset, order=ref.order
            )
            try:
                return convert(view)
            finally:
                del view
        finally:
            try:
                shm.close()
            except BufferError:
                pass  # held by a traceback: closed when collected


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


# (class, complex, order, ndim, dtype) -> whether the constructor copies
_COPIES = {}


def _copies(cls, kwargs, array):
    """Whether `cls(array, **kwargs)` copies the data (probed once)."""
    np = import_numpy()
    order = "F" if array.flags.f_contiguous else "C"
    key = (cls, bool(kwargs), order, array.ndim, array.dtype.str)
    if key not in _COPIES:
        shape = (2,) * max(array.ndim, 1)
        probe = np.zeros(shape, array.dtype, order=order)
        try:
            obj = cls(probe, **kwargs)
            probe[...] = 1
            _COPIES[key] = not matlab_to_numpy(obj).any()
        except Exception:
            _COPIES[key] = False
    return _COPIES[key]


def numpy_to_matlab(array, borrowed=False):
    """
    Convert a numpy array to the corresponding `matlab.*` array.

    Parameters
    ----------
    array : numpy.ndarray
    borrowed : bool
        The memory of `array` is about to go away (e.g. a view of a
        shared memory segment): copy it first, unless the `matlab.*`
        constructor is known to copy anyway.

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
    if borrowed and not _copies(cls, kwargs, array):
        array = array.copy(order="K")
    try:
        return cls(array, **kwargs)
    except Exception:
        # Older releases only accept (nested) sequences
        return cls(array.tolist(), **kwargs)
