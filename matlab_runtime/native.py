"""
EXPERIMENTAL -- pure-Python replacement for MathWorks' `mwpython` (macOS).

This module is meant for diagnostics. It does *not* make GUI toolkits
(matplotlib, Jupyter kernels, ...) work: user code runs off the main
thread, exactly as under `mwpython`. See `matlab_runtime.remote` for the
recommended way of using MATLAB packages from a regular interpreter.

Background
----------
On macOS, the MATLAB Runtime (its JVM / AppKit integration) requires the
process *main thread* to run a CoreFoundation run loop. MathWorks'
`mwpython` launcher therefore runs the CFRunLoop on the main thread and
the Python interpreter on a secondary thread. The C/C++ SDK exposes the
same mechanism through

    int mclRunMain(mclMainFcnType run_main, int argc, const char **argv);

This module does the same thing from a regular Python interpreter:

1. locate the runtime and set `DYLD_FALLBACK_LIBRARY_PATH` (dyld only
   reads `DYLD_*` variables at process start, so the current interpreter
   is re-executed once with the right environment);
2. `ctypes`-load `libmwmclmcrrt.dylib` by absolute path and call
   `mclmcrInitialize()` (proxy layer initialisation);
3. call `mclRunMain(callback, argc, argv)` from the main thread. The
   callback runs the user target (script / `-m module` / `-c cmd` / REPL)
   with `runpy` on the secondary thread created by the runtime, and
   returns an exit code.

Known limitations
-----------------
* Python signal handlers only run on the main thread, which is blocked
  inside `mclRunMain`. Ctrl-C is forwarded on a best-effort basis
  (`KeyboardInterrupt` is raised asynchronously in the worker thread, and
  only delivered while it executes Python bytecode). A second Ctrl-C
  hard-exits the process (exit code 130).
* User code runs on a non-main thread: `signal.signal(...)` raises
  `ValueError`, and libraries that require the main thread (e.g. GUI
  toolkits such as matplotlib's macosx backend) will not work.
* Interpreters under SIP-protected paths (e.g. `/usr/bin/python3`) have
  `DYLD_*` variables stripped; use a conda/homebrew/python.org Python.

Importing this module has no side effects (no library is loaded).

Environment variables (set by the launcher's command-line options)
-----------------------------------------------------------------------
They are only honoured by `matlab_runtime.impl` while `mpython-native` is
actually running in the current process (`STATE["active"]`); outside the
launcher (including in child processes that inherit them) they are
ignored and `matlab_runtime` behaves exactly as without this module.

MATLAB_RUNTIME_NATIVE_STARTUP_OPTIONS
    Comma-separated runtime startup options (e.g. "-nojvm") merged into the
    `option_list` of `matlab_runtime.init_runtime`.
MATLAB_RUNTIME_NATIVE_BYPASS_GUARD
    If truthy, `matlab_runtime.init` installs the (opt-in) in-memory
    neutralizer of the "must run under mwpython" guard of the MathWorks
    Python SDK (see `install_guard_bypass`).
MATLAB_RUNTIME_NATIVE_DIAGNOSE_GUARD
    If truthy, `matlab_runtime.init` prints the guard-like code it finds in
    the MathWorks Python SDK sources (see `diagnose_guard`).
MATLAB_RUNTIME_NATIVE_MCLMCR_MODE
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
]

ENV_REEXEC = "_MATLAB_RUNTIME_NATIVE_REEXEC"
ENV_OPTIONS = "MATLAB_RUNTIME_NATIVE_STARTUP_OPTIONS"
ENV_BYPASS = "MATLAB_RUNTIME_NATIVE_BYPASS_GUARD"
ENV_DIAGNOSE = "MATLAB_RUNTIME_NATIVE_DIAGNOSE_GUARD"
ENV_MCLMCR_MODE = "MATLAB_RUNTIME_NATIVE_MCLMCR_MODE"

# Process-wide state (read by `matlab_runtime.impl`).
STATE = {
    "active": False,        # True once `main()` runs (launcher process)
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


def _split_options(value):
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def _unique(items):
    return list(dict.fromkeys(items))


# ----------------------------------------------------------------------
#   RUNTIME DISCOVERY
# ----------------------------------------------------------------------


def find_mcrroot(variant="latest_installed", arch=None):
    """
    Locate a runtime installation that contains `runtime/<arch>`.

    Use `$MATLAB_RUNTIME_PATH` to point to an unusual location.

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

    for path, ver in iter_existing_installations(variant):
        if op.isdir(op.join(path, "runtime", arch)):
            return path, _release(path, ver)

    if variant and variant != "latest_installed":
        path = find_runtime(variant)
        if path and op.isdir(op.join(path, "runtime", arch)):
            return path, _release(path)

    raise RuntimeError(
        "No MATLAB Runtime found. If you have installed Matlab Runtime "
        "in an unusual location, set the MATLAB_RUNTIME_PATH environment "
        "variable to the path of the installation "
        "(e.g. /Applications/MATLAB/MATLAB_Runtime/R2024b)."
    )


