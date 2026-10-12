"""
EXPERIMENTAL -- pure-Python replacement for MathWorks' `mwpython` (macOS).

Background
----------
On macOS, the MATLAB Runtime (its JVM / AppKit integration) requires the
process *main thread* to run a CoreFoundation run loop. MathWorks'
`mwpython` launcher therefore runs the CFRunLoop on the main thread and
the Python interpreter on a secondary thread. The C/C++ SDK exposes the
same mechanism through

    int mclRunMain(mclMainFcnType run_main, int argc, const char **argv);

This module does the same thing from a regular Python interpreter:

1. locate the runtime (`$MCRROOT`) and set `DYLD_FALLBACK_LIBRARY_PATH`
   (dyld only reads `DYLD_*` variables at process start, so the current
   interpreter is re-executed once with the right environment);
2. `ctypes`-load `libmwmclmcrrt.dylib` by absolute path and call
   `mclmcrInitialize()` (proxy layer initialisation);
3. call `mclRunMain(callback, argc, argv)` from the main thread. The
   callback runs the user target (script / `-m module` / `-c cmd` / REPL)
   with `runpy` on the secondary thread created by the runtime, and
   returns an exit code.

Known limitations
-----------------
* Python signal handlers only run on the main thread, which is blocked
  inside `mclRunMain`. Ctrl-C is forwarded on a best-effort basis: a
  wakeup fd + watcher thread raises `KeyboardInterrupt` asynchronously
  in the worker thread (delivered only when the worker executes Python
  bytecode, i.e. not while it is blocked inside a MATLAB call). A second
  Ctrl-C hard-exits the process (exit code 130).
* User code runs on a non-main thread: `signal.signal(...)` raises
  `ValueError`, and libraries that require the main thread (e.g. GUI
  toolkits such as matplotlib's macosx backend) will not work.
* Interpreters under SIP-protected paths (e.g. `/usr/bin/python3`) have
  `DYLD_*` variables stripped; use a conda/homebrew/python.org Python.

Importing this module has no side effects (no library is loaded).

Environment variables understood by this module and by `matlab_runtime.impl`
-----------------------------------------------------------------------------
MATLAB_RUNTIME_STARTUP_OPTIONS
    Comma-separated runtime startup options (e.g. "-nojvm") merged into the
    `option_list` of `matlab_runtime.init_runtime`.
MATLAB_RUNTIME_BYPASS_GUARD
    If truthy, `matlab_runtime.init` installs the (opt-in) in-memory
    neutralizer of the "must run under mwpython" guard of the MathWorks
    Python SDK (see `install_guard_bypass`).
MATLAB_RUNTIME_DIAGNOSE_GUARD
    If truthy, `matlab_runtime.init` prints the guard-like code it finds in
    the MathWorks Python SDK sources (see `diagnose_guard`).
MATLAB_RUNTIME_MCLMCR_MODE
    Integer passed to `mclmcrInitialize2(int)` if the library does not
    export a plain `mclmcrInitialize()` symbol (default: 0).
"""
import os
import os.path as op
import re
import sys

__all__ = [
    "main",
    "find_mcrroot",
    "find_mclmcrrt",
    "dyld_fallback_library_path",
    "run_in_runloop",
    "diagnose_guard",
    "install_guard_bypass",
    "uninstall_guard_bypass",
    "find_guards_in_source",
    "startup_options_from_env",
]

ENV_REEXEC = "_MATLAB_RUNTIME_NATIVE_REEXEC"
ENV_OPTIONS = "MATLAB_RUNTIME_STARTUP_OPTIONS"
ENV_BYPASS = "MATLAB_RUNTIME_BYPASS_GUARD"
ENV_DIAGNOSE = "MATLAB_RUNTIME_DIAGNOSE_GUARD"
ENV_MCLMCR_MODE = "MATLAB_RUNTIME_MCLMCR_MODE"

# Process-wide state (read by `matlab_runtime.impl`).
STATE = {
    "in_runloop": False,    # True while user code runs inside mclRunMain
    "mcrroot": None,        # Runtime selected by the launcher
    "release": None,        # Release (e.g. "R2024b") selected by launcher
    "worker_ident": None,   # threading ident of the worker thread
}

