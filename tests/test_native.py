import subprocess
import sys
import textwrap
import warnings

import pytest

from matlab_runtime import native
from matlab_runtime.utils import guess_arch

IS_MAC = guess_arch()[:3] == "mac"

GUARDED_SOURCE = textwrap.dedent('''
    import sys

    def check():
        raise RuntimeError("Please use mwpython on this platform")

    if sys.platform == "linux" or sys.platform == "darwin" or True:
        raise RuntimeError("unsupported interpreter")

    def unrelated(x):
        if x < 0:
            raise ValueError("negative")
        return x
''')


@pytest.mark.skipif(IS_MAC, reason="sys.executable may be mwpython on mac")
def test_import_has_no_side_effects():
    code = textwrap.dedent('''
        import os, sys
        env = dict(os.environ)
        meta_path = list(sys.meta_path)
        import matlab_runtime.native as n
        assert "ctypes" not in sys.modules, "ctypes imported"
        assert dict(os.environ) == env, "environment modified"
        assert list(sys.meta_path) == meta_path, "meta_path modified"
        assert not n.STATE["in_runloop"] and n.STATE["mcrroot"] is None
    ''')
    subprocess.run([sys.executable, "-c", code], check=True)


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


def test_dyld_fallback_library_path():
    path = native.dyld_fallback_library_path("/R", "maca64", "/x::/usr/lib")
    parts = path.split(":")
    assert parts[:4] == [".", "/R/runtime/maca64", "/R/bin/maca64",
                         "/R/sys/os/maca64"]
    assert "/x" in parts and parts.count("/usr/lib") == 1


def test_find_guards_in_source():
    found = native.find_guards_in_source(GUARDED_SOURCE, "f.py", context=1)
    linenos = [f["lineno"] for f in found]
    assert 5 in linenos     # mwpython message
    assert 7 in linenos     # sys.platform test
    assert all(f["file"] == "f.py" for f in found)
    assert "unrelated" not in "".join(f["line"] for f in found)


def test_neutralize_source():
    code, report = native._neutralize_source(GUARDED_SOURCE, "f.py")
    assert sorted(lineno for lineno, _ in report) == [5, 8]
    ns = {}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        exec(code, ns)      # module-level guard no longer raises
        ns["check"]()       # function-level guard no longer raises
    assert len(w) == 2 and "mwpython guard" in str(w[0].message)
    # unrelated raise statements are untouched
    with pytest.raises(ValueError):
        ns["unrelated"](-1)


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
