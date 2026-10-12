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
                              [-f PRODUCT ...] [-c FILE ...] [-t TMPDIR]

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

  -t, --tmpdir      Folder for temporary files (download, unpacked
                    installer, installer temporary files).
                    Default: system temporary folder.
```

Install only the products required by a compiled package, using a
custom temporary folder:

```shell
install_matlab_runtime -v R2024b --ctf my_matlab_project.ctf --tmpdir /scratch/tmp
```

Product IDs listed in a CTF are mapped to installer product names using
the `productdata/` folder shipped with the installer. If an installer does
not support product selection (e.g., pre-R2019a Windows `.exe` installers),
an error is raised rather than installing all products.

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
install("R2024b", ctf="my.ctf", tmpdir="/scratch/tmp", auto_answer=True)
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

def install(
    version=None,
    prefix=None,
    auto_answer=False,
    patch=None,
    products=None,
    ctf=None,
    tmpdir=None,
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
    tmpdir : str, optional
        Folder in which temporary files (download, unpacked installer,
        installer temporary files) are written.
        Default: system default (see `tempfile.gettempdir()`).

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
    ValueError
        If a requested product is unknown.
    RuntimeError
        If products are requested but the installer does not
        support product selection.
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
(nor can it be used with MATLAB's `mwpython`).

#### Jupyter + libcrypto

When running jupyter, you may face an error related to `libcrypto.3.dylib`.
If this happens, you may want to try running the runtime installation
with the `--patch` option (or `patch=True`). **Warning:** this option
modifies files whithin the MATLAB runtime installation folder, which
may have unexpected effects. This is not a robust/thoroughly tested fix.

#### Jupyter + `mpython`

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