def dyld_fallback_library_path(mcrroot, arch, existing=None):
    """Search path similar to `mpython` (matlab_runtime.cli).

    The current directory (".") is deliberately not included, so that a
    dylib in the working directory cannot shadow the runtime's libraries.
    DYLD paths are macOS-only, so entries are always joined with ":".
    """
    if existing is None:
        existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    return ":".join(_unique([
        f"{mcrroot}/runtime/{arch}",
        f"{mcrroot}/bin/{arch}",
        f"{mcrroot}/sys/os/{arch}",
        op.join(sys.prefix, "lib"),
        *[x for x in existing.split(":") if x and x != "."],
        "/usr/local/lib",
        "/usr/lib",
    ]))


def find_mclmcrrt(mcrroot, arch):
    """Absolute path to libmwmclmcrrt (unversioned name preferred)."""
    import glob
    ext = ".dylib" if sys.platform == "darwin" else ".so"
    dirs = [op.join(mcrroot, "runtime", arch), op.join(mcrroot, "bin", arch)]
    for d in dirs:
        p = op.join(d, "libmwmclmcrrt" + ext)
        if op.exists(p):
            return p
    for d in dirs:
        # e.g. libmwmclmcrrt.24.2.dylib / libmwmclmcrrt.so.24.2
        hits = sorted(glob.glob(op.join(d, "libmwmclmcrrt*" + ext + "*")))
        if hits:
            return hits[-1]
    raise FileNotFoundError(f"libmwmclmcrrt{ext} not found under {dirs}")


# ----------------------------------------------------------------------
#   MCLMCR (ctypes)
# ----------------------------------------------------------------------


def _load_mclmcrrt(path, verbose):
    import ctypes
    _log(f"Loading {path} (RTLD_GLOBAL)", verbose=verbose)
    return ctypes.CDLL(path, mode=getattr(ctypes, "RTLD_GLOBAL", 0))


def _mclmcr_initialize(lib, verbose):
    import ctypes
    if hasattr(lib, "mclmcrInitialize"):
        f, args, name = lib.mclmcrInitialize, (), "mclmcrInitialize"
    elif hasattr(lib, "mclmcrInitialize2"):
        mode = int(os.environ.get(ENV_MCLMCR_MODE, "0"))
        f, args, name = lib.mclmcrInitialize2, (mode,), "mclmcrInitialize2"
    else:
        _log("WARNING: neither mclmcrInitialize nor mclmcrInitialize2 is "
             "exported; continuing without proxy-layer initialization.")
        return None
    f.argtypes = [ctypes.c_int] * len(args)
    f.restype = ctypes.c_bool
    ok = f(*args)
    _log(f"{name}{args if args else '()'} -> {ok}", verbose=verbose)
    return ok


def _clamp_exit_code(code):
    """Map an int to a process exit status without wrapping to 0."""
    code = int(code)
    if code == 0:
        return 0
    return min(code, 255) if code > 0 else 1


def _exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return _clamp_exit_code(code)
    print(code, file=sys.stderr)
    return 1


def _join_user_threads(verbose=False):
    """
    Wait for non-daemon threads started by the user target (as the
    interpreter would at shutdown), so that the runtime is not terminated
    under their feet. Interruptible by (forwarded) Ctrl-C.
    """
    import threading
    current, main_thread = threading.current_thread(), threading.main_thread()
    try:
        while True:
            pending = [t for t in threading.enumerate()
                       if t is not current and t is not main_thread
                       and not t.daemon and t.is_alive()]
            if not pending:
                return
            _log(f"Waiting for non-daemon threads: "
                 f"{[t.name for t in pending]}", verbose=verbose)
            for t in pending:
                while t.is_alive():
                    t.join(0.25)
    except KeyboardInterrupt:
        _log("Interrupted while waiting for non-daemon threads.")


