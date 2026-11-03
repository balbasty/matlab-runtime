__all__ = [
    "install",
    "uninstall",
    "init",
    "import_deployed",
    "init_runtime",
    "terminate_runtime",
    "guess_prefix",
    "user_prefix",
    "guess_arch",
]
import atexit
import importlib
import os
import os.path as op
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
import weakref

from .utils import (
    askprefix,
    askuser,
    guess_arch,
    guess_prefix,
    user_prefix,
    guess_installer,
    guess_release,
    find_runtime,
    find_runtime_in_prefix,
    has_mwpython,
    LICENSE_URL,
    tilde,
    patch_runtime,
    macos_version,
    matlab_release,
    matlab_version,
    python_supported,
    resolve_runtime,
    ctf_matlab_release,
    deployed_ctf,
    guess_pymatlab_version,
    url_download,
    ctf_required_products,
    running_under_mwpython,
    MODE_ENV,
    installer_products,
    make_installer_input,
    resolve_products,
    unquarantine,
    ZipFileWithExecPerm,
)


def install(
    version=None,
    prefix=None,
    auto_answer=False,
    patch=None,
    products=None,
    ctf=None,
    default_scope=None,
    if_missing=False,
    python_version=None,
):
    """
    Install the matlab runtime.

    !!! warning
        BY SETTING `auto_answer=True`, YOU ACCEPT THE TERMS OF THE
        MATLAB RUNTIME LICENSE. THE MATLAB RUNTIME INSTALLER WILL BE
        RUN WITH THE ARGUMENT `-agreeToLicense yes`.
        IF YOU ARE NOT WILLING TO DO SO, DO NOT CALL THIS FUNCTION.

        https://mathworks.com/help/compiler/install-the-matlab-runtime.html

    Parameters
    ----------
    version : [list of] str, default="latest"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        With `if_missing`, the default is 'latest_installed': any
        installed release will do, else the most recent release (that
        supports `python_version`) is installed.
    prefix : str, optional
        Install location. Default: `$MATLAB_RUNTIME_PATH` if set, else
        the user is asked to choose (see `utils.askprefix`) between:
        * "user" (`user_prefix()`, no admin rights needed):
            * Windows:  %LOCALAPPDATA%\\Programs\\MATLAB\\MATLAB Runtime
            * Linux:    ~/.local/share/MATLAB/MATLAB_Runtime
            * MacOS:    ~/Applications/MATLAB/MATLAB_Runtime
        * "system" (`guess_prefix()`):
            * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
            * Linux:    /usr/local/MATLAB/MATLAB_Runtime
            * MacOS:    /Applications/MATLAB/MATLAB_Runtime
        * "custom": a folder that the user types.
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    patch : bool, optional
        Patch the runtime (MacOS only, see the README). Default: no.
    products : [list of] str, optional
        Only install these products. Products can be specified by
        installer name (e.g., 'MATLAB Runtime - Numerics'),
        short name (e.g., 'Numerics') or ID (e.g., '35010').
        Default: install all products.
    ctf : [list of] str, optional
        Only install the products required by these compiled CTF
        archives. Combined (union) with `products`.
    default_scope : {"user", "system", "custom"}, default="system"
        Installation location chosen when `prefix` is not provided and
        the user just presses Enter (or when `auto_answer` is set).
        Applications can use it to choose the default that suits them.
    if_missing : bool | {"mwpython"}, default=False
        Only install the runtime if no matching installation is found
        (see `utils.resolve_runtime`, which searches `prefix` first,
        then the standard locations). If one is found, nothing is asked
        or printed, and it is returned. With "mwpython", only count
        installations that ship `bin/mwpython` (the ones that `mpython`
        can use, on MacOS).
    python_version : str | False, optional
        With `if_missing`, only count releases that support this python
        version (e.g. '3.12'). Default: the running python.
        False: do not check the python version.

    Returns
    -------
    release : str
        MATLAB release (e.g. 'R2024b').
    path : str
        Root of the installation (installed or found).
        A list of `(release, path)` is returned if `version` is a list.

    Raises
    ------
    UserInterruptionError
        If the user declines the installation.
    ValueError
        If a requested product is unknown.
    VersionNotFoundError
        With `if_missing`, if the requested release does not support
        `python_version` (`UnsupportedPythonError`).

    Warns
    -----
    UserWarning
        If products are requested but the installer does not
        support product selection (all products are then installed).
    """
    if if_missing not in (False, None, True, "mwpython"):
        raise ValueError(
            f"Invalid value for if_missing: {if_missing!r}. "
            f"Expected True, False or 'mwpython'."
        )
    options = dict(patch=patch, products=products, ctf=ctf,
                   default_scope=default_scope)

    def find(a_version):
        # (release, path) of a matching installation, else (release, None)
        if python_version is None:
            python = sys.version_info
        else:
            python = python_version or None
        return resolve_runtime(
            a_version or "latest_installed",
            prefix,
            python_version=python,
            accept=has_mwpython if if_missing == "mwpython" else None,
            missing_ok=True,
        )

    # --- iterate if multiple versions  --------------------------------
    if isinstance(version, (list, tuple, set)):
        versions = list(version)
        found = [find(v) if if_missing else (v, None) for v in versions]
        if prefix is None and not all(path for _, path in found):
            # Ask for the location once
            prefix = _default_prefix(default_scope, auto_answer)
        return [
            (release, path) if path else
            _install(release, prefix, auto_answer, missing=bool(if_missing),
                     **options)
            for release, path in found
        ]

    # --- single version  ----------------------------------------------
    if if_missing:
        version, path = find(version)
        if path:
            return version, path
    return _install(version, prefix, auto_answer, missing=bool(if_missing),
                    **options)


