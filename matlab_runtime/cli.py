import argparse
import os
import os.path as op
import subprocess
import sys
from .impl import install, uninstall
from .utils import (
  guess_arch,
  guess_prefix,
  mwpython_version_args,
  resolve_runtime,
  VersionNotFoundError,
)


# --- install_matlab_runtime -------------------------------------------

def _make_parser():
    _ = "Install any matlab runtime in any location."
    p = argparse.ArgumentParser("install_matlab_runtime", description=_)
    _ = (
        "Version of the runtime to [un]install, such as 'latest' or 'R2022b' "
        "or '9.13'. Default is 'all' if '--uninstall' else 'latest'."
    )
    p.add_argument("-v", "--version", nargs="+", help=_)
    _ = f"Installation prefix. Default: '{guess_prefix()}'."
    p.add_argument("-p", "--prefix", help=_, default=guess_prefix())
    _ = (
        "Uninstall this version of the runtime. "
        "Use '--version all' to uninstall all versions."
    )
    p.add_argument("-u", "--uninstall", action="store_true", help=_)
    _ = (
        "Default answer (usually yes) to all questions. "
        "BY USING THIS OPTION, YOU ACCEPT THE TERMS OF THE MATLAB RUNTIME "
        "LICENSE. THE MATLAB RUNTIME INSTALLER WILL BE RUN WITH THE "
        "ARGUMENT `-agreeToLicense yes`. "
        "IF YOU ARE NOT WILLING TO DO SO, DO NOT USE THIS OPTION. "
        "https://mathworks.com/help/compiler/install-the-matlab-runtime.html"
    )
    p.add_argument("-y", "--yes", action="store_true", help=_)
    _ = "Patch the runtime if needed."
    p.add_argument("-x", "--patch", action="store_true", help=_)
    _ = (
        "Only install these products, by installer name (e.g. "
        "'MATLAB Runtime - Numerics'), short name (e.g. 'Numerics') "
        "or ID (e.g. '35010'). "
        "Default: all products."
    )
    p.add_argument("-f", "--products", nargs="+", metavar="PRODUCT", help=_)
    _ = (
        "Only install the products required by these compiled CTF "
        "archives. Can be combined with '--products'."
    )
    p.add_argument("-c", "--ctf", nargs="+", metavar="FILE", help=_)
    return p


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = _make_parser()
    p = parser.parse_args(args)
    if p.uninstall:
        if p.products or p.ctf:
            parser.error("'--products' and '--ctf' cannot be used with "
                         "'--uninstall'")
        uninstall(p.version, p.prefix, p.yes)
    else:
        install(
            p.version, p.prefix, p.yes, p.patch,
            products=p.products, ctf=p.ctf,
        )


# --- mwpython2 --------------------------------------------------------


_mpython_help = """
usage: mpython [-verbose] [-variant vrt] [py_args] [-mlstartup opt[,opt]] [-c cmd | -m mod | scr.py]

Arguments:
    -verbose            : verbose mode
    -variant            : MATLAB runtime version to use (default: latest
                          installed that supports this Python)
    py_args             : arguments and options passed to Python
    -mlstartup opt[,opt]: set of MATLAB runtime startup options
    -c cmd              : execute Python command cmd
    -m mod [arg[,arg]]  : execute Python module mod
    scr.py [arg[,arg]]  : execute Python script scr.py

Examples:
    Execute Python script myscript.py with mpython in verbose mode:
        mpython -verbose myscript.py arg1 arg2
    Execute Python script myscript.py, suppressing the runtime's Java VM:
        mpython -mlstartup -nojvm myscript.py arg1 arg2
    Execute Python module mymod.py:
        mpython -m mymod arg1 arg2
    Execute Python command 'x=3;print(x)'
        mpython -c "'x=3;print(x)'"
"""  # noqa: E501


def _has_mwpython(path):
    return op.exists(op.join(path, "bin", "mwpython"))


