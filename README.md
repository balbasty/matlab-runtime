# Cross-platform installer for the MATLAB Runtime

This is a small package that simply allows to

* download and run the correct MATLAB Runtime installer for the current
  platform, _i.e._, wraps the steps described in
  [install-the-matlab-runtime](https://mathworks.com/help/compiler/install-the-matlab-runtime.html);
* correctly set the environement variables for deployment, _i.e._, wraps
  the steps described in
  [mcr-path-settings-for-run-time-deployment](https://uk.mathworks.com/help/compiler/mcr-path-settings-for-run-time-deployment.html);
* use the MATLAB SDK to import a compiled MATLAB package in Python, _i.e._,
  wraps the steps described in
  [import-compiled-python-packages](https://uk.mathworks.com/help/compiler_sdk/python/import-compiled-python-packages.html).

## TOC

* [Installation](#installation)
* [Command line tool](#command-line-tool)
* [Python API](#python-api)
  * [Examples](#examples)
  * [API](#api)
* [Runtime selection](#runtime-selection)
  * [Environment variables](#environment-variables)
* [Out-of-process runtime (`matlab_runtime.remote`)](#out-of-process-runtime-matlab_runtimeremote)
  * [Runtime mode](#runtime-mode)
* [Troubleshooting](#troubleshooting)
  * [MacOS](#macos)
  * [Windows](#windows)

## Installation

```shell
pip install matlab-runtime
```

## Command line tool

```text
usage: install_matlab_runtime [-h] [-v VERSION ...] [-p PREFIX] [-u] [-y] [-x]
                              [-f PRODUCT ...] [-c FILE ...]

Install any matlab runtime in any location.

options:

  -h, --help        Show this help message and exit

  -v, --version     Version of the runtime to [un]install,
                    such as 'latest' or 'R2022b' or '9.13'.
                    Default is 'all' if '--uninstall' else 'latest'.

  -p, --prefix      Installation prefix. Default:
                    * Windows:  C:\Program Files\MATLAB\MATLAB Runtime\
                    * Linux:    /usr/local/MATLAB/MATLAB_Runtime
                    * MacOS:    /Applications/MATLAB/MATLAB_Runtime

  -u, --uninstall   Uninstall this version of the runtime.
                    Use '--version all' to uninstall all versions.

  -y, --yes         Default answer (usually yes) to all questions.
                    BY USING THIS OPTION, YOU ACCEPT THE TERMS OF THE MATLAB
                    RUNTIME LICENSE. THE MATLAB RUNTIME INSTALLER WILL BE RUN
                    WITH THE ARGUMENT `-agreeToLicense yes`.
                    IF YOU ARE NOT WILLING TO DO SO, DO NOT CALL THIS FUNCTION.
                    https://mathworks.com/help/compiler/install-the-matlab-runtime.html

  -x, --patch       Patch the runtime if needed.

  -f, --products    Only install these products, by installer name
                    (e.g. 'MATLAB Runtime - Numerics'), short name
                    (e.g. 'Numerics') or ID (e.g. '35010').
                    Default: all products.

  -c, --ctf         Only install the products required by these compiled
                    CTF archives. Can be combined with '--products'.
```

Install only the products required by a compiled package:

```shell
install_matlab_runtime -v R2024b --ctf my_matlab_project.ctf
```

Temporary files (download, unpacked installer) are written to the system
temporary folder, which can be changed with the `TMPDIR`/`TMP`/`TEMP`
environment variables.

Product IDs listed in a CTF are mapped to installer product names using
the `productdata/` folder shipped with the installer. If an installer does
not support product selection (e.g., pre-R2019a Windows `.exe` installers),
a warning is raised and all products are installed.

Product names differ between releases (e.g., product `35010` is called
"MATLAB Runtime - Numerics" in R2024b but "MATLAB Base Runtime" in R2026a),
whereas product IDs are stable. Prefer IDs or CTF archives in scripts that
target several releases.

Partial installation is based on
[`install_runtime_products.py`](https://github.com/pranaysy/spm-python-setup/blob/main/install_runtime_products.py)
by [@pranaysy](https://github.com/pranaysy).

## Python API

### Examples

Install a version of the runtime:

```python
from matlab_runtime import install, guess_prefix

version = "R2024b"
install(version, auto_answer=True)

print(guess_prefix())
```

Install only a subset of products (by name, or required by CTF files):

```python
install("R2024b", products=["Numerics", "MATLAB Runtime - Python"], auto_answer=True)
install("R2024b", ctf=["path/to/my_matlab_project.ctf"], auto_answer=True)
```

Import a compiled MATLAB package:

```python
import my_matlab_project
from matlab_runtime import init, import_deployed

init("R2024b")  # Optional: same version used when compiling the package

my_matlab_project = import_deployed(my_matlab_project)
my_matlab_project.my_function()
```

If `init` has not been called, `import_deployed` initializes the release
that the package was compiled with (read from its CTF archive). It raises
an error if another release has already been initialized, since a
compiled package only runs with the MATLAB Runtime of its own release.

On macOS, unless python runs under `mwpython`/`mpython`, the same code
runs the MATLAB Runtime in a worker process (see
[Runtime mode](#runtime-mode)): functions then return numpy arrays,
numbers, strings, lists and dicts instead of `matlab.*` objects.

Note that `my_matlab_project` only needs to contain an (eventually empty)
`__init__.py` and the compiled CTF file with the same name as the module.
None of the other files spit out by the MATLAB compiler are requried.

```text
└── my_matlab_project/
    ├── __init__.py
    └── my_matlab_project.ctf
```

### API

```python
def guess_prefix():
    """
    Guess the MATLAB Runtime installation prefix.

    If the environment variable `"MATLAB_RUNTIME_PATH"` is set, return it
    (or its parent folder if it points to a release folder, i.e. a folder
    that contains `VersionInfo.xml`).

    Otherwise, the default prefix is platform-specific:

    * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
    * Linux:    /usr/local/MATLAB/MATLAB_Runtime
    * MacOS:    /Applications/MATLAB/MATLAB_Runtime

    Returns
    -------
    prefix : str
    """
    ...

def install(
    version=None,
    prefix=None,
    auto_answer=False,
    patch=None,
    products=None,
    ctf=None,
):
    """
    Install the matlab runtime.

    !!! warning
        BY SETTING `default_answer=True`, YOU ACCEPT THE TERMS OF THE
        MATLAB RUNTIME LICENSE. THE MATLAB RUNTIME INSTALLER WILL BE
        RUN WITH THE ARGUMENT `-agreeToLicense yes`.
        IF YOU ARE NOT WILLING TO DO SO, DO NOT CALL THIS FUNCTION.

        https://mathworks.com/help/compiler/install-the-matlab-runtime.html

    Parameters
    ----------
    version : [list of] str, default="latest"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    default_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    patch : bool
        Patch the runtime if needed (MacOS only).
    products : [list of] str, optional
        Only install these products. Products can be specified by
        installer name (e.g., 'MATLAB Runtime - Numerics'),
        short name (e.g., 'Numerics') or ID (e.g., '35010').
        Default: install all products.
    ctf : [list of] str, optional
        Only install the products required by these compiled CTF
        archives. Combined (union) with `products`.

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
    ValueError
        If a requested product is unknown.

    Warns
    -----
    UserWarning
        If products are requested but the installer does not
        support product selection (all products are then installed).
    """
    ...

def uninstall(version=None, prefix=None, auto_answer=False):
    """
    Uninstall the matlab runtime.

    Parameters
    ----------
    version : [list of] str, default="all"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'all', uninstall all installed versions.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    auto_answer : bool
        Default answer to all questions.
    """
    ...

def init(
    version="latest_installed",
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
    mode=None,
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
        folder), and install location. Default:
        * `$MATLAB_RUNTIME_PATH`, if set; else
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    auto_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    mode : {"inprocess", "remote", "auto"}, optional
        Where the MATLAB SDK runs (see "Runtime mode"). Default: the mode
        already used in this process, else `$MATLAB_RUNTIME_MODE`,
        else "auto" ("remote" on macOS unless running under mwpython,
        "inprocess" elsewhere).

    Raises
    ------
    ValueError
        If another release of the SDK has already been initialized.
    RuntimeError
        If the runtime is already used in another mode in this process.
    RuntimeNotFoundError
        If no matching runtime is installed (and not `install_if_missing`).
    UnsupportedPythonError
        If the requested release does not support the running Python
        (according to `utils.SUPPORTED_PYTHON_VERSIONS`).
    """
    ...

def import_deployed(*packages, option_list=tuple(), mode=None):
    """
    Initialize compiled MATLAB packages so that they can be used from python.

    If the MATLAB SDK is not initialized yet, it is initialized with the
    release that the packages were compiled with (read from their CTF
    archive), or else with the most recent installed release.

    Parameters
    ----------
    *packages : module | str
        Python package that contains a compiled MATLAB package (a ctf file).
    option_list : list[str]
        Options passed to MATLAB, if the runtime is not initialized yet.
    mode : {"inprocess", "remote", "auto"}, optional
        Where the MATLAB SDK runs (see `init` and "Runtime mode").

    Returns
    -------
    *modules : module | remote.RemoteHandle
        Imported MATLAB modules (handles to them in "remote" mode).
    """
    ...
```

## Runtime selection

`init()`, `import_deployed()` and `mpython` all choose a runtime the same
way (`matlab_runtime.utils.resolve_runtime`). The following locations are
searched, in order:

1. the `prefix` argument if provided, else `$MATLAB_RUNTIME_PATH` if set,
   else the default prefix (see `guess_prefix()`);
2. `$MATLAB_PATH`, if set;
3. standard locations of the MATLAB Runtime **and of full MATLAB
   installations**, e.g. `/usr/local/MATLAB/MATLAB_Runtime/*`,
   `/usr/local/MATLAB/*`, `/Applications/MATLAB_*.app`,
   `C:\Program Files\MATLAB\*`;
4. the installation that contains the `matlab` executable found on the
   `PATH`.

Each of these locations may be either a **prefix** (a folder that contains
release folders, such as `R2024b` or the legacy `v97`) or a **release
folder** (a folder that contains `VersionInfo.xml`). The release of each
installation is always read from its `VersionInfo.xml`, never guessed from
folder names.

* With an explicit version (e.g. `init("R2024b")` or `mpython -variant R2024b`),
  the first installation of that release is used.
* With `"latest_installed"` (the default), the most recent installed
  release is used. `init()` and `mpython` only consider releases that
  support the running Python (according to
  `utils.SUPPORTED_PYTHON_VERSIONS`; releases missing from that table
  are accepted, with a warning in `init()`). On ties, the location found
  first (_e.g._, `prefix`) wins. If `prefix` (or `$MATLAB_RUNTIME_PATH`)
  is a release folder, that installation is used, even if a more recent
  release is installed elsewhere.
* `import_deployed()` (without a prior `init()`) uses the release the
  package was compiled with.
* If nothing matches, a `RuntimeNotFoundError` (or `UnsupportedPythonError`)
  is raised, listing the installations that were found and the Python
  versions they support. Nothing is downloaded unless
  `install_if_missing=True`, in which case the requested release (or the
  most recent release that supports the running Python) is installed.

The list of installations can be obtained with
`matlab_runtime.utils.iter_installations()`.

### Environment variables

| Variable | Meaning |
|----------|---------|
| `MATLAB_RUNTIME_PATH` | Runtime location searched first: a prefix (folder that contains release folders) or a single release folder. It is also the default installation prefix (its parent folder, if it is a release folder). |
| `MATLAB_RUNTIME_MODE` | Default runtime mode: `inprocess`, `remote` or `auto` (see [Runtime mode](#runtime-mode)). |
| `MATLAB_PATH` | Additional location searched for a MATLAB or MATLAB Runtime installation (release folder or prefix). |
| `MATLAB_RUNTIME_OFFLINE` | If set to `1`, do not fetch the up-to-date release registry from GitHub on import; only use the registry bundled with the package. |
| `MATLAB_RUNTIME_INFO_TIMEOUT` | Maximum time (in seconds, default `3`) spent fetching the release registry on import. `0` is equivalent to `MATLAB_RUNTIME_OFFLINE=1`. |

The release registry (`info.json`: supported Python versions, update
numbers, ...) bundled with the package is updated, entry by entry, with
the latest registry published on GitHub. If it cannot be fetched in time,
the bundled registry is silently used.
## Out-of-process runtime (`matlab_runtime.remote`)

> **Experimental.**

`matlab_runtime.remote` runs the MATLAB SDK in a separate worker process
and talks to it from the current interpreter. On macOS, the worker is
started through `mwpython` (with the same environment fixes as
`mpython`), so compiled MATLAB packages can be used from the normal
python interpreter, from a Jupyter kernel (no `kernel.json` edit
needed), and alongside packages such as `matplotlib`.

```python
import numpy as np
import my_matlab_project
from matlab_runtime import remote

remote.init("R2024b")  # Same version used when compiling the package

my_matlab_project = remote.import_deployed(my_matlab_project)
y = my_matlab_project.my_function(np.ones([3, 3]))
a, b = my_matlab_project.my_other_function(y, nargout=2)

remote.shutdown()  # Optional: also done at exit
```

`remote.init` takes the same arguments as `init`, plus:

* `executable`: python interpreter used to run the worker
  (default: the current one, through `mwpython` on macOS);
* `use_mpython`: whether to go through `mwpython`
  (default: on macOS, unless `executable` is given);
* `start_timeout`: maximum time (in seconds) given to the worker to start;
* `shm_threshold`: arrays of at least that many bytes are exchanged through
  shared memory (default: `MATLAB_RUNTIME_SHM_THRESHOLD` or 1 MiB;
  0 or a negative value disables it). On Linux, shared memory lives in
  `/dev/shm`, which is half of the RAM on a normal machine but only
  64 MB by default in Docker/Podman (use `--shm-size=...` or
  `--ipc=host`) and Kubernetes (mount an `emptyDir` with
  `medium: Memory` at `/dev/shm`). When it is short of space, arrays
  are sent inline instead (slower, but it works). Large results are
  numpy views of their shared memory, which is freed once they (and
  their views) are gone, or when the worker stops; passing them, or
  contiguous slices of them, back to MATLAB copies nothing on the
  python side (in-place edits are seen). If the python process is
  killed hard, the segments of live results leak.

`remote.import_deployed` starts the worker (with default options) if
`remote.init` has not been called, with the release the package was
compiled with (read from its CTF archive, as `import_deployed`).
`remote.kill()` stops the worker immediately, and `remote.is_alive()`
tells whether it is running.

What comes back:

* MATLAB numeric and logical arrays are returned as numpy arrays
  (with at least two dimensions); numpy arrays are passed as the
  corresponding MATLAB arrays (`float64` -> `double`, `float32` ->
  `single`, `bool` -> `logical`, integers -> integers, complex ->
  complex). numpy is only needed in the client if arrays are exchanged.
* Strings, numbers, lists (cells), dicts (structs) and tuples are copied.
* Anything else (deployed packages, MATLAB objects, ...) stays in the
  worker and is returned as a `remote.RemoteHandle`, whose attributes and
  methods are accessed remotely. It can be passed back as an argument,
  and the worker then receives the original object.
  `remote.is_matlab_object(x)` tells whether `x` is a MATLAB object or
  function handle, be it a `RemoteHandle` or an in-process
  `matlab.object` (code that tests `isinstance(x, matlab.object)` can
  use it to support both).
  This includes python containers other than lists, tuples and dicts,
  such as sets (MATLAB never returns them; only values built in python
  by the package can be).
* Errors raised in the worker are raised again in the client, with the
  same type for builtin exceptions and those of `matlab_runtime.utils`
  (e.g. `RuntimeNotFoundError`), `remote.RemoteError` otherwise, and
  with the remote traceback attached as `__cause__`.
* Text printed by the worker is forwarded to `sys.stdout`/`sys.stderr`.
  `stdout=`/`stderr=` file-like arguments are filled when the call
  returns. If the worker dies, `remote.WorkerDiedError` is raised (with
  its last error messages); call `remote.shutdown()` and `remote.init()`
  to start a new one.

Limitations:

* One call at a time (calls from several threads wait for each other);
  `background=True` is not supported.
* Interrupting (Ctrl-C, or interrupting a Jupyter kernel) stops waiting
  for the result, but not the MATLAB computation: the next call waits for
  it to finish.
* Attributes of remote objects cannot be set, and names that start with
  an underscore are not forwarded.
* Arguments must be picklable; packages passed to `import_deployed` must
  be importable by the worker (it uses the same `sys.path`).
* On macOS (or with `install_if_missing=True`), the runtime is chosen in
  the client, as `init` does (see [Runtime selection](#runtime-selection),
  for the Python version of the worker), and that same installation
  provides `mwpython` and is initialized in the worker. Otherwise, the
  worker chooses it with `init`.
* Still to be verified on macOS: array conversions with each MATLAB
  release, shared memory under `mwpython`, forwarding of output printed
  by MATLAB itself, and startup time.

### Runtime mode

`init`, `import_deployed` and `init_runtime` take a `mode` argument:

* `"inprocess"`: the MATLAB SDK is loaded in the current process (the
  historical behaviour).
* `"remote"`: `init` calls `remote.init` (same arguments) and
  `import_deployed` returns `remote.import_deployed` handles: the SDK
  runs in a worker process. `init_runtime` initializes the runtime in
  the worker (starting it if needed), and `terminate_runtime` stops the
  worker (handles become invalid; the next `init`/`import_deployed`
  starts a new one).
* `"auto"` (default): `"remote"` on macOS, unless python already runs
  under `mwpython` (e.g. through `mpython`/`mwpython2`), and
  `"inprocess"` on Linux and Windows.

The mode is chosen from the `mode` argument, else the environment
variable `MATLAB_RUNTIME_MODE`, else `"auto"`, by the first of these
calls, and then fixed for the process: later calls without `mode` (or
with `"auto"`) use it, and asking for the other mode raises a
`RuntimeError` (as does `"remote"` once the SDK has been loaded
in-process). The worker always runs in-process.

**In remote mode, values come back converted** (see above): numpy arrays
instead of `matlab.double` & co, lists and dicts for cells and structs,
and `remote.RemoteHandle` for MATLAB objects and function handles (use
`remote.is_matlab_object(x)` rather than `isinstance(x, matlab.object)`).
Code that relies on `matlab.*` types should use `mode="inprocess"`
(under `mpython` on macOS).

## Troubleshooting

### MacOS

#### `mpython` / `mwpython`

The MATLAB SDK cannot be used with the normal python interpreter on MacOS.
Instead, the MATLAB runtime ships with its own interpreter called `mwpython`.

By default, `init` and `import_deployed` therefore run the MATLAB SDK in
a worker process started through `mwpython` when they are called from
the normal interpreter (see [Runtime mode](#runtime-mode)), so that
`mpython` is not needed. The rest of this section applies to the
in-process mode.

However, `mwpython` does not interface correctly with conda environments
(it overrides environement variables that cause compiled libraries
to not be correctly loaded). For example, `mwpython` crashes when
importing `scipy.sparse`.

Instead, we provide our own wrapper, `mpython`, which is automatically
installed with this package. It does solve the conda environement issue.

If the requested runtime (`-variant`, or the most recent release that
supports the running python) is not installed, `mpython` offers to
install it when run from an interactive terminal (unless
`MATLAB_RUNTIME_OFFLINE=1`); otherwise it prints the
`install_matlab_runtime --version ...` command to run.

That said, the `matplotlib` package still cannot be used with this wrapper
(nor can it be used with MATLAB's `mwpython`). To use both, see
[`matlab_runtime.remote`](#out-of-process-runtime-matlab_runtimeremote).

#### Jupyter + libcrypto

When running jupyter, you may face an error related to `libcrypto.3.dylib`.
If this happens, you may want to try running the runtime installation
with the `--patch` option (or `patch=True`). **Warning:** this option
modifies files whithin the MATLAB runtime installation folder, which
may have unexpected effects. This is not a robust/thoroughly tested fix.

#### Jupyter + `mpython`

The simplest option is to use
[`matlab_runtime.remote`](#out-of-process-runtime-matlab_runtimeremote),
which does not require any change to the kernel. Otherwise:

By default, jupyter runs its kernel through the "normal" python interpreter.
In order to use the MATLAB packages in the kernel, it is necessary to
inform jupyter that it should run its kernels through `mpython`.

To do so, locate the kernel file(s), usually at
`/Users/{username}/Library/Jupyter/kernels/{kernel_name}/kernel.json`, and
replace the path to the default interpreter (_e.g._,
`"/Users/{username}/miniforge3/envs/{env_name}/bin/python"`) with the path
to `mpython` (_e.g._,
`"/Users/{username}/miniforge3/envs/{env_name}/bin/mpython"`).

If you cannot locate the kerenel file, install the kernel
by running:

```shell
mpython -m ipykernel install --user --name {kernel_name}
```

You may need to install `ipykernel` beforehand.

### Windows

#### `DeclarativeService.dll not found`

Python installation made from Microsoft Store on Windows will not work
(raises DeclarativeService.dll not found), install it from Python website.