_PREFIX = "[mpython-native]"


def _log(*msg, verbose=True):
    if verbose:
        print(_PREFIX, *msg, file=sys.stderr, flush=True)


def _truthy(value):
    return str(value or "").strip().lower() not in ("", "0", "false", "no")


# ----------------------------------------------------------------------
#   STARTUP OPTIONS
# ----------------------------------------------------------------------


def _split_options(value):
    if not value:
        return []
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _merge_options(*lists):
    out = []
    for lst in lists:
        for opt in lst:
            if opt not in out:
                out.append(opt)
    return out


def startup_options_from_env():
    """Return the startup options stored in MATLAB_RUNTIME_STARTUP_OPTIONS."""
    return _split_options(os.environ.get(ENV_OPTIONS, ""))


# ----------------------------------------------------------------------
#   RUNTIME DISCOVERY
# ----------------------------------------------------------------------


def _lib_ext():
    return ".dylib" if sys.platform == "darwin" else ".so"


def find_mcrroot(variant="latest_installed", mcrroot=None, arch=None):
    """
    Locate a runtime installation that contains `runtime/<arch>`.

    Returns
    -------
    mcrroot : str
    release : str or None
    """
    from .utils import (
        guess_arch, iter_existing_installations, find_runtime,
        guess_matlab_release,
    )
    arch = arch or guess_arch()

    def _release(path, ver=None):
        if ver and re.fullmatch(r"R\d{4}[ab]", ver):
            return ver
        try:
            return guess_matlab_release(path)
        except Exception:
            return None

    if mcrroot:
        mcrroot = op.realpath(op.abspath(mcrroot))
        if not op.isdir(op.join(mcrroot, "runtime", arch)):
            raise RuntimeError(
                f"Not a MATLAB Runtime root (no runtime/{arch}): {mcrroot}"
            )
        return mcrroot, _release(mcrroot)

    for path, ver in iter_existing_installations(variant):
        if op.isdir(op.join(path, "runtime", arch)):
            return path, _release(path, ver)

    if variant and variant != "latest_installed":
        path = find_runtime(variant)
        if path and op.isdir(op.join(path, "runtime", arch)):
            return path, _release(path)

    raise RuntimeError(
        "No MATLAB Runtime found. If you have installed Matlab Runtime "
        "in an unusual location, use -mcrroot or set the "
        "MATLAB_RUNTIME_PATH environment variable to the path of the "
        "installation (e.g. /Applications/MATLAB/MATLAB_Runtime/R2024b)."
    )


def dyld_fallback_library_path(mcrroot, arch, existing=None):
    """Same search path as `mpython` (matlab_runtime.cli)."""
    existing = existing if existing is not None else os.environ.get(
        "DYLD_FALLBACK_LIBRARY_PATH", ""
    )
    existing = [x for x in existing.split(os.pathsep) if x]
    paths = [
        ".",
        f"{mcrroot}/runtime/{arch}",
        f"{mcrroot}/bin/{arch}",
        f"{mcrroot}/sys/os/{arch}",
        op.join(sys.prefix, "lib"),
        *existing,
        "/usr/local/lib",
        "/usr/lib",
    ]
    return os.pathsep.join(_merge_options(paths))


def find_mclmcrrt(mcrroot, arch):
    """Absolute path to libmwmclmcrrt (unversioned name preferred)."""
    import glob
    ext = _lib_ext()
    dirs = [op.join(mcrroot, "runtime", arch), op.join(mcrroot, "bin", arch)]
    for d in dirs:
        p = op.join(d, "libmwmclmcrrt" + ext)
        if op.exists(p):
            return p
    for d in dirs:
        # e.g. libmwmclmcrrt.24.2.dylib / libmwmclmcrrt.so.24.2
        hits = sorted(glob.glob(op.join(d, "libmwmclmcrrt*" + ext + "*")))
        hits += sorted(glob.glob(op.join(d, "libmwmclmcrrt*" + ext)))
        if hits:
            return hits[-1]
    raise FileNotFoundError(
        f"libmwmclmcrrt{ext} not found under {dirs}"
    )