def _find_mwpython_runtime(variant="latest_installed", python_version=None,
                           prefix=None):
    """
    Find the MATLAB Runtime whose `mwpython` should wrap python (macOS).

    Parameters
    ----------
    variant : str, default="latest_installed"
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2').
        If 'latest_installed', the most recent installed release that
        supports `python_version`.
    python_version : str | tuple, optional
        Python version to wrap. Default: the running Python.
    prefix : str, optional
        Installation prefix (or release folder) searched first, as in
        `matlab_runtime.init`. Default: `$MATLAB_RUNTIME_PATH` if set,
        else `guess_prefix()`.

    Returns
    -------
    release : str
        MATLAB release (e.g. 'R2024b').
    mcrroot : str
        Root of the installation (that contains `bin/mwpython`).

    Raises
    ------
    RuntimeNotFoundError
        If no matching installation ships `bin/mwpython`.
    UnsupportedPythonError
        If the (installed) release(s) do not support `python_version`.
    """
    if python_version is None:
        python_version = sys.version_info
    return resolve_runtime(
        variant, prefix, python_version=python_version, accept=_has_mwpython
    )


class _MPythonError(RuntimeError):
    """
    `mwpython` cannot be used (e.g., not on macOS).

    Raised by `_mpython_command`; `mpython` prints it and exits with
    `returncode`.
    """

    def __init__(self, message, returncode=1):
        super().__init__(message)
        self.returncode = returncode


def _mpython_command(args, variant="latest_installed", verbose=False,
                     prefix=None):
    """
    Command and environment that run python through mwpython (macOS).

    Uses DYLD_FALLBACK_LIBRARY_PATH instead of DYLD_LIBRARY_PATH, and
    the calling python to determine which python to wrap.
    `os.environ` is not modified.

    Parameters
    ----------
    args : list[str]
        Arguments passed to python, e.g. `["-m", "module"]`.
    variant : str
        MATLAB runtime version to use (see `_find_mwpython_runtime`).
    verbose : bool
        Print what is being done.
    prefix : str, optional
        Installation prefix (or release folder) searched first
        (see `_find_mwpython_runtime`).

    Returns
    -------
    argv : list[str]
        Command to run.
    env : dict[str, str]
        Environment to run it in.

    Raises
    ------
    _MPythonError
        If not on macOS.
    VersionNotFoundError
        If no suitable runtime is installed (`RuntimeNotFoundError`), or
        if it does not support this python (`UnsupportedPythonError`).
    """
    ENV = dict(os.environ)

    # --- ARCH ---------------------------------------------------------

    if verbose:
        print("------------------------------------------")

    arch = guess_arch()

    if arch[:3] != "mac":
        raise _MPythonError("Execute mwpython only on Mac.", 10)

    if verbose:
        print(f"arch: {arch}")

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    variant, mcrroot = _find_mwpython_runtime(variant, python_version, prefix)
    exe_dir = op.join(mcrroot, "bin")
    if verbose:
        print(f"Found mwpython for MATLAB {variant} at {exe_dir}")

    # --- PYTHONHOME ---------------------------------------------------

    python_app = sys.executable
    python_home = sys.prefix
    python_libdir = op.join(python_home, "lib")

    ENV["PYTHONHOME"] = python_home

    if verbose:
        print(f"PYTHONHOME set to {python_home}")
        print(f"Original Python interpreter: {python_app}")
        print(f"Using Python {python_version}")

    # --- DYLD_FALLBACK_LIBRARY_PATH -----------------------------------

    if verbose:
        print("Setting up environment variable DYLD_FALLBACK_LIBRARY_PATH")

    MCRROOT = op.dirname(exe_dir)
    DYLD_FALLBACK_LIBRARY_PATH = ENV.get("DYLD_FALLBACK_LIBRARY_PATH")
    if DYLD_FALLBACK_LIBRARY_PATH:
        DYLD_FALLBACK_LIBRARY_PATH \
            = DYLD_FALLBACK_LIBRARY_PATH.split(os.pathsep)
    else:
        DYLD_FALLBACK_LIBRARY_PATH = []
    DYLD_FALLBACK_LIBRARY_PATH = (
       ".",
       f"{MCRROOT}/runtime/{arch}",
       f"{MCRROOT}/bin/{arch}",
       f"{MCRROOT}/sys/os/{arch}",
       python_libdir,
       *DYLD_FALLBACK_LIBRARY_PATH,
       "/usr/local/lib",
       "/usr/lib",
    )
    DYLD_FALLBACK_LIBRARY_PATH = os.pathsep.join(DYLD_FALLBACK_LIBRARY_PATH)
    ENV["DYLD_FALLBACK_LIBRARY_PATH"] = DYLD_FALLBACK_LIBRARY_PATH

    if verbose:
        print(f"DYLD_FALLBACK_LIBRARY_PATH is {DYLD_FALLBACK_LIBRARY_PATH}")

    # --- PYTHONPATH ---------------------------------------------------

    PYTHONPATH = ENV.get("PYTHONPATH")
    if PYTHONPATH:
        PYTHONPATH = PYTHONPATH.split(os.pathsep)
    else:
        PYTHONPATH = []
    PYTHONPATH = sys.path + PYTHONPATH
    PYTHONPATH = os.pathsep.join(PYTHONPATH)
    ENV["PYTHONPATH"] = PYTHONPATH

    if verbose:
        print(f"PYTHONPATH is {PYTHONPATH}")

    # --- mwpython app -------------------------------------------------
    maybe_mwpython_apps = [
        f"{exe_dir}/{arch}/mwpython.app/Contents/MacOS/mwpython",
        f"{exe_dir}/{arch}/mwpython{python_version}.app/Contents/MacOS/"
        f"mwpython{python_version}",
    ]
    mwpython_app = None
    for maybe_mwpython_app in maybe_mwpython_apps:
        if op.exists(maybe_mwpython_app):
            mwpython_app = maybe_mwpython_app
    if not mwpython_app:
        raise RuntimeError("Failed to locate mwpython")

    return [mwpython_app, *args, *mwpython_version_args(python_version)], ENV


