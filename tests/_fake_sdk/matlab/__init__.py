"""Minimal stand-in for the `matlab` package of the MATLAB SDK (tests)."""
import numpy as np


class _Array:
    _dtype = None

    def __init__(self, initializer=None, size=None, is_complex=False):
        data = np.asarray(initializer if initializer is not None else [])
        if data.ndim < 2:
            data = data.reshape(1, -1)
        self._np_dtype = np.dtype(self._dtype)
        if is_complex:
            self._np_dtype = np.result_type(self._dtype, np.complex64)
        data = data.astype(self._np_dtype)
        self._is_complex = bool(is_complex)
        self.size = data.shape
        # MATLAB stores data in column-major order
        self._data = data.ravel(order="F").tolist()

    def __array__(self, dtype=None, copy=None):
        array = np.asarray(self._data, dtype=self._np_dtype)
        array = array.reshape(self.size, order="F")
        return array if dtype is None else array.astype(dtype)


def _make(name, dtype):
    return type(name, (_Array,), {"_dtype": dtype, "__module__": __name__})


class logical(_Array):
    """Like the real SDK, `logical` takes no `is_complex` argument."""
    _dtype = np.bool_

    def __init__(self, initializer=None, size=None):
        super().__init__(initializer, size)


double = _make("double", np.float64)
single = _make("single", np.float32)
int8 = _make("int8", np.int8)
int16 = _make("int16", np.int16)
int32 = _make("int32", np.int32)
int64 = _make("int64", np.int64)
uint8 = _make("uint8", np.uint8)
uint16 = _make("uint16", np.uint16)
uint32 = _make("uint32", np.uint32)
uint64 = _make("uint64", np.uint64)


class object:  # noqa: A001  (as in the real SDK)
    """Stand-in for MATLAB objects and function handles."""

    def __init__(self, func=None):
        self._func = func

    def __call__(self, *args):
        return self._func(*args)
