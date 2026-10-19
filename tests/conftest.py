"""Session-wide hooks.

Terminate the in-process MATLAB runtime (initialized, e.g., by
test_import.py) at the end of the session, while the signal handlers it
installed are still in place.

pytest's faulthandler plugin enables `faulthandler` in `pytest_configure`
and disables it in `pytest_unconfigure`, which restores the SIGSEGV (and
SIGBUS, SIGFPE, ...) handlers that were installed *before* the session:
the handlers that the MATLAB runtime (its JVM) installed during the
session are silently replaced by the default action. `impl`'s atexit
handler, which runs after `pytest_unconfigure`, then shuts the runtime
down without them; the JVM uses SIGSEGV internally (safepoint polls,
implicit null checks), so the process can die with a plain
"Segmentation fault" after all tests passed, depending on timing.
"""
import sys

import pytest


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # After the session fixtures are torn down (runner's own
    # pytest_sessionfinish), before pytest_unconfigure.
    impl = sys.modules.get("matlab_runtime.impl")
    if impl is not None:
        impl._cleanup()