def _python_on_sip_path(exe):
    exe = op.realpath(exe)
    return exe.startswith(("/usr/bin/", "/bin/", "/sbin/", "/usr/sbin/",
                           "/System/"))


def _package_parent():
    return op.dirname(op.dirname(op.abspath(__file__)))


def _needs_pythonpath(parent):
    try:
        import site
        sp = list(site.getsitepackages())
        sp.append(site.getusersitepackages())
    except Exception:
        sp = []
    sp = [op.realpath(x) for x in sp]
    return op.realpath(parent) not in sp


# ----------------------------------------------------------------------
#   MCLMCR (ctypes)
# ----------------------------------------------------------------------


def _load_mclmcrrt(path, verbose):
    import ctypes
    mode = getattr(ctypes, "RTLD_GLOBAL", 0)
    _log(f"Loading {path} (RTLD_GLOBAL)", verbose=verbose)
    return ctypes.CDLL(path, mode=mode)


def _mclmcr_initialize(lib, verbose):
    import ctypes
    try:
        f = lib.mclmcrInitialize
        f.argtypes = []
        f.restype = ctypes.c_bool
        ok = f()
        _log(f"mclmcrInitialize() -> {ok}", verbose=verbose)
        return ok
    except AttributeError:
        pass
    try:
        f = lib.mclmcrInitialize2
    except AttributeError:
        _log("WARNING: neither mclmcrInitialize nor mclmcrInitialize2 is "
             "exported; continuing without proxy-layer initialization.")
        return None
    f.argtypes = [ctypes.c_int]
    f.restype = ctypes.c_bool
    mode = int(os.environ.get(ENV_MCLMCR_MODE, "0"))
    ok = f(mode)
    _log(f"mclmcrInitialize2({mode}) -> {ok}", verbose=verbose)
    return ok


def _exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _fresh_main():
    import types
    mod = types.ModuleType("__main__")
    mod.__dict__["__builtins__"] = __builtins__
    sys.modules["__main__"] = mod
    return mod


def _run_target(target, verbose=False):
    """
    Run the user target in the current thread and return an exit code.

    target : (kind, value, rest) with kind in {"c", "m", "script", "repl"}
    """
    import runpy
    import traceback
    kind, value, rest = target
    try:
        if kind == "c":
            sys.argv = ["-c", *rest]
            if sys.path:
                sys.path[0] = ""
            code = compile(value, "<string>", "exec")
            exec(code, _fresh_main().__dict__)
        elif kind == "m":
            sys.argv = [value, *rest]
            runpy.run_module(value, run_name="__main__", alter_sys=True)
        elif kind == "script":
            sys.argv = [value, *rest]
            script_dir = op.dirname(op.abspath(value))
            if sys.path:
                sys.path[0] = script_dir
            else:
                sys.path.insert(0, script_dir)
            runpy.run_path(value, run_name="__main__")
        else:
            import code as _code
            try:
                __import__("readline")  # line editing in the console
            except ImportError:
                pass
            sys.argv = [""]
            _code.interact(local=_fresh_main().__dict__)
        return 0
    except SystemExit as e:
        return _exit_code(e.code)
    except KeyboardInterrupt:
        traceback.print_exc()
        return 130
    except BaseException:
        traceback.print_exc()
        return 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass


def _cleanup_matlab(verbose):
    # Terminate deployed packages + runtime from the worker thread, as in
    # the C SDK pattern (mclTerminateApplication inside run_main).
    impl = sys.modules.get("matlab_runtime.impl")
    if impl is None or not hasattr(impl, "_cleanup"):
        return
    try:
        _log("Terminating MATLAB runtime (worker thread)", verbose=verbose)
        impl._cleanup()
    except BaseException as e:
        _log(f"WARNING: runtime cleanup failed: {type(e).__name__}: {e}")