def _install(
    version=None,
    prefix=None,
    auto_answer=False,
    patch=None,
    products=None,
    ctf=None,
    default_scope=None,
    missing=False,
):
    """
    Install one release of the runtime (see `install`).

    The questions are asked up front (location, license), so that the
    download and installation then run without interruption.
    `missing` only changes the first message.
    Returns `(release, path)`.
    """
    # --- requested products  ------------------------------------------
    if isinstance(products, str):
        products = [products]
    if isinstance(ctf, str):
        ctf = [ctf]
    requested = list(products or [])
    for ctf_file in (ctf or []):
        requested += ctf_required_products(ctf_file)

    # --- prepare  -----------------------------------------------------
    license = "matlabruntime_license_agreement.pdf"
    arch = guess_arch()
    version = guess_release(version or "latest", arch)
    url = guess_installer(version, arch)
    name = f"MATLAB Runtime {version}"
    update = re.search(r"_Update_(\d+)_", url)
    full_name = f"{name} (Update {update[1]})" if update else name
    proc, log = None, ""  # installer outputs, for diagnostics

    if missing:
        print(f"{name} is not installed.")
    if prefix is None:
        prefix = _default_prefix(default_scope, auto_answer)
    prefix = op.realpath(op.abspath(prefix))

    # --- check already exists -----------------------------------------
    path = find_runtime_in_prefix(version, prefix)
    if path:
        print(f"{name} is already installed in {tilde(path)}.")
        if not askuser("Reinstall it?", "no", auto_answer):
            return version, path
    elif not missing:
        # With `missing`, other installations have already been rejected
        path = find_runtime(version)
        if path:
            print(f"{name} is already installed in {tilde(path)}.")
            if not askuser(f"Install another copy in {tilde(prefix)}?",
                           "no", auto_answer):
                return version, path

    # --- summary and license (last question) --------------------------
    target = op.join(prefix, version)
    what = []
    if products:
        what += list(products)
    if ctf:
        what += [f"those needed by {op.basename(f)}" for f in ctf]
    print()
    print(f"Ready to install {full_name}")
    print(f"  location:  {tilde(target)}")
    print(f"  products:  {', '.join(what) or 'all'}")
    print(f"  license:   {LICENSE_URL}")
    print("Installing the MATLAB Runtime means that you accept the terms "
          "of its license.")
    askuser("Accept the license and install?", "yes", auto_answer,
            raise_if_no=True)
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = op.realpath(op.abspath(tmpdir))

        # --- download -------------------------------------------------
        print(f"Downloading {op.basename(url)}")
        installer = url_download(url, tmpdir)

        # --- unzip ----------------------------------------------------
        if installer.endswith(".zip"):
            print("Extracting the installer")
            with ZipFileWithExecPerm(installer) as zip:
                zip.extractall(tmpdir)

            if arch[:3] == "win":
                installer = op.join(tmpdir, "setup.exe")
            else:
                installer = op.join(tmpdir, "install")

        if not op.exists(installer):
            print("No installer found in archive:", os.listdir(tmpdir))
            raise FileNotFoundError("No installer found in archive")

        # --- products -------------------------------------------------
        names = None
        if requested and not url.endswith(".zip"):
            warnings.warn(
                f"Product selection is not supported by this "
                f"installer ({url}). Installing all products instead."
            )
        elif requested:
            available = installer_products(tmpdir)
            if available is None:
                warnings.warn(
                    "The installer does not provide product metadata: "
                    "product names cannot be validated."
                )
            names = resolve_products(requested, available)
            print("Selected products:", ", ".join(names))

        # --- install --------------------------------------------------
        if arch[:3] == "mac" and macos_version() > (10, 14):
            # Only needed if the installer is quarantined (see unquarantine)
            unquarantine(tmpdir, probe=installer)

        if names:
            # Product selection is only possible through an input file
            inputfile = op.join(tmpdir, "installer_input.txt")
            logfile = op.join(tmpdir, "installer_log.txt")
            with open(inputfile, "w") as f:
                f.write(make_installer_input(names, prefix, logfile))
            call = [installer, "-inputFile", inputfile]
        else:
            call = [
                installer,
                "-agreeToLicense", "yes",
                "-mode", "silent",
                "-destinationFolder", prefix,
            ]
        print("Installing (this can take several minutes)")
        # NOTE: capture_output=True is **required** on windows
        # NOTE: `-tmpdir` makes older installers (e.g. R2019b) fail to
        # extract archives ("No such file or directory"), so the installer's
        # temporary directory is set through the environment instead.
        env = dict(os.environ, TMPDIR=tmpdir, TMP=tmpdir, TEMP=tmpdir)
        proc = subprocess.run(call, capture_output=True, env=env)
        ret = proc.returncode
        log = ""
        if names and op.exists(logfile):
            with open(logfile, errors="replace") as f:
                log = f.read()
            if "End - Successful" not in log:
                print(log[-3000:])
                ret = ret or 1
        if ret:
            print(f"The installer failed (exit code {ret}).")

    # --- check --------------------------------------------------------
    path = find_runtime_in_prefix(version, prefix) or target
    if not op.exists(op.join(path, "VersionInfo.xml")):
        if op.exists(path):
            print(
                f"Runtime not found where it is expected: {path} exists "
                f"but has no VersionInfo.xml. It contains:",
                sorted(os.listdir(path))
            )
        elif op.exists(prefix):
            print(
                f"Runtime not found where it is expected: {path} does not "
                f"exist. {prefix} contains:",
                sorted(os.listdir(prefix))
            )
        if log and "End - Successful" in log:
            print("Installer log (tail):\n", log[-3000:])
        if proc is not None:
            outputs = (("stdout", proc.stdout), ("stderr", proc.stderr))
            for name, out in outputs:
                out = (out or b"").decode(errors="replace").strip()
                if out:
                    print(f"Installer {name} (tail):\n", out[-3000:])
        raise FileNotFoundError(
            f"Runtime not found where it is expected: {path}"
        )

    # --- patch --------------------------------------------------------
    if arch[:3] == "mac" and patch:
        print("Patching the runtime")
        patch_runtime(path)

    # --- goodbye ------------------------------------------------------
    print(f"{name} installed in {tilde(path)}")
    print(f"License agreement: {tilde(op.join(path, license))}")
    return version, path


