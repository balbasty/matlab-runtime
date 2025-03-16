import argparse
import sys
from .impl import install, uninstall
from .utils import guess_prefix


def _make_parser():
    _ = "Install any matlab runtime in any location."
    p = argparse.ArgumentParser("install_matlab_runtime", description=_)
    _ = (
        "Version of the runtime to [un]install, such as 'latest' or 'R2022b' "
        "or '9.13'. Default is 'all' if '--uninstall' else 'latest'."
    )
    p.add_argument("--version", "-v", nargs="+", help=_)
    _ = f"Installation prefix. Default: '{guess_prefix()}'."
    p.add_argument("--prefix", "-p", help=_, default=guess_prefix())
    _ = ("Uninstall this version of the runtime. "
         "Use '--version all' to uninstall all versions.")
    p.add_argument("--uninstall", "-u", action="store_true", help=_)
    _ = ("Default answer (usually yes) to all questions, "
         "**including MATLAB license agreement**.")
    p.add_argument("--yes", "-y", action="store_true", help=_)
    return p


def main(args=None):
    p = _make_parser().parse_args(args or sys.argv[1:])
    if p.uninstall:
        uninstall(p.version, p.prefix, p.yes)
    else:
        install(p.version, p.prefix, p.yes)