class _SigintForwarder:
    """
    Best-effort Ctrl-C forwarding to the worker thread.

    Python's C-level SIGINT handler writes the signal number to the
    wakeup fd (from whatever thread receives the signal). A watcher
    thread reads it and raises KeyboardInterrupt asynchronously in the
    worker thread. A second Ctrl-C hard-exits with code 130.
    """

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.installed = False

    def install(self):
        import signal
        import threading
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._r, self._w = os.pipe()
            os.set_blocking(self._w, False)
            self._old_fd = signal.set_wakeup_fd(self._w)
            self._old_handler = signal.getsignal(signal.SIGINT)
            # A no-op Python handler keeps the C-level handler installed
            # (which trips the wakeup fd) without raising KeyboardInterrupt
            # in the main thread once mclRunMain returns.
            signal.signal(signal.SIGINT, lambda *a: None)
        except Exception as e:
            _log(f"WARNING: Ctrl-C forwarding unavailable: {e}")
            return
        self._stop = threading.Event()
        self._count = 0
        t = threading.Thread(target=self._watch, name="sigint-forwarder",
                             daemon=True)
        t.start()
        self.installed = True

    def _watch(self):
        import select
        import signal
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._r], [], [], 0.25)
                if not ready:
                    continue
                data = os.read(self._r, 64)
            except Exception:
                return
            for b in data:
                if b != signal.SIGINT:
                    continue
                self._count += 1
                if self._count > 1:
                    print(f"\n{_PREFIX} Second interrupt: exiting.",
                          file=sys.stderr, flush=True)
                    os._exit(130)
                tid = STATE.get("worker_ident")
                if tid is None:
                    continue
                print(f"\n{_PREFIX} Interrupt: raising KeyboardInterrupt "
                      "in worker thread (press Ctrl-C again to "
                      "force-quit).", file=sys.stderr, flush=True)
                self._async_raise(tid, KeyboardInterrupt)

    @staticmethod
    def _async_raise(tid, exc):
        import ctypes
        f = ctypes.pythonapi.PyThreadState_SetAsyncExc
        f.argtypes = [ctypes.c_ulong, ctypes.py_object]
        f.restype = ctypes.c_int
        return f(tid, exc)

    def uninstall(self):
        import signal
        if not self.installed:
            return
        self._stop.set()
        try:
            signal.set_wakeup_fd(self._old_fd)
            signal.signal(signal.SIGINT, self._old_handler)
        except Exception:
            pass
        self.installed = False


def run_in_runloop(lib, target, verbose=False):
    """
    Call `mclRunMain` on the main thread; run `target` in its callback.

    Returns the callback's exit code.
    """
    import ctypes
    import threading

    MAINFUNC = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)
    )
    result = {"code": None}

    def _main(argc, argv):
        try:
            STATE["worker_ident"] = threading.get_ident()
            STATE["in_runloop"] = True
            _log(f"Entered mclRunMain callback (thread ident "
                 f"{STATE['worker_ident']}, main thread? "
                 f"{threading.current_thread() is threading.main_thread()})",
                 verbose=verbose)
            code = _run_target(target, verbose)
            _cleanup_matlab(verbose)
            result["code"] = code
            return int(code) & 0xFF if code >= 0 else 1
        except BaseException:  # never let anything escape into C
            import traceback
            traceback.print_exc()
            result["code"] = 1
            return 1
        finally:
            STATE["in_runloop"] = False

    callback = MAINFUNC(_main)
    # Keep a reference so the trampoline is not garbage collected.
    STATE["_callback"] = callback

    run_main = lib.mclRunMain
    run_main.argtypes = [
        MAINFUNC, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)
    ]
    run_main.restype = ctypes.c_int

    argv_list = [os.fsencode(x) for x in (sys.argv[:1] or [b"python"])]
    argv = (ctypes.c_char_p * (len(argv_list) + 1))(*argv_list, None)

    sigint = _SigintForwarder(verbose)
    sigint.install()
    try:
        _log("Calling mclRunMain (main thread will run the CFRunLoop)",
             verbose=verbose)
        ret = run_main(callback, len(argv_list), argv)
        _log(f"mclRunMain returned {ret} (callback code: {result['code']})",
             verbose=verbose)
    finally:
        sigint.uninstall()
    if result["code"] is None:
        # Callback never ran / did not finish
        _log("WARNING: mclRunMain returned without running the callback.")
        return ret if ret else 1
    return result["code"]


# ----------------------------------------------------------------------
#   MWPYTHON GUARD (diagnostic + opt-in in-memory neutralizer)
# ----------------------------------------------------------------------