def uninstall(version=None, prefix=None, auto_answer=False,
              default_scope=None):
    """
    Uninstall the matlab runtime.

    Parameters
    ----------
    version : [list of] str, default="all"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'all', uninstall all installed versions.
    prefix : str, optional
        Install location. Default: `$MATLAB_RUNTIME_PATH` if set, else
        the user is asked to choose (see `install`).
    auto_answer : bool
        Default answer to all questions.
    default_scope : {"user", "system", "custom"}, default="system"
        Location chosen when `prefix` is not provided and the user just
        presses Enter (or when `auto_answer` is set).
    """
    # --- iterate if multiple versions  --------------------------------
    if isinstance(version, (list, tuple, set)):
        if prefix is None:
            prefix = _default_prefix(default_scope, auto_answer)
        for a_version in version:
            try:
                uninstall(a_version, prefix, auto_answer)
            except Exception as e:
                print(f"[{type(e)}] Failed to uninstall runtime:", version, e)
        return

    # --- prepare  -----------------------------------------------------
    raise_if_no = True
    arch = guess_arch()
    version = version or "all"
    if version != "all":
        version = matlab_release(version)

    if prefix is None:
        prefix = _default_prefix(default_scope, auto_answer)

    if version.lower() == "all":
        rmdir = prefix
    else:
        rmdir = op.join(prefix, version)

    # --- uninstall  ---------------------------------------------------
    question = f"Remove directory {rmdir} and its content?"
    askuser(question, "yes", auto_answer, raise_if_no)

    if arch[:3] == "win":
        # --- Windows: call uninstaller ---
        if version == "all":
            versions = [op.join(prefix, ver) for ver in os.listdir(prefix)]
        else:
            versions = [version]
        for ver in versions:
            subprocess.run([op.join(
                prefix, ver, "bin", arch, "Uninstall_MATLAB_Runtime.exe"
            )])
    else:
        # --- Unix: remove folder ---
        shutil.rmtree(rmdir)

    print("Runtime(s) succesfully uninstalled from:", rmdir)


