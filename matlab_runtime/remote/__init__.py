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
"""
import atexit
import threading

from .client import RemoteHandle, RemoteMethod, Session
from .protocol import RemoteError, RemoteTraceback, WorkerDiedError

__all__ = [
    "init",
    "import_deployed",
    "shutdown",
    "kill",
    "is_alive",
    "RemoteHandle",
    "RemoteMethod",
    "RemoteError",
    "RemoteTraceback",
    "WorkerDiedError",
]

_SESSION = None
_LOCK = threading.Lock()
_ATEXIT = []


def _session(**options):
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            session = Session(**options)
            session.start()
            _SESSION = session
            if not _ATEXIT:
                atexit.register(shutdown)
                _ATEXIT.append(True)
        return _SESSION


def init(
    version="latest_installed",
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    *,
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
        If 'latest_installed', use the most recent currently installed
        version. If no version is installed, equivalent to 'latest'.
    install_if_missing : bool
        If target version is missing, run installer (in this process).
    prefix : str, optional
        Install location.
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**

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
        memory. 0 disables shared memory. Default: environment variable
        `MATLAB_RUNTIME_SHM_THRESHOLD`, else 1 MiB.
    """
    version = version or "latest_installed"
    variant = "latest_installed"
    if version != "latest_installed" or install_if_missing:
        from ..utils import guess_release, find_runtime
        release = guess_release(version, prefix=prefix)
        if version != "latest_installed":
            variant = release
        if install_if_missing and not find_runtime(release, prefix):
            # Install here, where the user can answer questions
            from ..impl import install
            install(release, prefix, auto_answer)
    session = _session(
        executable=executable,
        use_mpython=use_mpython,
        variant=variant,
        start_timeout=start_timeout,
        shm_threshold=shm_threshold,
    )
    session.request("init", kwargs=dict(version=version, prefix=prefix))


def import_deployed(*packages, option_list=tuple()):
    """
    Initialize compiled MATLAB packages in the worker.

    Starts the worker (with default options) if needed.

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
    *handles : RemoteHandle
        Proxies to the imported MATLAB modules.
    """
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


def shutdown():
    """Stop the worker (gracefully). A later call to `init` restarts it."""
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


def kill():
    """Stop the worker immediately. A later call to `init` restarts it."""
    global _SESSION
    with _LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.kill()


def is_alive():
    """Whether a worker is running."""
    session = _SESSION
    return session is not None and session.alive