# Patterns that look like "you must run under mwpython" checks.
_GUARD_LINE = re.compile(
    r"mwpython|darwin|maci64|maca64|\bmac\b|platform\.system|sys\.platform"
    r"|main.?thread|mclRunMain|MWPYTHON",
    re.IGNORECASE,
)
_GUARD_RAISE = re.compile(r"mwpython|main.?thread|mclRunMain", re.IGNORECASE)
_GUARD_TEST = re.compile(
    r"mwpython|darwin|maci64|maca64|\bmac\b|platform\.system|sys\.platform"
    r"|executable",
    re.IGNORECASE,
)
_GUARD_PACKAGES = ("matlab_pysdk",)


def find_guards_in_source(source, filename="<source>", context=3):
    """
    Return guard-looking lines of a python source.

    Returns
    -------
    list of dict(file, lineno, line, context)
    """
    lines = source.splitlines()
    out = []
    for i, line in enumerate(lines):
        if _GUARD_LINE.search(line):
            lo, hi = max(0, i - context), min(len(lines), i + context + 1)
            ctx = "\n".join(
                f"{'>' if j == i else ' '} {j + 1:5d}: {lines[j]}"
                for j in range(lo, hi)
            )
            out.append(dict(file=filename, lineno=i + 1, line=line,
                            context=ctx))
    return out


def _pysdk_dirs(mcrroot=None):
    """Candidate directories of the `matlab_pysdk` package."""
    import importlib.machinery
    dirs = []
    search = list(sys.path)
    if mcrroot:
        search.insert(0, op.join(mcrroot, "toolbox", "compiler_sdk",
                                 "pysdk_py"))
    spec = importlib.machinery.PathFinder.find_spec("matlab_pysdk", search)
    if spec is not None and spec.submodule_search_locations:
        dirs.extend(spec.submodule_search_locations)
    return dirs


def diagnose_guard(mcrroot=None, context=3, file=None):
    """
    Print (and return) guard-looking code in the MathWorks Python SDK.

    Nothing is imported or modified: sources are located through
    `importlib.machinery.PathFinder` and read as text.
    """
    file = file or sys.stderr
    if mcrroot is None:
        mcrroot = STATE.get("mcrroot")
    findings = []
    dirs = _pysdk_dirs(mcrroot)
    if not dirs:
        print(f"{_PREFIX} [guard] matlab_pysdk not found "
              f"(mcrroot={mcrroot})", file=file)
        return findings
    for d in dirs:
        for root, _, files in os.walk(d):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = op.join(root, name)
                try:
                    with open(path, "rb") as f:
                        import importlib.util
                        src = importlib.util.decode_source(f.read())
                except Exception as e:
                    print(f"{_PREFIX} [guard] cannot read {path}: {e}",
                          file=file)
                    continue
                findings.extend(find_guards_in_source(src, path, context))
    print(f"{_PREFIX} [guard] {len(findings)} guard-like line(s) in "
          f"{dirs}", file=file)
    for f_ in findings:
        print(f"--- {f_['file']}:{f_['lineno']}\n{f_['context']}", file=file)
    return findings


