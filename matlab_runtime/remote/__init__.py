"""
Out-of-process MATLAB runtime.

The MATLAB SDK runs in a separate worker process (started through
`mwpython` on macOS), so that compiled MATLAB packages can be used from
any interpreter, including Jupyter kernels, alongside packages such as
matplotlib.

```python
from matlab_runtime import remote

remote.init("R2024b")
mod = remote.import_deployed("my_matlab_project")
mod.my_function(np.ones([3, 3]))
```

Numbers, strings, numpy arrays (MATLAB arrays) and lists, tuples and
dicts of them are copied back; anything else (MATLAB objects, but also
python sets, ...) stays in the worker and is returned as a
`RemoteHandle`.

`matlab_runtime.init` and `matlab_runtime.import_deployed` use this
module in "remote" mode (the default on macOS outside `mwpython`, see
their `mode` argument).
"""
import atexit
import sys
import threading
import warnings

from .client import RemoteHandle, RemoteMethod, Session, _uses_mpython
from .protocol import RemoteError, RemoteTraceback, WorkerDiedError

__all__ = [
    "init",
    "import_deployed",
    "shutdown",
    "kill",
    "is_alive",
    "is_matlab_object",
    "RemoteHandle",
    "RemoteMethod",
    "RemoteError",
    "RemoteTraceback",
    "WorkerDiedError",
]

_SESSION = None
_LOCK = threading.Lock()
_ATEXIT = []
# Options that only matter when the worker is started
_PROCESS_OPTIONS = ("executable", "use_mpython", "start_timeout",
                    "shm_threshold")


def _differs(session, options, key):
    """Whether an option given explicitly differs from the session's."""
    value = options.get(key)
    if value is None:
        return False
    if key == "use_mpython":
        return _uses_mpython(options.get("executable"), value) != (
            _uses_mpython(session.executable, session.use_mpython)
        )
    return value != getattr(session, key)


def _session(**options):
    """
    Return the module-wide `Session`, starting it if needed.

    Parameters
    ----------
    **options
        Keyword arguments of `Session`. They are only used when a new
        session is created; if one is already running, they are ignored
        (with a warning if a process option differs from the running
        session's).

    Returns
    -------
    session : Session
        The running session. A new one is registered to be closed at
        interpreter exit (once).
    """
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            session = Session(**options)
            session.start()
            _SESSION = session
            if not _ATEXIT:
                atexit.register(shutdown)
                _ATEXIT.append(True)
        else:
            differ = [
                key for key in _PROCESS_OPTIONS
                if _differs(_SESSION, options, key)
            ]
            if differ:
                warnings.warn(
                    "The MATLAB runtime worker is already running: "
                    f"ignoring {', '.join(differ)}. Call "
                    "matlab_runtime.remote.shutdown() first to restart "
                    "it with other options.",
                    stacklevel=3,
                )
        return _SESSION


def _worker_python(executable=None, use_mpython=None):
    """Python version ("X.Y") of the worker, if known."""
    if executable is not None and not _uses_mpython(executable, use_mpython):
        return None  # another interpreter: it checks its own version
    return "{}.{}".format(*sys.version_info[:2])


def init(
    version="latest_installed",
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    *,
    default_scope=None,
    products=None,
    ctf=None,
    only_needed_products=False,
    patch=None,
    executable=None,
    use_mpython=None,
    start_timeout=600,
    shm_threshold=None,
):
    """
    Start the worker process and initialize the MATLAB SDK in it.

    Parameters
    ----------
    version : str, default="latest_installed"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'latest_installed', use the most recent installed release
        that supports the worker's Python (see `utils.resolve_runtime`).
        If none is installed, raise an error, unless `install_if_missing`,
        in which case the most recent release that supports it is
        installed.
    install_if_missing : bool
        If target version is missing, run installer (in this process).
    prefix : str, optional
        Location searched first (an installation prefix or a release
        folder), and install location (see `matlab_runtime.init`).
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    default_scope : {"user", "system", "custom"}, default="system"
        Install location chosen when `prefix` is not provided and the
        user just presses Enter (see `matlab_runtime.install`).
    products, ctf : [list of] str, optional
        Products that the runtime must provide, and compiled CTF
        archives whose required products it must provide
        (see `matlab_runtime.init`). They are checked (and installed)
        here, when the runtime is resolved here (see Notes).
    only_needed_products : bool
        When installing, only install `products` and the products
        required by `ctf` (see `matlab_runtime.init`).
    patch : bool, optional
        Patch the runtime if it is installed (MacOS only).

    Other Parameters
    ----------------
    executable : str, optional
        Python interpreter used to run the worker.
        Default: this interpreter (through `mwpython` on macOS).
    use_mpython : bool, optional
        Start the worker through `mwpython`. Default: True on macOS
        unless `executable` is given.
    start_timeout : float
        Maximum time (in seconds) given to the worker to start.
    shm_threshold : int, optional
        Arrays of at least this many bytes are exchanged through shared
        memory. 0 (or a negative value) disables shared memory.
        Default: environment variable `MATLAB_RUNTIME_SHM_THRESHOLD`,
        else 1 MiB. On Linux, segments live in `/dev/shm`, which is
        only 64 MB by default in Docker/Podman (`--shm-size=...` or
        `--ipc=host`) and Kubernetes (`emptyDir` with
        `medium: Memory` mounted at `/dev/shm`). Arrays that do not
        fit are sent inline instead (slower). Large results are views
        of their shared memory, and are passed back without a copy.

    Raises
    ------
    RuntimeNotFoundError
        If no matching runtime is installed (and not `install_if_missing`).
    UnsupportedPythonError
        If the requested release does not support the worker's Python.
    ValueError
        If another release of the SDK has already been initialized in
        the worker.
    WorkerDiedError
        If the worker exits while starting.
    TimeoutError
        If the worker does not connect within `start_timeout`.

    Notes
    -----
    If a worker is already running, it is reused: the process options
    (`executable`, `use_mpython`, `start_timeout`, `shm_threshold`) are
    ignored (with a warning if they differ from the running worker's)
    and only the SDK initialization request is sent again.

    When the worker goes through `mwpython`, or when the runtime may be
    installed, the installation is chosen here with
    `utils.resolve_runtime` (as `matlab_runtime.init` does), and that
    same installation provides `mwpython` and is initialized in the
    worker. Otherwise, the worker resolves `version` and `prefix` itself
    (with `matlab_runtime.init`).
    """
    version = version or "latest_installed"
    path = None
    mpython = _uses_mpython(executable, use_mpython)
    if mpython or install_if_missing:
        from ..cli import _has_mwpython
        from ..impl import _resolve_or_install
        # Install here, where the user can answer questions
        version, path = _resolve_or_install(
            version, prefix, install_if_missing, auto_answer,
            default_scope, products, ctf, only_needed_products, patch,
            python_version=_worker_python(executable, use_mpython),
            accept=_has_mwpython if mpython else None,
        )
    session = _session(
        executable=executable,
        use_mpython=use_mpython,
        variant=version,
        prefix=path or prefix,
        start_timeout=start_timeout,
        shm_threshold=shm_threshold,
    )
    session.request(
        "init", kwargs=dict(version=version, prefix=path or prefix)
    )


