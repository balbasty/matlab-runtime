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
* [Out-of-process runtime (`matlab_runtime.remote`)](#out-of-process-runtime-matlab_runtimeremote)
* [Troubleshooting](#troubleshooting)
  * [MacOS](#macos)
  * [Windows](#windows)

## Installation

```shell
pip install matlab-runtime
```

## Command line tool

```text
usage: install_matlab_runtime [-h] [-v VERSION ...] [-p PREFIX] [-u] [-y]

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
```

## Python API

### Examples

Install a version of the runtime:

```python
from matlab_runtime import install, guess_prefix

version = "R2024b"
install(version, auto_answer=True)

print(guess_prefix())
```

Import a compiled MATLAB package:

```python
import my_matlab_project
from matlab_runtime import init, import_deployed

init("R2024b")  # Same version used when compiling the package

my_matlab_project = import_deployed(my_matlab_project)
my_matlab_project.my_function()
```

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

    If the environment variable `"MATLAB_RUNTIME_PATH"` is set, return it.

    Otherwise, the default prefix is platform-specific:

    * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
    * Linux:    /usr/local/MATLAB/MATLAB_Runtime
    * MacOS:    /Applications/MATLAB/MATLAB_Runtime

    Returns
    -------
    prefix : str
    """
    ...

def install(version=None, prefix=None, auto_answer=False):
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

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
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
):
    """
    Set current environment so that the MATLAB Python SDK is properly
    linked and usable.

    Parameters
    ----------
    version : str, default="latest_installed"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'latest_installed', use the most recent currently installed
        version. If no version is installed, equivalent to 'latest'.
    install_if_missing : bool
        If target version is missing, run installer.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    default_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    """
    ...

def import_deployed(*packages):
    """
    Initialize compiled MATLAB packages so that they can be used from python.

    Parameters
    ----------
    *packages : module | str
        Python package that contains a compiled MATLAB package (a ctf file).

    Returns
    -------
    *modules : module
        Imported MATLAB modules.
    """
    ...
```

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
  0 disables it).

`remote.import_deployed` starts the worker (with default options) if
`remote.init` has not been called. `remote.kill()` stops the worker
immediately, and `remote.is_alive()` tells whether it is running.

What comes back:

* MATLAB numeric and logical arrays are returned as numpy arrays
  (with at least two dimensions); numpy arrays are passed as the
  corresponding MATLAB arrays (`float64` -> `double`, `float32` ->
  `single`, `bool` -> `logical`, integers -> integers, complex ->
  complex). numpy is only needed in the client if arrays are exchanged.
* Strings, numbers, lists (cells), dicts (structs) and tuples are copied.
* Anything else (deployed packages, MATLAB objects, ...) stays in the
  worker and is returned as a `remote.RemoteHandle`, whose attributes and
  methods are accessed remotely. It can be passed back as an argument.
* Errors raised in the worker are raised again in the client, with the
  same type for builtin exceptions (`remote.RemoteError` otherwise) and
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
* On macOS, the runtime used by `mwpython` is found the same way as
  `mpython` does (standard locations, or `MATLAB_RUNTIME_PATH`).
* Still to be verified on macOS: array conversions with each MATLAB
  release, shared memory under `mwpython`, forwarding of output printed
  by MATLAB itself, and startup time.

## Troubleshooting

### MacOS

#### `mpython` / `mwpython`

The MATLAB SDK cannot be used with the normal python interpreter on MacOS.
Instead, the MATLAB runtime ships with its own interpreter called `mwpython`.

However, `mwpython` does not interface correctly with conda environments
(it overrides environement variables that cause compiled libraries
to not be correctly loaded). For example, `mwpython` crashes when
importing `scipy.sparse`.

Instead, we provide our own wrapper, `mpython`, which is automatically
installed with this package. It does solve the conda environement issue.

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