def _fresh_main():
    import types
    mod = types.ModuleType("__main__")
    mod.__dict__["__builtins__"] = __builtins__
    sys.modules["__main__"] = mod
    return mod


def _run_target(target):
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
            exec(compile(value, "<string>", "exec"), _fresh_main().__dict__)
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
            import code
            try:
                __import__("readline")  # line editing in the console
            except ImportError:
                pass
            sys.argv = [""]
            code.interact(local=_fresh_main().__dict__)
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

    installed = False

    def install(self):
        import signal
        import threading
        if threading.current_thread() is not threading.main_thread():
            return
        self._fds = ()
        self._thread = None
        old_fd = old_handler = None
        ok = False
        try:
            self._r, self._w = self._fds = os.pipe()
            os.set_blocking(self._w, False)
            old_fd = signal.set_wakeup_fd(self._w)
            old_handler = signal.getsignal(signal.SIGINT)
            # A no-op Python handler keeps the C-level handler installed
            # (which trips the wakeup fd) without raising KeyboardInterrupt
            # in the main thread once mclRunMain returns.
            signal.signal(signal.SIGINT, lambda *a: None)
            self._old_fd, self._old_handler = old_fd, old_handler
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._watch, name="sigint-forwarder", daemon=True
            )
            self._thread.start()
            ok = self.installed = True
        except Exception as e:
            _log(f"WARNING: Ctrl-C forwarding unavailable: {e}")
        finally:
            if not ok:
                self._restore(old_fd, old_handler)
                self._close()

    @staticmethod
    def _restore(old_fd, old_handler):
        import signal
        try:
            if old_fd is not None:
                signal.set_wakeup_fd(old_fd)
            if old_handler is not None:
                signal.signal(signal.SIGINT, old_handler)
        except Exception:
            pass

    def _close(self):
        for fd in getattr(self, "_fds", ()):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = ()

    def _watch(self):
        import ctypes
        import select
        import signal
        count = 0
        while not self._stop.is_set():
            try:
                if not select.select([self._r], [], [], 0.25)[0]:
                    continue
                data = os.read(self._r, 64)
            except Exception:
                return
            for _ in range(data.count(bytes([signal.SIGINT]))):
                count += 1
                if count > 1:
                    print(f"\n{_PREFIX} Second interrupt: exiting.",
                          file=sys.stderr, flush=True)
                    os._exit(130)
                tid = STATE.get("worker_ident")
                if tid is None:
                    continue
                print(f"\n{_PREFIX} Interrupt: raising KeyboardInterrupt "
                      "in worker thread (press Ctrl-C again to "
                      "force-quit).", file=sys.stderr, flush=True)
                f = ctypes.pythonapi.PyThreadState_SetAsyncExc
                f.argtypes = [ctypes.c_ulong, ctypes.py_object]
                f.restype = ctypes.c_int
                f(tid, KeyboardInterrupt)

    def uninstall(self):
        if not self.installed:
            return
        self.installed = False
        try:
            self._stop.set()
            self._restore(self._old_fd, self._old_handler)
            if self._thread is not None:
                self._thread.join(1.0)  # select() times out every 0.25s
        finally:
            self._close()


def run_in_runloop(lib, target, verbose=False):
    """
    Call `mclRunMain` on the main thread; run `target` in its callback.

    Returns the callback's exit code.
    """
    import ctypes
    import threading
    import traceback

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
            code = _clamp_exit_code(_run_target(target))
            _join_user_threads(verbose)
            _cleanup_matlab(verbose)
            result["code"] = code
            return code
        except BaseException:  # never let anything escape into C
            traceback.print_exc()
            result["code"] = 1
            return 1
        finally:
            STATE["in_runloop"] = False

    # Keep a reference so the trampoline is not garbage collected.
    callback = STATE["_callback"] = MAINFUNC(_main)

    run_main = lib.mclRunMain
    run_main.argtypes = [
        MAINFUNC, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)
    ]
    run_main.restype = ctypes.c_int

    argv_list = [os.fsencode(x) for x in (sys.argv[:1] or ["python"])]
    argv = (ctypes.c_char_p * (len(argv_list) + 1))(*argv_list, None)

    sigint = _SigintForwarder()
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
        _log("WARNING: mclRunMain returned without running the callback.")
        return _clamp_exit_code(ret) if ret else 1
    return result["code"]