def _deployed_release(packages):
    """Release that the packages target (from their CTF), or None."""
    from ..impl import _deployed_release
    return _deployed_release(packages)


def import_deployed(
    *packages,
    option_list=tuple(),
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    default_scope=None,
    only_needed_products=False,
    products=None,
    patch=None,
):
    """
    Initialize compiled MATLAB packages in the worker.

    If no worker is running, one is started with `init` (and default
    process options), with the release that the packages were compiled
    with (read from their CTF archive here, as
    `matlab_runtime.import_deployed` does), or else with the most recent
    installed release. That release is installed (here) if it is
    missing and `install_if_missing` is set.

    The arguments `install_if_missing`, `prefix`, `auto_answer`,
    `default_scope`, `only_needed_products`, `products` and `patch` are
    those of `matlab_runtime.import_deployed`, and are only used if no
    worker is running.

    Parameters
    ----------
    *packages : module | str
        Python package that contains a compiled MATLAB package (a ctf
        file), or its name. It must be importable by the worker, which
        uses the same `sys.path` as this interpreter.
    option_list : list[str]
        Options passed to MATLAB when the runtime is initialized.

    Returns
    -------
    handles : RemoteHandle | tuple[RemoteHandle]
        Proxies to the imported MATLAB modules: a single handle if one
        package was given, a tuple of handles otherwise.

    Raises
    ------
    ImportError
        If a package cannot be imported in the worker.
    ValueError
        If the packages were compiled with a release that differs from
        the one initialized in the worker (or from each other).
    """
    if _SESSION is None:
        from ..impl import _init_options
        options = _init_options(
            packages,
            install_if_missing=install_if_missing,
            prefix=prefix,
            auto_answer=auto_answer,
            default_scope=default_scope,
            only_needed_products=only_needed_products,
            products=products,
            patch=patch,
        )
        init(_deployed_release(packages) or "latest_installed", **options)
    session = _session()
    handles = []
    for package in packages:
        if isinstance(package, str):
            name, file = package, None
        else:
            name, file = package.__name__, getattr(package, "__file__", None)
        handles.append(session.request(
            "import", name=name, file=file, option_list=list(option_list)
        ))
    return handles[0] if len(handles) == 1 else tuple(handles)


def _init_runtime(option_list=tuple(), error_if_already_init=False):
    """
    Initialize the MATLAB runtime in the worker (started if needed).

    Used by `matlab_runtime.init_runtime` in remote mode.
    """
    if _SESSION is None:
        init()
    _session().request(
        "init_runtime", option_list=list(option_list),
        error_if_already_init=bool(error_if_already_init),
    )


def is_matlab_object(x):
    """
    Whether `x` is a MATLAB object (e.g., a function handle).

    Works both with in-process objects (instances of `matlab.object`)
    and with handles to objects that live in the worker, so that code
    written for `isinstance(x, matlab.object)` (e.g., mpython) can
    support both modes.

    Parameters
    ----------
    x : object

    Returns
    -------
    bool
        True if `x` is a `RemoteHandle` to a `matlab.object`, or an
        instance of `matlab.object` (if the `matlab` module of the SDK
        is already imported).
    """
    if isinstance(x, RemoteHandle):
        return x._ref.type == "matlab.object"
    matlab = sys.modules.get("matlab")
    matlab_object = getattr(matlab, "object", None)
    return isinstance(matlab_object, type) and isinstance(x, matlab_object)


def shutdown():
    """
    Stop the worker (gracefully). A later call to `init` restarts it.

    Does nothing if no worker is running. It is registered with
    `atexit` the first time a worker is started.
    """
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


def kill():
    """
    Stop the worker immediately. A later call to `init` restarts it.

    Unlike `shutdown`, does not wait for a pending call to finish and
    does not ask the worker to exit: the process is terminated.
    Does nothing if no worker is running.
    """
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.kill()


def is_alive():
    """
    Whether a worker is running.

    Returns
    -------
    bool
        False before `init`, after `shutdown` or `kill`, and after the
        worker died unexpectedly.
    """
    session = _SESSION
    return session is not None and session.alive
