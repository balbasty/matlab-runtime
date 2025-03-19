import os.path as op
from tempfile import gettempdir

from matlab_runtime import install, guess_arch, guess_prefix

if guess_arch()[:3] == "mac":
    # Use default prefix so that we can easily call mwpython2 later
    tmp_prefix = guess_prefix()
else:
    # Use an installation prefix that does not require root access
    tmp_prefix = op.join(gettempdir(), "MATLAB", "MATLAB_Runtime")


def test_install_r2024b():
    install("R2024b", prefix=tmp_prefix, auto_answer=True)