def _default_prefix(default_scope=None, auto_answer=False):
    """
    Prefix used when none is provided: `$MATLAB_RUNTIME_PATH` if set,
    else the user's choice (see `utils.askprefix`).
    """
    if os.environ.get("MATLAB_RUNTIME_PATH", ""):
        return guess_prefix()
    return askprefix(default_scope, auto_answer)


_DEPLOYED_MODULES = {}
_INITIALIZED = {"SDK": False, "RUNTIME": False}
_CPP = "matlabruntimeforpython_abi3"
_SDK = "matlab_pysdk.runtime"
_MLB = "matlab"

# --- runtime mode -----------------------------------------------------
# "inprocess": the MATLAB SDK is loaded in this process.
# "remote":    it runs in a worker process (`matlab_runtime.remote`).
# Once chosen (by the first call to `init`, `import_deployed` or
# `init_runtime`), the mode is fixed for the process.
_MODES = ("inprocess", "remote", "auto")
_MODE = {"mode": None}


def _resolve_mode(mode=None):
    """
    Mode that `mode` (or `$MATLAB_RUNTIME_MODE`, or "auto") stands for.

    "auto" is "remote" on macOS, unless this interpreter is `mwpython`
    (in which case the SDK can be loaded in-process), and "inprocess"
    on other platforms.
    """
    if mode is None:
        mode = os.environ.get(MODE_ENV, "") or "auto"
    key = str(mode).strip().lower()
    if key not in _MODES:
        raise ValueError(
            f"Unknown MATLAB runtime mode {mode!r}: expected one of "
            f"{', '.join(map(repr, _MODES))} (argument `mode`, or "
            f"environment variable {MODE_ENV})."
        )
    if key == "auto":
        if sys.platform == "darwin" and not running_under_mwpython():
            return "remote"
        return "inprocess"
    return key