def mpython(args=None):
    """
    Python wrapper that replaces MathWorks's `mwpython` (macOS).

    Parses the `mwpython` options (`-h`, `-verbose`, `-variant X`,
    `-c ...`, `-m ...`), builds the command with `_mpython_command` and
    runs it. Errors are printed rather than raised.

    Parameters
    ----------
    args : list[str], optional
        Command-line arguments. Default: `sys.argv[1:]`.

    Returns
    -------
    int
        Exit code: that of the python process, 10 if not on macOS,
        1 for invalid options or if no suitable runtime is found.
    """
    if args is None:
        args = sys.argv[1:]
    args = list(args)

    command = []
    module = []
    args_ = []
    variant = "latest_installed"
    verbose = False

    while args:
        arg = args.pop(0)
        if arg in ("-h", "-?", "-help"):
            print(_mpython_help)
            return 0
        elif arg == "-verbose":
            verbose = True
        elif arg == "-variant":
            if not args:
                print("Argument following -variant must not be empty")
                return 1
            variant = args.pop(0)
        elif arg == "-c":
            if not args:
                print("Argument following -c must not be empty")
                return 1
            command, args = args, []
        elif arg == "-m":
            if not args:
                print("Argument following -m must not be empty")
                return 1
            module, args = args, []
        else:
            args_ += [arg]
    args = args_

    if command:
        what, args = "Executing command: ", ["-c", *command]
    elif module:
        what, args = "Executing module: ", ["-m", *module]
    else:
        what = "Executing: "

    try:
        argv, env = _mpython_command(args, variant, verbose)
    except _MPythonError as e:
        print(e)
        return e.returncode
    except VersionNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    # As before `_mpython_command` was extracted, `mpython` exports the
    # mwpython environment (PYTHONHOME, DYLD_FALLBACK_LIBRARY_PATH, ...)
    os.environ.update(env)

    if verbose:
        print(what + " ".join(argv))
    ret = subprocess.run(argv, env=env).returncode
    if ret and (command or module):
        print(
            f"The following command failed with return value {ret}: "
            f"{' '.join(argv[:-2])}"
        )
    return ret


# backward compatibility !! DO NOT REMOVE !!
mwpython2 = mpython
