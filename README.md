# Cross-platform installer for the MATLAB runtime

## Installation

```shell
pip install matlab-runtime-installer @ https://github.com/balbasty/matlab-runtime-installer
```

## Command line tool

```shell
install_matlab_runtime [--version VERSION] [--prefix PREFIX] [--uninstall] [--yes]
```

## Python API

```python
from matlab_runtime_installer import install

version = "R2024b"
prefix = install(version, prefix=None, auto_answer=True)

print(prefix)
```
