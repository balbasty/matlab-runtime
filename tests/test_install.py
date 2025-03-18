import os.path as op
from tempfile import gettempdir

from matlab_runtime_installer import install

# Use an installation prefix that does not require root access
tmp_prefix = op.join(gettempdir(), "MATLAB", "MATLAB_Runtime")


def test_install_r2024b():
    install("R2024b", prefix=tmp_prefix, auto_answer=True)


# def test_install_r2024a():
#     install("R2024a", prefix=tmp_prefix, auto_answer=True)