def _use_mode(mode=None):
    """
    Choose the mode of a call to `init`, `import_deployed` or
    `init_runtime`, and fix it for the process.

    Once a mode is fixed, `mode=None` and `mode="auto"` select it (and
    `$MATLAB_RUNTIME_MODE` is no longer read), while another explicit
    mode raises a RuntimeError.
    """
    current = _MODE["mode"]
    if current is not None and mode is not None:
        wanted = _resolve_mode(mode)
        if str(mode).strip().lower() == "auto":
            wanted = current
    elif current is not None:
        wanted = current
    else:
        wanted = _resolve_mode(mode)
    if current is None:
        if wanted == "remote" and (
            _INITIALIZED["SDK"] or _INITIALIZED["RUNTIME"]
        ):
            raise RuntimeError(
                "The MATLAB SDK has already been initialized in this "
                "process: it cannot be used in 'remote' mode."
            )
        _MODE["mode"] = wanted
    elif wanted != current:
        raise RuntimeError(
            f"The MATLAB runtime is already used in {current!r} mode in "
            f"this process: it cannot be used in {wanted!r} mode as well."
        )
    return wanted


def init(
    version="latest_installed",
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    mode=None,
    default_scope=None,
    *,
    products=None,
    ctf=None,
    patch=None,
):
    """
    Set current environment so that the MATLAB Python SDK is properly
    linked and usable.

    Parameters
    ----------
    version : str, default="latest_installed"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'latest_installed', use the most recent installed release
        that supports the running Python (see `utils.resolve_runtime`).
        If none is installed, raise an error, unless `install_if_missing`,
        in which case the most recent release that supports the running
        Python is installed.
    install_if_missing : bool
        If target version is missing, run installer.
    prefix : str, optional
        Location searched first (an installation prefix or a release
        folder), and install location. Default: `$MATLAB_RUNTIME_PATH`,
        if set; else the system-wide prefix (`guess_prefix()`) is
        searched first, and the user is asked where to install
        (see `install`).
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    mode : {"inprocess", "remote", "auto"}, optional
        Where the MATLAB SDK runs:

        * "inprocess": in this process.
        * "remote": in a worker process, through `matlab_runtime.remote`
          (`init` then calls `remote.init` with the same arguments).
          Deployed functions then return numpy arrays, numbers,
          strings, lists and dicts instead of `matlab.*` objects, and
          any other object (e.g., MATLAB objects and function handles)
          as a `remote.RemoteHandle` (see `remote.is_matlab_object`).
        * "auto": "remote" on macOS, unless this interpreter is
          `mwpython` (e.g., started with `mpython`); "inprocess" on
          other platforms.

        Default: the mode already used in this process, else the
        environment variable `MATLAB_RUNTIME_MODE`, else "auto".
        The mode is fixed for the process by the first call to `init`,
        `import_deployed` or `init_runtime`.
    default_scope : {"user", "system", "custom"}, default="system"
        Install location chosen when `prefix` is not provided and the
        user just presses Enter (or when `auto_answer` is set)
        (see `install`).

    Other Parameters
    ----------------
    products : [list of] str, optional
        Only install these products, if the runtime is installed
        (see `install`). Default: all products.
    ctf : [list of] str, optional
        Only install the products required by these compiled CTF
        archives, if the runtime is installed (see `install`).
        Combined (union) with `products`.
    patch : bool, optional
        Patch the runtime if it is installed (MacOS only, see `install`).

    Raises
    ------
    ValueError
        If another release of the SDK has already been initialized,
        or if `mode` is not a valid mode.
    RuntimeError
        If the runtime is already used in another mode in this process.
    RuntimeNotFoundError
        If no matching runtime is installed (and not `install_if_missing`).
    UnsupportedPythonError
        If the requested release does not support the running Python
        (according to `utils.SUPPORTED_PYTHON_VERSIONS`).

    Warns
    -----
    UserWarning
        If the release is not listed in `utils.SUPPORTED_PYTHON_VERSIONS`,
        so that compatibility with the running Python cannot be checked.
    """
    # --- remote mode  -------------------------------------------------
    if _use_mode(mode) == "remote":
        from . import remote
        return remote.init(version, install_if_missing, prefix, auto_answer,
                           default_scope=default_scope, products=products,
                           ctf=ctf, patch=patch)

    # --- prepare  -----------------------------------------------------
    arch = guess_arch()
    version = version or "latest_installed"

    # --- if already initialized: stop  --------------------------------
    sdk_release = _INITIALIZED["SDK"]
    if sdk_release:
        if str(version).lower() == "latest_installed":
            return
        release = guess_release(version, arch)
        if sdk_release != release:
            raise ValueError(
                f"Another version ({sdk_release}) of the MATLAB SDK has "
                f"already been initialized, and differs from the target "
                f"version ({release}). It is currently not possible to "
                f"run multiple versions of MATLAB at the same time."
            )
        return

    # --- find version  ------------------------------------------------
    python_version = "{}.{}".format(*sys.version_info[:2])
    if install_if_missing:
        release, path = install(
            version, prefix, auto_answer, patch=patch, products=products,
            ctf=ctf, default_scope=default_scope,
            if_missing=True, python_version=python_version,
        )
    else:
        release, path = resolve_runtime(
            version, prefix, python_version=python_version,
        )

    if python_supported(release, python_version) is None:
        warnings.warn(
            f"Python versions supported by MATLAB {release} are unknown: "
            f"cannot check that Python {python_version} is supported."
        )

    # --- prepare paths  -----------------------------------------------
    ext = op.join(path, 'extern', 'bin', arch)
    sdk = op.join(path, 'toolbox', 'compiler_sdk', 'pysdk_py')
    mod = op.join(sdk, 'matlab_mod_dist')
    bin = op.join(path, 'bin', arch)

    # --- set paths  ---------------------------------------------------
    if arch[:3] == "win":
        PATH = os.environ['PATH'].split(os.pathsep)
        os.environ['PATH'] = os.pathsep.join([ext, bin] + PATH)

    sys.path.insert(0, bin)
    sys.path.insert(0, mod)
    sys.path.insert(0, sdk)
    sys.path.insert(0, ext)

    # --- imports  -----------------------------------------------------
    importlib.import_module(_CPP)
    importlib.import_module(_SDK)
    matlab = importlib.import_module(_MLB)

    # --- check version  -----------------------------------------------
    try:
        current_version = guess_pymatlab_version(matlab)
        current_version = tuple(map(int, current_version.split(".")[:2]))
    except ValueError:
        current_version = None

    target_version = matlab_version(release)
    target_version = tuple(map(int, target_version.split(".")[:2]))

    if current_version is None:
        warnings.warn("Could not determine version of matlab-python.")
    elif current_version != target_version:
        raise RuntimeError(
            f'Runtime version of package ({target_version}) does not match '
            f'runtime version of previously loaded package ({current_version})'
        )

    _INITIALIZED["SDK"] = release


