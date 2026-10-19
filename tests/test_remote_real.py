"""matlab_runtime.remote with a real MATLAB Runtime (skipped otherwise)."""
import os.path as op
from tempfile import gettempdir

import pytest

from matlab_runtime import guess_arch, guess_prefix, remote
from matlab_runtime.utils import find_runtime

if guess_arch()[:3] == "mac":
    tmp_prefix = guess_prefix()
else:
    tmp_prefix = op.join(gettempdir(), "MATLAB", "MATLAB_Runtime")


@pytest.mark.skipif(
    not find_runtime("R2024b", tmp_prefix), reason="MATLAB Runtime R2024b"
)
def test_remote_import_r2024b():
    try:
        remote.init("R2024b", prefix=tmp_prefix)
        from . import _test_runtime
        mod = remote.import_deployed(_test_runtime)
        assert mod.test_runtime() == "success"
    finally:
        remote.shutdown()