# ----------------------------------------------------------------------
#   MWPYTHON GUARD (diagnostic + opt-in in-memory neutralizer)
# ----------------------------------------------------------------------

# Patterns that look like "you must run under mwpython" checks.
_GUARD_LINE = re.compile(
    r"mwpython|darwin|maci64|maca64|\bmac\b|platform\.system|sys\.platform"
    r"|main.?thread|mclRunMain",
    re.IGNORECASE,
)
# Only statements whose message mentions mwpython are neutralized.
_GUARD_MESSAGE = re.compile(r"mwpython", re.IGNORECASE)
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
    search = list(sys.path)
    if mcrroot:
        search.insert(0, op.join(mcrroot, "toolbox", "compiler_sdk",
                                 "pysdk_py"))
    spec = importlib.machinery.PathFinder.find_spec("matlab_pysdk", search)
    return list(getattr(spec, "submodule_search_locations", None) or [])


def diagnose_guard(mcrroot=None, context=3, file=None):
    """
    Print (and return) guard-looking code in the MathWorks Python SDK.

    Nothing is imported or modified: sources are located through
    `importlib.machinery.PathFinder` and read as text.
    """
    import importlib.util
    file = file or sys.stderr
    mcrroot = mcrroot or STATE.get("mcrroot")
    dirs = _pysdk_dirs(mcrroot)
    findings = []
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
                        src = importlib.util.decode_source(f.read())
                except Exception as e:
                    print(f"{_PREFIX} [guard] cannot read {path}: {e}",
                          file=file)
                    continue
                findings.extend(find_guards_in_source(src, path, context))
    print(f"{_PREFIX} [guard] {len(findings)} guard-like line(s) in "
          f"{dirs}", file=file)
    for item in findings:
        print(f"--- {item['file']}:{item['lineno']}\n{item['context']}",
              file=file)
    return findings


def _neutralize_source(source, filename):
    """
    Return (code_object, report) where `raise X` / `sys.exit(...)`
    statements that look like an mwpython guard are replaced by
    `warnings.warn(...)`.

    A statement is replaced only if a string constant in the raised /
    exit expression, or in the test of the `if` whose body directly
    contains it, mentions "mwpython" (case-insensitive). Bare `raise`
    (re-raise) statements are never touched.
    """
    import ast

    tree = ast.parse(source, filename)
    lines = source.splitlines()
    report = []

    def seg(node):
        s = ast.get_source_segment(source, node)
        if s is None and 0 < node.lineno <= len(lines):
            s = lines[node.lineno - 1]
        return s or ""

    def mentions_mwpython(node):
        return node is not None and any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and _GUARD_MESSAGE.search(n.value)
            for n in ast.walk(node)
        )

    def is_guard(stmt, test):
        if isinstance(stmt, ast.Raise):
            if stmt.exc is None:        # bare re-raise: never touched
                return False
            expr = stmt.exc
        elif (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and re.fullmatch(r"(sys\.|os\.)?_?exit|quit",
                             seg(stmt.value.func)) is not None
        ):
            expr = stmt.value
        else:
            return False
        return mentions_mwpython(expr) or mentions_mwpython(test)

    def warning(stmt):
        msg = (f"matlab_runtime bypassed a suspected mwpython guard at "
               f"{filename}:{stmt.lineno}: {seg(stmt).strip()[:200]}")
        new = ast.parse(f"__import__('warnings').warn({msg!r})").body[0]
        return ast.copy_location(new, stmt)

    def visit(node):
        for field, val in ast.iter_fields(node):
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                test = (node.test if isinstance(node, ast.If)
                        and field == "body" else None)
                new = []
                for stmt in val:
                    if is_guard(stmt, test):
                        report.append((stmt.lineno, seg(stmt)))
                        new.append(warning(stmt))
                    else:
                        visit(stmt)
                        new.append(stmt)
                setattr(node, field, new)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, ast.AST):
                        visit(item)
            elif isinstance(val, ast.AST):
                visit(val)

    visit(tree)
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec", dont_inherit=True), report


