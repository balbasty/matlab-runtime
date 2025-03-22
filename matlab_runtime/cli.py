import argparse
import os
import os.path as op
import subprocess
import sys
from .impl import install, uninstall
from .utils import (
  guess_arch,
  guess_prefix,
  iter_existing_installations,
  SUPPORTED_PYTHON_VERSIONS,
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
    p.add_argument("-p", "--patch", action="store_true", help=_)
    return p


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    p = _make_parser().parse_args(args)
    if p.uninstall:
        uninstall(p.version, p.prefix, p.yes)
    else:
        install(p.version, p.prefix, p.yes, p.patch)


# --- mwpython2 --------------------------------------------------------


_mwpython_help = """
usage: mwpython2 [-verbose] [-variant vrt] [py_args] [-mlstartup opt[,opt]] [-c cmd | -m mod | scr.py]

Arguments:
    -verbose            : verbose mode
    -variant            : MATLAB runtime version to use (latest installed)
    py_args             : arguments and options passed to Python
    -mlstartup opt[,opt]: set of MATLAB runtime startup options
    -c cmd              : execute Python command cmd
    -m mod [arg[,arg]]  : execute Python module mod
    scr.py [arg[,arg]]  : execute Python script scr.py

Examples:
    Execute Python script myscript.py with mwpython in verbose mode:
        mwpython -verbose myscript.py arg1 arg2
    Execute Python script myscript.py, suppressing the runtime's Java VM:
        mwpython -mlstartup -nojvm myscript.py arg1 arg2
    Execute Python module mymod.py:
        mwpython -m mymod arg1 arg2
    Execute Python command 'x=3;print(x)'
        mwpython -c "'x=3;print(x)'"
"""  # noqa: E501


def mwpython2(args=None):
    # Python wrapper that replaces MathWorks's mwpython.
    # Uses DYLD_FALLBACK_LIBRARY_PATH instead of DYLD_LIBRARY_PATH.
    # Uses the calling python to determine which python to wrap.
    if args is None:
        args = sys.argv[1:]
    args = list(args)
    ENV = os.environ

    command = []
    module = []
    args_ = []
    variant = "latest_installed"
    verbose = False

    while args:
        arg = args.pop(0)
        if arg in ("-h", "-?", "-help"):
            print(_mwpython_help)
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

    # --- ARCH ---------------------------------------------------------

    if verbose:
        print("------------------------------------------")

    arch = guess_arch()

    if arch[:3] != "mac":
        print("Execute mwpython only on Mac.")
        return 10

    if verbose:
        print(f"arch: {arch}")

    exe_dir = None
    for path, ver in iter_existing_installations(variant):
        if op.exists(op.join(path, "bin", 'mwpython')):
            exe_dir = op.join(path, "bin")
            variant = ver
            if verbose:
                print(f"Found mwpython for MATLAB {variant} at {exe_dir}")
            break
    if exe_dir is None:
        raise RuntimeError(
            "No MATLAB Runtime found. If you have installed Matlab Runtime "
            "in an unusual location, please set the MATLAB_RUNTIME_PATH "
            "environment variable to the path of the installation."
        )

    # --- PYTHONHOME ---------------------------------------------------

    python_app = sys.executable
    python_home = sys.prefix
    python_libdir = op.join(python_home, "lib")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    supported_python_versions = SUPPORTED_PYTHON_VERSIONS[variant]
    if python_version not in supported_python_versions:
        print(
            f"Python {python_version} is unsupported with MATLAB {variant}. "
            f"Supported versions are:", ", ".join(supported_python_versions)
        )
        return 1

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

    # --- subprocess ---------------------------------------------------
    flag_and_ver = ["-mwpythonver", python_version]
    opt = dict(env=ENV)

    # --- command ------------------------------------------------------
    if command:
        command_and_version = command + flag_and_ver
        if verbose:
            print(
                f"Executing command: "
                f"{mwpython_app} -c {' '.join(command_and_version)}"
            )
        p = subprocess.run([mwpython_app, "-c", *command_and_version], **opt)
        ret = p.returncode
        if ret:
            print(
                f"The following command failed with return value {ret}: "
                f"{mwpython_app} -c {' '.join(command)}"
            )
        return ret

    # --- module -------------------------------------------------------
    if module:
        module_and_version = module + flag_and_ver
        if verbose:
            print(
                f"Executing module: "
                f"{mwpython_app} -m {' '.join(module_and_version)}"
            )
        p = subprocess.run([mwpython_app, "-m", *module_and_version], **opt)
        ret = p.returncode
        if ret:
            print(
                f"The following command failed with return value {ret}: "
                f"{mwpython_app} -m {' '.join(module)}"
            )
        return ret

    # --- args ---------------------------------------------------------
    args_and_version = args + flag_and_ver
    if verbose:
        print(
            f"Executing: "
            f"{mwpython_app} {' '.join(args_and_version)}"
        )
    p = subprocess.run([mwpython_app, *args_and_version], **opt)
    return p.returncode
