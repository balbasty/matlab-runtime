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

## Installation

```shell
pip install git+https://github.com/balbasty/matlab-runtime
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

  -p, --patch       Patch the runtime if needed.
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

## Troubleshooting

### MacOS

#### `mwpython2`

The MATLAB SDK cannot be used with the normal python interpreter on MacOS.
Instead, the MATLAB runtime ships with its own interpreter called `mwpython`.

However, `mwpython` does not interface correctly with conda environments
(it overrides environement variables that cause compiled libraries
to not be correctly loaded). For example, `mwpython` crashes when
importing `scipy.sparse`.

Instead, we provide our own wrapper, `mwpython2`, which is automatically
installed with this package. It does solve the conda environement issue.

That said, the `matplotlib` package still cannot be used with this wrapper
(nor can it be used with `mwpython`).

#### Jupyter + libcrypto

When running jupyter, you may face an error related to `libcrypto.3.dylib`.
If this happens, you may want to try running the runtime installation
with the `--patch` option (or `patch=True`). **Warning:** this option
modifies files whithin the MATLAB runtime installation folder, which
may have unexpected effects. This is not a robust/thoroughly tested fix.

#### Jupyter + `mwpython2`

By default, jupyter runs its kernel through the "normal" python interpreter.
In order to use the MATLAB packages in the kernel, it is necessary to
inform jupyter that it should run its kernels through `mwpython2`.

To do so, locate the kernel file(s), usually at
`/Users/{username}/Library/Jupyter/kernels/{kernel_name}/kernel.json`, and
replace the path to the default interpreter (_e.g._,
`"/Users/{username}/miniforge3/envs/{env_name}/bin/python"`) with the path
to `mwpython2` (_e.g._,
`"/Users/{username}/miniforge3/envs/{env_name}/bin/mwpython2"`).

If you cannot locate the kerenel file, install the kernel
by running:

```shell
mwpython2 -m ipykernel install --user --name {kernel_name}
```

You may need to install `ipykernel` beforehand.