_BYPASS = {"finder": None}


def install_guard_bypass(verbose=True, packages=_GUARD_PACKAGES):
    """
    OPT-IN: neutralize suspected "must run under mwpython" guards in the
    MathWorks Python SDK, *in memory only*.

    A meta-path finder intercepts imports of `matlab_pysdk[.*]` and
    compiles a rewritten version of the original source (see
    `_neutralize_source`). Files on disk are never modified. Every
    replacement is logged.

    Must be called before `matlab_pysdk` is imported (i.e. before
    `matlab_runtime.init()`).
    """
    import importlib.abc
    import importlib.machinery
    import importlib.util

    if _BYPASS["finder"] is not None:
        return _BYPASS["finder"]

    def _match(name):
        return any(name == p or name.startswith(p + ".") for p in packages)

    already = [m for m in sys.modules if _match(m)]
    if already:
        _log(f"WARNING: guard bypass installed too late, already imported: "
             f"{already}")

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = importlib.util.decode_source(self.get_data(self.path))
            code, report = _neutralize_source(source, self.path)
            for lineno, text in report:
                _log(f"[guard-bypass] {self.path}:{lineno}: "
                     f"{text.strip()[:300]!r} -> warnings.warn")
            if not report:
                _log(f"[guard-bypass] {fullname}: nothing to neutralize",
                     verbose=verbose)
            return code

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if not _match(fullname):
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

    finder = _BYPASS["finder"] = _Finder()
    sys.meta_path.insert(0, finder)
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
usage: mpython-native [-verbose] [-variant vrt] [-mlstartup opt[,opt]]
                      [-nojvm] [-bypass-guard] [-diagnose-guard]
                      [-no-runloop] [-no-reexec] [-hard-exit]
                      [-c cmd | -m mod | scr.py] [arg ...]

EXPERIMENTAL (macOS, for diagnostics): run Python code that uses
MATLAB-compiled packages with the *calling* Python interpreter, without
MathWorks' mwpython. The main thread runs the MATLAB runtime's CFRunLoop
(via mclRunMain) and the target runs on a secondary thread, like mwpython
does. GUI toolkits (matplotlib, Jupyter) still do not work; see
matlab_runtime.remote instead.

Arguments:
    -verbose             : verbose mode (diagnostics on stderr)
    -variant vrt         : MATLAB runtime version to use (latest installed)
    -mlstartup opt[,opt] : runtime startup options, forwarded to
                           matlab_runtime.init_runtime via
                           $MATLAB_RUNTIME_NATIVE_STARTUP_OPTIONS
    -nojvm               : shortcut for -mlstartup -nojvm
    -bypass-guard        : opt-in in-memory neutralizer of the SDK's
                           "must use mwpython" guard (logged)
    -diagnose-guard      : print guard-like code found in the SDK sources
    -no-runloop          : do not call mclRunMain; run the target on the
                           main thread (environment setup only)
    -no-reexec           : do not re-exec the interpreter to apply
                           DYLD_FALLBACK_LIBRARY_PATH
    -hard-exit           : os._exit() with the target's code (skips
                           interpreter finalization; for hangs at exit)
    -c cmd               : execute Python command cmd
    -m mod [arg ...]     : execute Python module mod
    scr.py [arg ...]     : execute Python script scr.py
    (no target)          : interactive console