def _deployed_release(packages):
    """
    Release that compiled packages target, read from their CTF archives.

    Returns None if it cannot be determined. Raises ValueError if the
    packages were compiled with different releases.
    """
    releases = {}
    for package in packages:
        ctf = deployed_ctf(package)
        if not ctf:
            continue
        try:
            release = ctf_matlab_release(ctf)
        except Exception:
            continue
        name = package if isinstance(package, str) else package.__name__
        releases.setdefault(release, []).append(name)
    if len(releases) > 1:
        listing = "; ".join(
            f"{release}: {', '.join(names)}"
            for release, names in releases.items()
        )
        raise ValueError(
            f"Packages were compiled with different MATLAB releases "
            f"({listing}) and cannot be used in the same process."
        )
    return next(iter(releases), None)


def _deployed_ctfs(packages):
    """CTF archives of compiled packages (those that have one)."""
    return [ctf for ctf in map(deployed_ctf, packages) if ctf]


def _init_options(packages, only_needed_products=False, products=None,
                  **options):
    """
    Keyword arguments of `init` used by `import_deployed`.

    With `only_needed_products`, the products required by the packages'
    CTF archives are installed (in addition to `products`), if the
    runtime is installed.
    """
    ctf = _deployed_ctfs(packages) if only_needed_products else None
    return dict(options, products=products, ctf=ctf)


