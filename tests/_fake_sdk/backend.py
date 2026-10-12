"""Fake MATLAB SDK backend for the worker (tests only)."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matlab  # noqa: E402  (the fake one)


class MatlabError(Exception):
    pass


class Obj:
    """An object that cannot be pickled."""

    def __init__(self):
        self.lock = threading.Lock()
        self.size = 7

    def value(self):
        return 42

    def __call__(self, x):
        return x + 1


class FakePackage:

    def test_runtime(self):
        return "success"

    def add(self, a, b):
        return a + b

    def ident(self, *args):
        return args[0] if len(args) == 1 else args

    def twoout(self, x, nargout=1):
        return x if nargout == 1 else (x, 2 * x)

    def big(self, n):
        return matlab.double([float(i) for i in range(n)])

    def echo_streams(self, stdout=None, stderr=None):
        print("printed by worker")
        print("printed on stderr", file=sys.stderr)
        if stdout is not None:
            stdout.write("to stdout\n")
        if stderr is not None:
            stderr.write("to stderr\n")
        return 0.0

    def make_obj(self):
        return Obj()

    def fail(self, msg):
        raise ValueError(msg)

    def fail_matlab(self):
        raise MatlabError("MATLAB says no")

    def crash(self):
        os._exit(3)

    def sleep(self, seconds):
        time.sleep(seconds)
        return seconds

    def getenv(self, name):
        return os.environ.get(name)

    def types(self, x):
        return type(x).__module__ + "." + type(x).__name__


class FakeBackend:

    def __init__(self):
        self.initialized = None

    def init(self, **kwargs):
        if self.initialized is not None:
            raise ValueError("MATLAB SDK already initialized")
        self.initialized = kwargs

    def import_package(self, name, file, option_list=()):
        return FakePackage()
