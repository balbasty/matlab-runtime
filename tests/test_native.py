import importlib
import os
import sys
import textwrap
import types
import warnings
import weakref

import pytest

import matlab_runtime
from matlab_runtime import impl, native
from matlab_runtime.utils import guess_arch

IS_MAC = guess_arch()[:3] == "mac"

GUARDED_SOURCE = textwrap.dedent('''
    import sys

    def check():
        raise RuntimeError("Please use mwpython on this platform")  # G1

    if "MWPYTHON" not in sys.executable.upper() or True:
        raise RuntimeError("unsupported interpreter")  # G2

    def check_exit():
        sys.exit("This must be run with mwpython")  # G3

    def darwin_only():
        if sys.platform == "darwin" or True:
            raise RuntimeError("not supported on mac")  # KEEP-DARWIN

    def reraise():
        try:
            raise KeyError("inner")
        except KeyError:
            if "mwpython" in sys.executable or True:
                raise  # KEEP-BARE

    def unrelated(x):
        if x < 0:
            raise ValueError("negative")
        return x
''')


def _line(marker):
    for i, line in enumerate(GUARDED_SOURCE.splitlines()):
        if marker in line:
            return i + 1
    raise AssertionError(marker)


def test_import_has_no_side_effects(monkeypatch):
    import ctypes

    def no_load(*a, **k):
        raise AssertionError("MATLAB library loaded at import")

    monkeypatch.setattr(ctypes, "CDLL", no_load)
    monkeypatch.setattr(ctypes.cdll, "LoadLibrary", no_load)
    # Import a fresh copy; the original module is restored afterwards.
    monkeypatch.delitem(sys.modules, "matlab_runtime.native")
    monkeypatch.setattr(matlab_runtime, "native", native)
    env = dict(os.environ)
    meta_path = list(sys.meta_path)
    n = importlib.import_module("matlab_runtime.native")
    assert n is not native
    assert dict(os.environ) == env, "environment modified"
    assert list(sys.meta_path) == meta_path, "meta_path modified"
    assert not n.STATE["active"] and not n.STATE["in_runloop"]
    assert n.STATE["mcrroot"] is None and n._BYPASS["finder"] is None


def test_parse_defaults():
    o = native._parse([])
    assert o["target"] == ("repl", None, [])
    assert o["variant"] == "latest_installed"
    assert o["options"] == []
    assert not any(o[k] for k in ("verbose", "bypass_guard",
                                  "diagnose_guard", "no_runloop",
                                  "no_reexec", "hard_exit", "help"))


def test_parse_flags_and_script():
    o = native._parse([
        "-verbose", "-variant", "R2024b", "-nojvm",
        "-mlstartup", "-nojvm, -nodisplay", "-bypass-guard",
        "-diagnose-guard", "-no-reexec", "-hard-exit",
        "script.py", "-verbose", "arg",
    ])
    assert o["verbose"] and o["bypass_guard"] and o["diagnose_guard"]
    assert o["no_reexec"] and o["hard_exit"]
    assert o["variant"] == "R2024b"
    assert o["options"] == ["-nojvm", "-nodisplay"]
    # arguments after the target belong to the target
    assert o["target"] == ("script", "script.py", ["-verbose", "arg"])


@pytest.mark.parametrize("flag", ["-c", "-m"])
def test_parse_c_m(flag):
    o = native._parse([flag, "value", "a", "-b"])
    assert o["target"] == (flag[1], "value", ["a", "-b"])


@pytest.mark.parametrize("args", [["-c"], ["-m"], ["-variant"],
                                  ["-unknown-option"]])
def test_parse_errors(args):
    with pytest.raises(ValueError):
        native._parse(args)
    assert native.main(args) == 2


def test_help(capsys):
    assert native.main(["-help"]) == 0
    assert "mpython-native" in capsys.readouterr().out


@pytest.mark.skipif(IS_MAC, reason="launcher is allowed to run on mac")
def test_non_mac_exit_code(capsys):
    assert native.main(["-c", "pass"]) == 10
    assert "macOS" in capsys.readouterr().err
    assert not native.STATE["active"]


def test_dyld_fallback_library_path():
    path = native.dyld_fallback_library_path("/R", "maca64", "/x::/usr/lib")
    parts = path.split(":")
    assert parts[:3] == ["/R/runtime/maca64", "/R/bin/maca64",
                         "/R/sys/os/maca64"]
    assert "/x" in parts and parts.count("/usr/lib") == 1
    # never the current directory (dylib hijack)
    assert "." not in parts and "" not in parts
    path = native.dyld_fallback_library_path("/R", "maca64", ".:/y")
    assert "." not in path.split(":")


def test_find_guards_in_source():
    found = native.find_guards_in_source(GUARDED_SOURCE, "f.py", context=1)
    linenos = [f["lineno"] for f in found]
    assert _line("G1") in linenos                 # mwpython message
    assert _line("KEEP-DARWIN") - 1 in linenos    # sys.platform test
    assert all(f["file"] == "f.py" for f in found)
    assert "unrelated" not in "".join(f["line"] for f in found)