def _neutralize_source(source, filename, verbose=True):
    """
    Return (code_object, report) where `raise` / `sys.exit` statements that
    look like an mwpython guard are replaced by `warnings.warn(...)`.
    """
    import ast

    tree = ast.parse(source, filename)
    report = []

    def seg(node):
        try:
            s = ast.get_source_segment(source, node)
        except Exception:
            s = None
        if s is None:
            lines = source.splitlines()
            s = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) \
                else ""
        return s

    def is_exit_call(node):
        if not (isinstance(node, ast.Expr) and isinstance(node.value,
                                                          ast.Call)):
            return False
        return re.fullmatch(r"(sys\.|os\.)?_?exit|quit",
                            seg(node.value.func) or "") is not None

    def is_guard_stmt(node, guarded_test):
        if not (isinstance(node, ast.Raise) or is_exit_call(node)):
            return False
        return bool(_GUARD_RAISE.search(seg(node))) or guarded_test

    def make_warning(node):
        msg = (f"matlab_runtime bypassed a suspected mwpython guard at "
               f"{filename}:{node.lineno}: {seg(node).strip()[:200]}")
        new = ast.parse(f"__import__('warnings').warn({msg!r})").body[0]
        return ast.copy_location(new, node)

    class T(ast.NodeTransformer):
        def __init__(self):
            self.guarded = [False]

        def _visit_body(self, body, guarded):
            self.guarded.append(guarded)
            out = []
            for stmt in body:
                if is_guard_stmt(stmt, guarded):
                    report.append((stmt.lineno, seg(stmt)))
                    out.append(make_warning(stmt))
                else:
                    r = self.visit(stmt)
                    if r is None:
                        continue
                    out.extend(r if isinstance(r, list) else [r])
            self.guarded.pop()
            return out or [ast.Pass()]

        def generic_visit(self, node):
            for field in ("body", "orelse", "finalbody"):
                val = getattr(node, field, None)
                if isinstance(val, list) and val and isinstance(val[0],
                                                                ast.stmt):
                    guarded = (
                        isinstance(node, ast.If) and field == "body"
                        and bool(_GUARD_TEST.search(seg(node.test)))
                    )
                    setattr(node, field, self._visit_body(val, guarded))
            for field, val in ast.iter_fields(node):
                if field in ("body", "orelse", "finalbody"):
                    if isinstance(val, list) and val and \
                            isinstance(val[0], ast.stmt):
                        continue
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, ast.AST):
                            self.visit(item)
                elif isinstance(val, ast.AST):
                    self.visit(val)
            return node

    tree = T().visit(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, filename, "exec", dont_inherit=True)
    return code, report


_BYPASS = {"finder": None}


def install_guard_bypass(verbose=True, packages=_GUARD_PACKAGES):
    """
    OPT-IN: neutralize suspected "must run under mwpython" guards in the
    MathWorks Python SDK, *in memory only*.

    A meta-path finder intercepts imports of `matlab_pysdk[.*]`, reads the
    original source, replaces `raise` / `sys.exit(...)` statements that
    either mention mwpython / the main thread, or sit directly in the body
    of an `if` whose test mentions darwin / mac / mwpython / platform /
    executable, by a `warnings.warn(...)`, and executes the result. Files
    on disk are never modified. Every replacement is logged.

    Must be called before `matlab_pysdk` is imported (i.e. before
    `matlab_runtime.init()`).
    """
    import importlib.abc
    import importlib.machinery
    import importlib.util

    if _BYPASS["finder"] is not None:
        return _BYPASS["finder"]
    already = [m for m in sys.modules
               if any(m == p or m.startswith(p + ".") for p in packages)]
    if already:
        _log(f"WARNING: guard bypass installed too late, already imported: "
             f"{already}")

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = importlib.util.decode_source(self.get_data(self.path))
            code, report = _neutralize_source(source, self.path, verbose)
            if report:
                for lineno, text in report:
                    _log(f"[guard-bypass] {self.path}:{lineno}: "
                         f"{text.strip()[:300]!r} -> warnings.warn")
            else:
                _log(f"[guard-bypass] {fullname}: nothing to neutralize",
                     verbose=verbose)
            return code

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if not any(fullname == p or fullname.startswith(p + ".")
                       for p in packages):
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    break
            else:
                return None
            origin = spec.origin or ""
            if not origin.endswith(".py") or not isinstance(
                spec.loader, importlib.machinery.SourceFileLoader
            ):
                _log(f"[guard-bypass] {fullname}: not a source module "
                     f"({origin}); left untouched")
                return spec
            kw = {}
            if spec.submodule_search_locations is not None:
                kw["submodule_search_locations"] = list(
                    spec.submodule_search_locations
                )
            return importlib.util.spec_from_file_location(
                fullname, origin, loader=_Loader(fullname, origin), **kw
            )

    finder = _Finder()
    sys.meta_path.insert(0, finder)
    _BYPASS["finder"] = finder
    _log(f"[guard-bypass] installed for {packages}", verbose=verbose)
    return finder


def uninstall_guard_bypass():
    finder = _BYPASS["finder"]
    if finder is not None and finder in sys.meta_path:
        sys.meta_path.remove(finder)
    _BYPASS["finder"] = None


# ----------------------------------------------------------------------
#   CLI
# ----------------------------------------------------------------------