class _PathInitializer:
    def __init__(self):
        if not _INITIALIZED["SDK"]:
            init()
        self.cppext_handle = importlib.import_module(_CPP)


def import_deployed(
    *packages,
    option_list=tuple(),
    mode=None,
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    default_scope=None,
    only_needed_products=False,
    products=None,
    patch=None,
):
    """
    Initialize compiled MATLAB packages so that they can be used from python.

    If the MATLAB SDK is not initialized yet, it is initialized (with
    `init`) with the release that the packages were compiled with (read
    from their CTF archive), or else with the most recent installed
    release. That release is installed if it is missing and
    `install_if_missing` is set.

    The arguments `install_if_missing`, `prefix`, `auto_answer`,
    `default_scope`, `only_needed_products`, `products` and `patch` are
    only used if the SDK is not initialized yet (they are passed to
    `init`).

    Parameters
    ----------
    *packages : module | str
        Python package that contains a compiled MATLAB package (a ctf file).
    option_list : list[str]
        Options passed to MATLAB, if the runtime is not initialized yet.
    mode : {"inprocess", "remote", "auto"}, optional
        Where the MATLAB SDK runs (see `init`). In "remote" mode, the
        packages are imported in the worker process, and handles to
        them (`remote.RemoteHandle`) are returned: their functions
        return numpy arrays, numbers, strings, lists and dicts instead
        of `matlab.*` objects.
    install_if_missing : bool
        If the target release is missing, run installer (see `init`).
    prefix : str, optional
        Location searched first, and install location (see `init`).
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    default_scope : {"user", "system", "custom"}, default="system"
        Install location chosen when `prefix` is not provided and the
        user just presses Enter (see `install`).
    only_needed_products : bool
        If the runtime is installed, only install the products required
        by the packages (read from their CTF archive), in addition to
        `products`. Default: install all products. Note that a runtime
        installed with a subset of products may not be able to run
        other packages.
    products : [list of] str, optional
        Only install these products, if the runtime is installed
        (see `install`). Default: all products.
    patch : bool, optional
        Patch the runtime if it is installed (MacOS only, see `install`).

    Returns
    -------
    *modules : module | remote.RemoteHandle
        Imported MATLAB modules.

    Raises
    ------
    ValueError
        If the packages were compiled with a release that differs from
        the one already initialized (or from each other).
    RuntimeError
        If the runtime is already used in another mode in this process.
    """
    install_options = dict(
        install_if_missing=install_if_missing,
        prefix=prefix,
        auto_answer=auto_answer,
        default_scope=default_scope,
        only_needed_products=only_needed_products,
        products=products,
        patch=patch,
    )
    if _use_mode(mode) == "remote":
        from . import remote
        return remote.import_deployed(
            *packages, option_list=option_list, **install_options
        )

    release = _deployed_release(packages)
    sdk_release = _INITIALIZED["SDK"]
    if release and sdk_release and release != sdk_release:
        raise ValueError(
            f"The MATLAB SDK was initialized with MATLAB {sdk_release}, but "
            f"the deployed package(s) were compiled with MATLAB {release}. "
            f"A compiled package can only run with the MATLAB Runtime of "
            f"the release that compiled it. Call init(\"{release}\") "
            f"before anything else initializes the SDK."
        )
    if not sdk_release:
        init(release or "latest_installed",
             **_init_options(packages, **install_options))

    if not _INITIALIZED["RUNTIME"]:
        init_runtime(option_list)

    sdk = importlib.import_module(_SDK)

    handles = []
    for package in packages:
        if isinstance(package, str):
            package = importlib.import_module(package)
        handle = _DEPLOYED_MODULES.get(package, lambda: None)()
        if handle is None:
            handle = sdk.DeployablePackage(
                _PathInitializer(), package.__name__, package.__file__
            )
            handle.initialize()
            _DEPLOYED_MODULES[package] = weakref.ref(handle)
        handles.append(handle)

    return handles[0] if len(handles) == 1 else tuple(handles)


