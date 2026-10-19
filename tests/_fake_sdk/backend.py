"""Fake MATLAB SDK backend for the worker (tests only)."""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matlab  # noqa: E402  (the fake one)
from matlab_runtime.utils import python_argv  # noqa: E402


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

    def __init__(self, backend=None):
        self.backend = backend

    def init_kwargs(self):
        return self.backend.initialized

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

    def slow_big(self, seconds, n):
        time.sleep(seconds)
        return self.big(n)

    def big_and_obj(self, n):
        return [self.big(n), Obj()]

    def spawn(self):
        # A helper process, like those started by MATLAB
        child = subprocess.Popen(
            python_argv("-c", "import time; time.sleep(60)")
        )
        return child.pid

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

    def runtime_options(self):
        return self.backend.runtime

    def fail_not_found(self, msg):
        from matlab_runtime.utils import RuntimeNotFoundError
        raise RuntimeNotFoundError(msg)

    # --- mpython-like -------------------------------------------------

    def make_handle(self, offset=1):
        """A MATLAB function handle (`@(x) x + offset`)."""
        return matlab.object(lambda x: x + offset)

    def mpython_endpoint(self, f, *args, nargout=1):
        """Like mpython's endpoint: call a handle, or a named function."""
        if isinstance(f, matlab.object):
            return f(*args)
        return {"f": f, "args": list(args), "nargout": nargout,
                "types": [self.types(x) for x in args]}

    def is_handle(self, x):
        return isinstance(x, matlab.object)

    def cell(self):
        """An mpython-like tagged dict built in the worker."""
        return {"type__": "cell", "size__": matlab.double([[1, 2]]),
                "data__": [matlab.double([[1.0, 2.0]]), "a"]}


class FakeBackend:

    def __init__(self):
        self.initialized = None
        self.runtime = None

    def init(self, **kwargs):
        if self.initialized is not None:
            raise ValueError("MATLAB SDK already initialized")
        self.initialized = kwargs

    def init_runtime(self, option_list=(), error_if_already_init=False):
        if self.runtime is not None:
            if error_if_already_init:
                raise ValueError("MATLAB runtime already initialized")
            return
        self.runtime = list(option_list)

    def import_package(self, name, file, option_list=()):
        self.init_runtime(option_list)
        return FakePackage(self)