def test_neutralize_source():
    code, report = native._neutralize_source(GUARDED_SOURCE, "f.py")
    assert sorted(lineno for lineno, _ in report) == sorted(
        _line(m) for m in ("G1", "G2", "G3"))
    ns = {}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        exec(code, ns)      # module-level guard (if-test) no longer raises
        ns["check"]()       # mwpython message no longer raises
        ns["check_exit"]()  # sys.exit("... mwpython") no longer exits
    assert len(w) == 3 and "mwpython guard" in str(w[0].message)
    # darwin-guarded raise without mwpython in its message: untouched
    with pytest.raises(RuntimeError, match="not supported on mac"):
        ns["darwin_only"]()
    # bare re-raise (even under an mwpython if-test): untouched
    with pytest.raises(KeyError):
        ns["reraise"]()
    # unrelated raise statements are untouched
    with pytest.raises(ValueError):
        ns["unrelated"](-1)


def test_neutralize_main_thread_message_untouched():
    src = "raise RuntimeError('must run on the main thread')\n"
    _, report = native._neutralize_source(src, "f.py")
    assert report == []


def test_install_guard_bypass(tmp_path, monkeypatch):
    pkg = tmp_path / "fake_native_pysdk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(GUARDED_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        native.install_guard_bypass(verbose=False,
                                    packages=("fake_native_pysdk",))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import fake_native_pysdk
        assert fake_native_pysdk.unrelated(1) == 1
    finally:
        native.uninstall_guard_bypass()
        sys.modules.pop("fake_native_pysdk", None)
    assert native._BYPASS["finder"] is None


@pytest.mark.parametrize("code,expected", [
    (None, 0), (0, 0), (3, 3), (255, 255), (256, 255), (512, 255),
    (-1, 1), ("message", 1),
])
def test_exit_code_never_wraps_to_zero(code, expected, capsys):
    assert native._exit_code(code) == expected


def test_join_user_threads_waits_for_non_daemon():
    import threading
    import time
    done = []

    def work():
        time.sleep(0.3)
        done.append(1)

    threading.Thread(target=work, daemon=False).start()
    threading.Thread(target=time.sleep, args=(5,), daemon=True).start()
    t0 = time.time()
    native._join_user_threads()
    assert done == [1]
    assert time.time() - t0 < 4  # daemon threads are not waited for


@pytest.mark.skipif(sys.platform == "win32",
                    reason="set_wakeup_fd needs a socket on Windows")
def test_sigint_forwarder_releases_fds():
    import signal
    old_handler = signal.getsignal(signal.SIGINT)
    fwd = native._SigintForwarder()
    fwd.install()
    if not fwd.installed:
        pytest.skip("not on the main thread")
    fds = fwd._fds
    fwd.uninstall()
    assert not fwd.installed and fwd._fds == ()
    assert signal.getsignal(signal.SIGINT) is old_handler
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


# ----------------------------------------------------------------------
#   matlab_runtime.impl hooks (only active inside the launcher)
# ----------------------------------------------------------------------


@pytest.fixture
def launcher(monkeypatch):
    def set_active(active):
        monkeypatch.setitem(native.STATE, "active", active)
        monkeypatch.setitem(native.STATE, "in_runloop", active)
    set_active(False)
    return set_active


@pytest.fixture
def fake_cppext(monkeypatch):
    calls = []
    mod = types.ModuleType(impl._CPP)
    mod.initializeApplication = lambda opts: calls.append(("init", opts))
    mod.terminateApplication = lambda: calls.append(("term",))
    monkeypatch.setitem(sys.modules, impl._CPP, mod)
    monkeypatch.setitem(impl._INITIALIZED, "SDK", "R2024b")
    monkeypatch.setitem(impl._INITIALIZED, "RUNTIME", False)
    monkeypatch.setattr(impl, "guess_arch", lambda: "glnxa64")
    monkeypatch.setenv("DISPLAY", ":0")
    return calls


@pytest.mark.parametrize("active", [False, True])
def test_impl_startup_options_only_in_launcher(
    active, launcher, fake_cppext, monkeypatch
):
    monkeypatch.setenv("MATLAB_RUNTIME_NATIVE_STARTUP_OPTIONS",
                       "-nojvm, -logfile")
    launcher(active)
    impl.init_runtime(["-logfile"])
    expected = ("-logfile", "-nojvm") if active else ["-logfile"]
    assert fake_cppext == [("init", expected)]


@pytest.mark.parametrize("active", [False, True])
def test_impl_guard_env_only_in_launcher(active, launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(native, "diagnose_guard",
                        lambda path: calls.append(("diagnose", path)))
    monkeypatch.setattr(native, "install_guard_bypass",
                        lambda: calls.append(("bypass",)))
    monkeypatch.setenv("MATLAB_RUNTIME_NATIVE_DIAGNOSE_GUARD", "1")
    monkeypatch.setenv("MATLAB_RUNTIME_NATIVE_BYPASS_GUARD", "1")
    launcher(active)
    impl._native_guard_hooks("/R")
    assert calls == ([("diagnose", "/R"), ("bypass",)] if active else [])


def test_impl_launcher_state(launcher, monkeypatch):
    monkeypatch.setitem(native.STATE, "release", "R2024b")
    assert impl._native_launcher_state() == {}   # imported, not running
    launcher(True)
    assert impl._native_launcher_state() is native.STATE


def test_impl_cleanup_idempotent(fake_cppext, monkeypatch):
    class Package:
        terminated = 0

        def terminate(self):
            Package.terminated += 1

    pkg = Package()
    monkeypatch.setattr(impl, "_DEPLOYED_MODULES",
                        {"pkg": weakref.ref(pkg)})
    impl._INITIALIZED["RUNTIME"] = True
    impl._cleanup()
    impl._cleanup()
    assert Package.terminated == 1
    assert fake_cppext == [("term",)]
    assert not impl._INITIALIZED["RUNTIME"]