def init_runtime(option_list=tuple(), error_if_already_init=False,
                 mode=None):
    """
    Initialize the MATLAB runtime.

    If the SDK is not initialized yet, it is initialized with `init()`.

    Parameters
    ----------
    option_list : list[str]
        Options passed to MATLAB.
    error_if_already_init : bool
        Raise a ValueError if the runtime is already initialized.
    mode : {"inprocess", "remote", "auto"}, optional
        Where the MATLAB SDK runs (see `init`). In "remote" mode, the
        runtime is initialized in the worker process (which is started,
        with `init()`, if needed).
    """
    if _use_mode(mode) == "remote":
        from . import remote
        return remote._init_runtime(option_list, error_if_already_init)

    if _INITIALIZED.get("RUNTIME", False):
        if error_if_already_init:
            raise ValueError("MATLAB runtime already initialized")
        else:
            return

    if not _INITIALIZED.get("SDK", False):
        init()

    cppext = importlib.import_module(_CPP)

    arch = guess_arch()
    if arch[:3] == "mac":
        ignored_option_found = False
        for option in option_list:
            if option in ('-nodisplay', '-nojvm'):
                ignored_option_found = True
                break
        if ignored_option_found:
            warnings.warn(
                'Options "-nodisplay" and "-nojvm" are ignored on Mac.'
                'They must be passed to mwpython in order to take effect.'
            )
    elif arch[:3] == "gln":
        if not os.environ.get("DISPLAY", ""):
            # Headless environment -> ensure -nodisplay is set
            if "-nodisplay" not in option_list:
                option_list = (*option_list, "-nodisplay")

    cppext.initializeApplication(option_list)
    _INITIALIZED["RUNTIME"] = True


def terminate_runtime():
    """
    Terminate runtime.

    In "remote" mode (see `init`), stop the worker process, which
    terminates its runtime. Handles to its objects are then invalid;
    a later call to `init` or `import_deployed` starts a new worker.
    The mode remains "remote".
    """
    if _MODE["mode"] == "remote":
        if "matlab_runtime.remote" in sys.modules:
            from . import remote
            remote.shutdown()
        return
    if _INITIALIZED["RUNTIME"]:
        cppext = importlib.import_module("matlabruntimeforpython_abi3")
        cppext.terminateApplication()
        _INITIALIZED["RUNTIME"] = False


def _cleanup():
    """
    Terminate the deployed packages that are still alive, then the
    runtime (registered with `atexit`).

    Safe to call several times: packages are only terminated once (the
    registry is emptied), and `terminate_runtime` does nothing once the
    runtime is terminated. It can therefore be called before `atexit`
    runs it, e.g. while signal handlers installed by the runtime are
    still in place (see `tests/conftest.py`).
    """
    packages = [ref() for ref in _DEPLOYED_MODULES.values()]
    _DEPLOYED_MODULES.clear()
    for package in packages:
        if package is not None:
            package.terminate()
    del packages
    terminate_runtime()


atexit.register(_cleanup)
