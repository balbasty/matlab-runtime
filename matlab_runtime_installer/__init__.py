try:
    from ._version import __version__
except (ImportError, ModuleNotFoundError):
    __version__ = None

from .impl import install, uninstall
from .utils import guess_prefix