_help = """
usage: mpython-native [-verbose] [-variant vrt] [-mcrroot dir]
                      [-mlstartup opt[,opt]] [-nojvm] [-nodisplay]
                      [-bypass-guard] [-diagnose-guard] [-no-runloop]
                      [-no-reexec] [-hard-exit] [py_args]
                      [-c cmd | -m mod | scr.py] [arg ...]

EXPERIMENTAL (macOS): run Python code that uses MATLAB-compiled packages
with the *calling* Python interpreter, without MathWorks' mwpython.app.
The main thread runs the MATLAB runtime's CFRunLoop (via mclRunMain) and
the target runs on a secondary thread, like mwpython does.

Arguments:
    -verbose             : verbose mode (diagnostics on stderr)
    -variant vrt         : MATLAB runtime version to use (latest installed)
    -mcrroot dir         : explicit runtime root (e.g. .../R2024b)
    -mlstartup opt[,opt] : runtime startup options, forwarded to
                           matlab_runtime.init_runtime via
                           $MATLAB_RUNTIME_STARTUP_OPTIONS
    -nojvm, -nodisplay   : shortcuts for -mlstartup -nojvm / -nodisplay
    -bypass-guard        : opt-in in-memory neutralizer of the SDK's
                           "must use mwpython" guard (logged)
    -diagnose-guard      : print guard-like code found in the SDK sources
    -no-runloop          : do not call mclRunMain; run the target on the
                           main thread (environment setup only)
    -no-reexec           : do not re-exec the interpreter to apply
                           DYLD_FALLBACK_LIBRARY_PATH
    -hard-exit           : os._exit() with the target's code (skips
                           interpreter finalization; for hangs at exit)
    py_args              : other options before the target are passed to
                           the re-executed python interpreter
    -c cmd               : execute Python command cmd
    -m mod [arg ...]     : execute Python module mod
    scr.py [arg ...]     : execute Python script scr.py
    (no target)          : interactive console

Examples:
    mpython-native -verbose myscript.py arg1 arg2
    mpython-native -nojvm -m mymod arg1
    mpython-native -c "import matlab_runtime; matlab_runtime.init()"
"""