Set MATLAB_RUNTIME_PATH to use a runtime installed in an unusual location.
"""


def _parse(args):
    o = dict(
        verbose=False, variant="latest_installed", options=[],
        bypass_guard=False, diagnose_guard=False, no_runloop=False,
        no_reexec=False,
        hard_exit=False, target=("repl", None, []), help=False,
    )
    flags = {
        "-verbose": "verbose",
        "-bypass-guard": "bypass_guard",
        "-diagnose-guard": "diagnose_guard",
        "-no-runloop": "no_runloop",
        "-no-reexec": "no_reexec",
        "-hard-exit": "hard_exit",
    }
    args = list(args)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "-?", "-help", "--help"):
            o["help"] = True
            return o
        elif arg in flags:
            o[flags[arg]] = True
        elif arg in ("-variant", "-mlstartup", "-c", "-m"):
            if not args:
                raise ValueError(f"Argument following {arg} must not be "
                                 f"empty")
            if arg == "-variant":
                o["variant"] = args.pop(0)
            elif arg == "-mlstartup":
                o["options"] += _split_options(args.pop(0))
            else:
                o["target"] = (arg[1], args[0], args[1:])
                break
        elif arg == "-nojvm":
            o["options"].append(arg)
        elif arg == "-":
            o["target"] = ("repl", None, args)
            break
        elif arg.startswith("-"):
            raise ValueError(f"Unknown option: {arg}")
        else:
            o["target"] = ("script", arg, args)
            break
    o["options"] = _unique(o["options"])
    return o


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    args = list(args)
    try:
        o = _parse(args)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if o["help"]:
        print(_help)
        return 0
    verbose = o["verbose"]

    from .utils import guess_arch, SUPPORTED_PYTHON_VERSIONS
    arch = guess_arch()
    if arch[:3] != "mac":
        print("mpython-native is only meant to be used on macOS.",
              file=sys.stderr)
        return 10
    # From here on, this process *is* the launcher: enables the
    # launcher-only hooks of matlab_runtime.impl.
    STATE["active"] = True
    reexeced = _truthy(os.environ.get(ENV_REEXEC))

    try:
        mcrroot, release = find_mcrroot(o["variant"], arch)
    except Exception as e:
        print(f"{_PREFIX} {e}", file=sys.stderr)
        return 1
    STATE["mcrroot"], STATE["release"] = mcrroot, release

    if not reexeced:
        _log(f"arch: {arch}; runtime: {mcrroot} ({release})",
             verbose=verbose)
        pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
        supported = SUPPORTED_PYTHON_VERSIONS.get(release or "", None)
        if supported is not None and pyver not in supported:
            _log(f"WARNING: Python {pyver} is not officially supported "
                 f"with MATLAB {release} (supported: "
                 f"{', '.join(supported)}). Continuing anyway.")

    # --- environment (also applied in-process, for child processes) ---
    env = dict(os.environ)
    if not reexeced:
        env["DYLD_FALLBACK_LIBRARY_PATH"] = \
            dyld_fallback_library_path(mcrroot, arch)
    options = _unique(_split_options(env.get(ENV_OPTIONS)) + o["options"])
    if options:
        env[ENV_OPTIONS] = ",".join(options)
    if o["bypass_guard"]:
        env[ENV_BYPASS] = "1"
    if o["diagnose_guard"]:
        env[ENV_DIAGNOSE] = "1"

    # --- re-exec so that dyld sees DYLD_FALLBACK_LIBRARY_PATH ---------
    if not reexeced and not o["no_reexec"]:
        exe = op.realpath(sys.executable)
        if exe.startswith(("/usr/bin/", "/bin/", "/sbin/", "/usr/sbin/",
                           "/System/")):
            _log(f"WARNING: {sys.executable} is under a SIP-protected path; "
                 f"DYLD_* variables will be stripped by macOS.")
        env[ENV_REEXEC] = "1"
        # Import this package from where it currently lives, without
        # leaking that location into the user's sys.path / PYTHONPATH.
        parent = op.dirname(op.dirname(op.abspath(__file__)))
        bootstrap = (
            f"import sys; sys.path.insert(0, {parent!r}); "
            f"import matlab_runtime.native as n; sys.path.remove({parent!r}); "
            f"sys.exit(n.main())"
        )
        argv = [sys.executable, "-c", bootstrap, *args]
        _log(f"DYLD_FALLBACK_LIBRARY_PATH="
             f"{env['DYLD_FALLBACK_LIBRARY_PATH']}", verbose=verbose)
        _log(f"Re-executing: {argv}", verbose=verbose)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, argv, env)

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
        code = _run_target(o["target"])
        _join_user_threads(verbose)
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
        os._exit(_clamp_exit_code(code))
    return code


if __name__ == "__main__":
    # Delegate to the importable module so that `STATE` and the guard
    # bypass are shared with `matlab_runtime.impl` (which imports
    # `matlab_runtime.native`, a different module object than __main__).
    from matlab_runtime.native import main as _main
    sys.exit(_main())
