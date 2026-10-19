try:
    from ._version import __version__
except (ImportError, ModuleNotFoundError):
    __version__ = None

from .impl import *     # noqa: F401, F403
from . import impl      # noqa: F401
from . import cli       # noqa: F401
from . import utils     # noqa: F401


def __getattr__(name):
    # `matlab_runtime.remote` is only imported when used
    if name == "remote":
        import importlib
        return importlib.import_module(".remote", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