def _parse(args):
    o = dict(
        verbose=False, variant="latest_installed", mcrroot=None, options=[],
        bypass_guard=False, diagnose_guard=False, no_runloop=False,
        no_reexec=False, hard_exit=False, allow_non_mac=False,
        py_args=[], target=("repl", None, []), help=False,
    )
    args = list(args)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "-?", "-help", "--help"):
            o["help"] = True
            return o
        elif arg == "-verbose":
            o["verbose"] = True
        elif arg in ("-variant", "-mcrroot", "-mlstartup"):
            if not args:
                raise ValueError(f"Argument following {arg} must not be "
                                 f"empty")
            val = args.pop(0)
            if arg == "-variant":
                o["variant"] = val
            elif arg == "-mcrroot":
                o["mcrroot"] = val
            else:
                o["options"] += _split_options(val)
        elif arg in ("-nojvm", "-nodisplay"):
            o["options"].append(arg)
        elif arg == "-bypass-guard":
            o["bypass_guard"] = True
        elif arg == "-diagnose-guard":
            o["diagnose_guard"] = True
        elif arg == "-no-runloop":
            o["no_runloop"] = True
        elif arg == "-no-reexec":
            o["no_reexec"] = True
        elif arg == "-hard-exit":
            o["hard_exit"] = True
        elif arg == "-allow-non-mac":   # developer testing only
            o["allow_non_mac"] = True
        elif arg in ("-c", "-m"):
            if not args:
                raise ValueError(f"Argument following {arg} must not be "
                                 f"empty")
            o["target"] = (arg[1], args[0], args[1:])
            break
        elif arg == "-":
            o["target"] = ("repl", None, args)
            break
        elif arg.startswith("-"):
            o["py_args"].append(arg)
        else:
            o["target"] = ("script", arg, args)
            break
    o["options"] = _merge_options(o["options"])
    return o


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    orig_args = list(args)
    try:
        o = _parse(orig_args)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if o["help"]:
        print(_help)
        return 0
    verbose = o["verbose"]

    from .utils import guess_arch, SUPPORTED_PYTHON_VERSIONS
    arch = guess_arch()
    if arch[:3] != "mac" and not o["allow_non_mac"]:
        print("mpython-native is only meant to be used on macOS.",
              file=sys.stderr)
        return 10
    reexeced = _truthy(os.environ.get(ENV_REEXEC))

    try:
        mcrroot, release = find_mcrroot(o["variant"], o["mcrroot"], arch)
    except Exception as e:
        print(f"{_PREFIX} {e}", file=sys.stderr)
        return 1
    STATE["mcrroot"], STATE["release"] = mcrroot, release
    if not reexeced:
        _log(f"arch: {arch}; runtime: {mcrroot} ({release})",
             verbose=verbose)

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    supported = SUPPORTED_PYTHON_VERSIONS.get(release or "", None)
    if supported is not None and pyver not in supported and not reexeced:
        _log(f"WARNING: Python {pyver} is not officially supported with "
             f"MATLAB {release} (supported: {', '.join(supported)}). "
             f"Continuing anyway.")

    # --- environment (also applied in-process, for child processes) ---
    env = dict(os.environ)
    env["DYLD_FALLBACK_LIBRARY_PATH"] = dyld_fallback_library_path(
        mcrroot, arch, os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    ) if not reexeced else os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    options = _merge_options(startup_options_from_env(), o["options"])
    if options:
        env[ENV_OPTIONS] = ",".join(options)
    if o["bypass_guard"]:
        env[ENV_BYPASS] = "1"
    if o["diagnose_guard"]:
        env[ENV_DIAGNOSE] = "1"

    # --- re-exec so that dyld sees DYLD_FALLBACK_LIBRARY_PATH ---------
    if not reexeced and not o["no_reexec"]:
        if sys.platform == "darwin" and _python_on_sip_path(sys.executable):
            _log(f"WARNING: {sys.executable} is under a SIP-protected path; "
                 f"DYLD_* variables will be stripped by macOS.")
        env[ENV_REEXEC] = "1"
        parent = _package_parent()
        if _needs_pythonpath(parent):
            pp = [x for x in env.get("PYTHONPATH", "").split(os.pathsep)
                  if x]
            env["PYTHONPATH"] = os.pathsep.join([parent, *pp])
        argv = [sys.executable, *o["py_args"], "-m", "matlab_runtime.macos",
                *orig_args]
        _log(f"DYLD_FALLBACK_LIBRARY_PATH="
             f"{env['DYLD_FALLBACK_LIBRARY_PATH']}", verbose=verbose)
        _log(f"Re-executing: {argv}", verbose=verbose)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, argv, env)
        return 1  # unreachable

    if o["py_args"] and not reexeced:
        _log(f"WARNING: python options {o['py_args']} ignored with "
             f"-no-reexec")
    if not reexeced:
        _log("NOTE: -no-reexec: DYLD_FALLBACK_LIBRARY_PATH only affects "
             "child processes, not this one.", verbose=verbose)
    os.environ.update(env)
    os.environ.pop(ENV_REEXEC, None)  # do not leak into user subprocesses
    if options:
        _log(f"Runtime startup options: {options}", verbose=verbose)

    if o["diagnose_guard"]:
        diagnose_guard(mcrroot)
    if o["bypass_guard"]:
        install_guard_bypass(verbose=verbose)

    # --- run ----------------------------------------------------------
    if o["no_runloop"]:
        _log("-no-runloop: running target on the main thread",
             verbose=verbose)
        code = _run_target(o["target"], verbose)
        _cleanup_matlab(verbose)
    else:
        try:
            lib = _load_mclmcrrt(find_mclmcrrt(mcrroot, arch), verbose)
            _mclmcr_initialize(lib, verbose)
        except Exception as e:
            print(f"{_PREFIX} Failed to load the MATLAB runtime library: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        code = run_in_runloop(lib, o["target"], verbose)

    if o["hard_exit"]:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    # Delegate to the importable module so that `STATE` and the guard
    # bypass are shared with `matlab_runtime.impl` (which imports
    # `matlab_runtime.macos`, a different module object than __main__).
    from matlab_runtime.macos import main as _main
    sys.exit(_main())
