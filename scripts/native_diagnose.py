#!/usr/bin/env python
"""
EXPERIMENTAL -- diagnose ways of running MATLAB-compiled Python packages on
macOS *without* MathWorks' `mwpython`.

Stdlib only (plus the `matlab_runtime` package of this repository, which is
picked from the repository checkout if present, else from site-packages).

Run on a Mac that has a MATLAB Runtime installed, from the repository root:

    python scripts/native_diagnose.py

and paste back the report file it writes (default:
./native_diagnose_report.txt).

Steps (each guarded; reported as PASS / FAIL / SKIP / INFO):

  a. environment: python, arch, runtime location, python version support,
     key runtime files, test-package (CTF) release / JVM requirement;
  b. the "must run under mwpython" guard: guard-like lines in the
     MathWorks Python SDK sources (matlab_pysdk), what the opt-in
     neutralizer would replace, strings of the C extension / mwpython
     launcher / libmwmclmcrrt, exported mcl* symbols, headers;
  R0. reference: the test script run through `mpython` (mwpython.app);
  A*. option 2: plain python (no run loop), with/without -nojvm and with
      /without the guard bypass;
  B*. option 1: `python -m matlab_runtime.native` (mclRunMain + CFRunLoop
      on the main thread, python on a worker thread).

Every experiment runs in its own subprocess with a timeout. On timeout,
native stacks are sampled with `sample` (if available), then SIGABRT is
sent so that faulthandler dumps all Python thread stacks, then SIGKILL.
"""
import argparse
import io
import os
import os.path as op
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import zipfile

HERE = op.dirname(op.abspath(__file__))
REPO = op.dirname(HERE)
if op.isdir(op.join(REPO, "matlab_runtime")):
    sys.path.insert(0, REPO)
DEFAULT_TEST_PACKAGE = op.join(REPO, "tests", "_test_runtime")

RESULTS = []    # list of dict(name, status, summary, details)


def record(name, status, summary="", details=""):
    RESULTS.append(dict(name=name, status=status, summary=summary,
                        details=details))
    print(f"[{status:4s}] {name}: {summary}", flush=True)


def guarded(name):
    """Decorator: run a step, record FAIL with traceback on exception."""
    def deco(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception:
                record(name, "FAIL", "step raised an exception",
                       traceback.format_exc())
                return None
        return wrapper
    return deco


# ----------------------------------------------------------------------
#   helpers
# ----------------------------------------------------------------------

_PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")


def strings_matching(path, pattern, max_hits=80):
    rx = re.compile(pattern, re.IGNORECASE)
    with open(path, "rb") as f:
        data = f.read()
    hits = []
    for m in _PRINTABLE.finditer(data):
        s = m.group().decode("ascii", "replace")
        if rx.search(s) and s not in hits:
            hits.append(s)
            if len(hits) >= max_hits:
                hits.append("... (truncated)")
                break
    return hits


def run_tool(cmd, timeout=60):
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout + p.stderr
    except Exception as e:
        return f"<{cmd[0]} failed: {e}>"


def clip(text, head=80, tail=250):
    lines = (text or "").splitlines()
    if len(lines) <= head + tail:
        return "\n".join(lines)
    return "\n".join(lines[:head] + [
        f"... [{len(lines) - head - tail} lines omitted] ..."
    ] + lines[-tail:])


def ctf_info(pkgdir):
    """Release + need_java of the .ctf in a compiled package directory."""
    info = {}
    ctfs = [f for f in os.listdir(pkgdir) if f.endswith(".ctf")]
    if not ctfs:
        return info
    info["ctf"] = op.join(pkgdir, ctfs[0])
    with zipfile.ZipFile(info["ctf"]) as z:
        names = z.namelist()
        if "metadata/mwcoreProperties.xml" in names:
            txt = z.read("metadata/mwcoreProperties.xml").decode("utf8",
                                                                  "replace")
            m = re.search(r"<matlabRelease>(\w+)</matlabRelease>", txt)
            if m:
                info["release"] = m.group(1)
        if ".META/manifest.xml" in names:
            txt = z.read(".META/manifest.xml").decode("utf8", "replace")
            for key in ("need_java", "mcr-major-version",
                        "mcr-minor-version", "supported_archs"):
                m = re.search(key + r'="([^"]*)"', txt)
                if m:
                    info[key] = m.group(1)
    return info


# ----------------------------------------------------------------------
#   step a: environment
# ----------------------------------------------------------------------

CTX = {}


@guarded("a1 python/platform")
def step_platform():
    import sysconfig
    exe = op.realpath(sys.executable)
    lines = [
        f"platform        : {platform.platform()}",
        f"mac_ver         : {platform.mac_ver()}",
        f"machine/proc    : {platform.machine()} / {platform.processor()}",
        f"python          : {sys.version}",
        f"executable      : {sys.executable} -> {exe}",
        f"prefix          : {sys.prefix} (base {sys.base_prefix})",
        f"framework       : {sysconfig.get_config_var('PYTHONFRAMEWORK')!r}",
        f"conda           : {os.environ.get('CONDA_PREFIX', '')}",
        "DYLD_* env      : " + repr({k: v for k, v in os.environ.items()
                                       if k.startswith('DYLD_')}),
    ]
    sip = sys.platform == "darwin" and exe.startswith(
        ("/usr/bin/", "/bin/", "/System/"))
    if sip:
        lines.append("WARNING: interpreter under a SIP-protected path; "
                     "DYLD_* variables will be stripped.")
    record("a1 python/platform", "FAIL" if sip else "INFO",
           f"Python {platform.python_version()} on {platform.machine()}",
           "\n".join(lines))


@guarded("a2 matlab_runtime package")
def step_package():
    import matlab_runtime
    # importing the launcher module must be side-effect free
    native = __import__("matlab_runtime.native", fromlist=["STATE"])
    CTX["mr"] = matlab_runtime
    CTX["native_state"] = dict(native.STATE)
    record("a2 matlab_runtime package", "PASS",
           f"{matlab_runtime.__file__} (version "
           f"{getattr(matlab_runtime, '__version__', None)})")


@guarded("a3 test package (CTF)")
def step_ctf(pkgdir):
    if not pkgdir:
        record("a3 test package (CTF)", "SKIP", "no --test-package")
        return
    if not op.isdir(pkgdir):
        record("a3 test package (CTF)", "FAIL", f"not a directory: {pkgdir}")
        CTX["pkgdir"] = None
        return
    info = ctf_info(pkgdir)
    CTX["ctf"] = info
    warn = ""
    if info.get("need_java") == "1":
        warn = (" -- NOTE: CTF declares need_java=1; -nojvm experiments may "
                "fail for that reason alone.")
    record("a3 test package (CTF)", "INFO" if info else "FAIL",
           f"{info}{warn}")


@guarded("a4 runtime location")
def step_runtime(variant, mcrroot):
    from matlab_runtime import native
    from matlab_runtime.utils import guess_arch, SUPPORTED_PYTHON_VERSIONS
    arch = guess_arch()
    CTX["arch"] = arch
    if not variant:
        variant = CTX.get("ctf", {}).get("release") or "latest_installed"
    if mcrroot:
        # picked up by matlab_runtime (and inherited by experiments)
        os.environ["MATLAB_RUNTIME_PATH"] = op.abspath(mcrroot)
    root, release = native.find_mcrroot(variant, arch)
    CTX["mcrroot"], CTX["release"] = root, release
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    supported = SUPPORTED_PYTHON_VERSIONS.get(release or "", [])
    ok = pyver in supported
    ctf_rel = CTX.get("ctf", {}).get("release")
    details = (f"arch={arch} mcrroot={root} release={release}\n"
               f"python {pyver} supported by {release}: {ok} "
               f"(supported: {supported})")
    status = "PASS" if ok else "FAIL"
    if ctf_rel and release and ctf_rel != release:
        details += (f"\nWARNING: test CTF built with {ctf_rel} but runtime "
                    f"is {release}; pass --variant {ctf_rel}.")
        status = "FAIL"
    record("a4 runtime location", status, f"{root} ({release})", details)


@guarded("a5 runtime files")
def step_files():
    import glob
    root, arch = CTX["mcrroot"], CTX["arch"]
    pats = {
        "libmwmclmcrrt": f"{root}/runtime/{arch}/libmwmclmcrrt*",
        "cppext": f"{root}/extern/bin/{arch}/matlabruntimeforpython*",
        "pysdk": f"{root}/toolbox/compiler_sdk/pysdk_py/matlab_pysdk/"
                 f"runtime/*.py",
        "mwpython (script)": f"{root}/bin/mwpython*",
        "mwpython.app": f"{root}/bin/{arch}/mwpython*.app/Contents/MacOS/*",
        "headers": f"{root}/extern/include/mclmcr*.h",
    }
    lines, missing = [], []
    for key, pat in pats.items():
        hits = sorted(glob.glob(pat))
        CTX["files_" + key] = hits
        lines.append(f"{key:18s}: {hits if hits else 'NOT FOUND'}  ({pat})")
        if not hits and key in ("libmwmclmcrrt", "cppext", "pysdk"):
            missing.append(key)
    try:
        from matlab_runtime import native
        lib = native.find_mclmcrrt(root, arch)
        lines.append(f"launcher would load: {lib}")
    except Exception as e:
        lines.append(f"launcher cannot find libmwmclmcrrt: {e}")
        missing.append("libmwmclmcrrt (launcher)")
    record("a5 runtime files", "FAIL" if missing else "PASS",
           f"missing: {missing}" if missing else "all key files found",
           "\n".join(lines))


# ----------------------------------------------------------------------
#   step b: guard
# ----------------------------------------------------------------------

@guarded("b1 guard in matlab_pysdk sources")
def step_guard_sources():
    from matlab_runtime import native
    buf = io.StringIO()
    findings = native.diagnose_guard(CTX["mcrroot"], context=3, file=buf)
    # Dry-run of the opt-in neutralizer
    dry = []
    for d in native._pysdk_dirs(CTX["mcrroot"]):
        for root, _, files in os.walk(d):
            for name in sorted(files):
                if name.endswith(".py"):
                    path = op.join(root, name)
                    with open(path, "rb") as f:
                        import importlib.util
                        src = importlib.util.decode_source(f.read())
                    try:
                        _, rep = native._neutralize_source(src, path)
                    except SyntaxError as e:
                        dry.append(f"{path}: SyntaxError {e}")
                        continue
                    for lineno, text in rep:
                        dry.append(f"{path}:{lineno}: {text.strip()[:300]}")
    details = (buf.getvalue() + "\n--- neutralizer dry-run (would replace "
               "with warnings.warn) ---\n" + ("\n".join(dry) or "(nothing)"))
    hits_mw = [f for f in findings if "mwpython" in f["line"].lower()]
    record("b1 guard in matlab_pysdk sources", "INFO",
           f"{len(findings)} guard-like lines ({len(hits_mw)} mention "
           f"mwpython); neutralizer would replace {len(dry)} statement(s)",
           details)


@guarded("b2 strings in binaries")
def step_guard_binaries():
    pat = (r"mwpython|darwin|main.?thread|mclRunMain|CFRunLoop|mlstartup"
           r"|\b(MW|MATLAB|MCR)[A-Z0-9_]{3,}\b|getenv|nojvm|nodisplay")
    lines = []
    targets = (CTX.get("files_cppext", []) + CTX.get("files_mwpython.app", [])
               + CTX.get("files_libmwmclmcrrt", [])[:1])
    for path in targets:
        if op.isdir(path):
            continue
        try:
            hits = strings_matching(path, pat)
        except Exception as e:
            hits = [f"<error {e}>"]
        lines.append(f"=== strings {path} ({len(hits)} hits)")
        lines += ["    " + h for h in hits]
    # mwpython shell script(s): print in full (they are short)
    for path in CTX.get("files_mwpython (script)", []):
        if op.isfile(path) and os.path.getsize(path) < 200_000:
            with open(path, "rb") as f:
                head = f.read(2)
            if head == b"#!":
                with open(path, encoding="utf8", errors="replace") as f:
                    lines.append(f"=== {path} (script, full)\n{f.read()}")
    record("b2 strings in binaries", "INFO",
           f"{len(targets)} binaries scanned", "\n".join(lines))


@guarded("b3 symbols / linkage / headers")
def step_symbols():
    lines = []
    lib = (CTX.get("files_libmwmclmcrrt") or [None])[0]
    if lib:
        out = run_tool(["nm", "-gU", lib])
        if out is None:
            lines.append("nm not available (install Xcode command line "
                         "tools); falling back to strings")
            out = "\n".join(strings_matching(lib, r"^_?mcl", 400))
        sel = [x for x in out.splitlines()
               if re.search(r"mclmcrInitialize|mclRunMain|"
                            r"mclInitializeApplication|"
                            r"mclTerminateApplication", x)]
        lines.append(f"=== exported mcl symbols of {lib}")
        lines += ["    " + x for x in sel] or ["    (none found)"]
    for path in CTX.get("files_cppext", []):
        out = run_tool(["otool", "-L", path])
        if out:
            lines.append(f"=== otool -L {path}\n{out}")
        out = run_tool(["otool", "-l", path])
        if out:
            rp = re.findall(r"path (\S+) \(offset", out)
            lines.append(f"    LC_RPATH: {rp}")
    for h in CTX.get("files_headers", []):
        with open(h, encoding="utf8", errors="replace") as f:
            src = f.read().splitlines()
        for i, x in enumerate(src):
            if re.search(r"mclmcrInitialize|mclRunMain|mclMainFcnType", x):
                lines.append(f"{h}:{i + 1}: {x.strip()}")
    record("b3 symbols / linkage / headers", "INFO", "", "\n".join(lines))


# ----------------------------------------------------------------------
#   experiments
# ----------------------------------------------------------------------

EXPERIMENT_SCRIPT = r'''
import faulthandler, os, sys, threading, time
T0 = time.time()
faulthandler.enable()
_t = float(os.environ.get("DIAG_TIMEOUT", "300"))
if _t > 30:
    faulthandler.dump_traceback_later(_t - 20, exit=False)

def step(msg):
    print(f"@@STEP {time.time() - T0:6.1f}s {msg}", flush=True)

step(f"start: python={sys.executable} main_thread="
     f"{threading.current_thread() is threading.main_thread()} "
     f"DYLD_FALLBACK_LIBRARY_PATH="
     f"{os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '<unset>')[:120]}")
import matlab_runtime
step(f"imported matlab_runtime from {matlab_runtime.__file__}")
opts = [x for x in os.environ.get("DIAG_OPTIONS", "").split(",") if x]
env_opts = os.environ.get("MATLAB_RUNTIME_STARTUP_OPTIONS", "")
version = os.environ.get("DIAG_VARIANT") or "latest_installed"
step(f"init({version!r})")
matlab_runtime.init(version)
step("init OK (SDK + C extension imported)")
step(f"init_runtime({opts!r}) [env options: {env_opts!r}]")
matlab_runtime.init_runtime(opts)
step("init_runtime OK")
pkgdir = os.environ.get("DIAG_TEST_PACKAGE", "")
if pkgdir:
    sys.path.insert(0, os.path.dirname(pkgdir))
    name = os.path.basename(pkgdir.rstrip(os.sep))
    step(f"import_deployed({name!r})")
    mod = matlab_runtime.import_deployed(name)
    step("import_deployed OK")
    fn = os.environ.get("DIAG_TEST_FUNCTION", "test_runtime")
    out = getattr(mod, fn)()
    step(f"{fn}() returned {out!r}")
    print("@@RESULT", "success" if out == "success" else repr(out),
          flush=True)
else:
    print("@@RESULT init-only", flush=True)
'''

TRIVIAL_SCRIPT = r'''
import sys, threading
print("@@STEP callback ran; main_thread =",
      threading.current_thread() is threading.main_thread(), flush=True)
print("@@RESULT success", flush=True)
sys.exit(7)
'''

HINTS = [
    (r"mwpython", "message mentions mwpython: the guard (or a "
                  "mwpython-specific code path) triggered"),
    (r"Library not loaded|image not found|dlopen|no suitable image",
     "dynamic loader error: DYLD_FALLBACK_LIBRARY_PATH / rpath issue"),
    (r"NSInternalInconsistencyException|must be (called|used) (on|from) "
     r"the main thread|main thread", "main-thread requirement (AppKit/JVM)"),
    (r"\bjvm\b|\bjava\b", "JVM-related message"),
    (r"bypassed a suspected mwpython guard|-> warnings\.warn",
     "guard bypass was applied"),
    (r"Fatal Python error|Segmentation fault|SIGSEGV|SIGBUS",
     "native crash"),
]


def _signame(rc):
    if rc is not None and rc < 0:
        try:
            return signal.Signals(-rc).name
        except Exception:
            return f"signal {-rc}"
    return None


def run_experiment(name, description, cmd, env_extra, timeout, expect_rc=0):
    env = dict(os.environ)
    env["PYTHONFAULTHANDLER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["DIAG_TIMEOUT"] = str(timeout)
    pp = [x for x in env.get("PYTHONPATH", "").split(os.pathsep) if x]
    if op.isdir(op.join(REPO, "matlab_runtime")):
        pp = [REPO] + pp
    env["PYTHONPATH"] = os.pathsep.join(pp)
    env.update({k: v for k, v in env_extra.items() if v is not None})
    for k in [k for k, v in env_extra.items() if v is None]:
        env.pop(k, None)

    t0 = time.time()
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            errors="replace", start_new_session=True)
    timed_out = False
    native = ""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if shutil.which("sample"):
            with tempfile.NamedTemporaryFile("r", suffix=".txt",
                                             delete=False) as f:
                tmp = f.name
            subprocess.run(["sample", str(proc.pid), "3", "-file", tmp],
                           capture_output=True, timeout=60)
            try:
                with open(tmp, errors="replace") as f:
                    native = f.read()
            finally:
                os.unlink(tmp)
        try:
            os.killpg(proc.pid, signal.SIGABRT)  # faulthandler dump
            out, err = proc.communicate(timeout=15)
        except Exception:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
    dt = time.time() - t0
    rc = proc.returncode
    text = (out or "") + "\n" + (err or "")
    steps = [x for x in (out or "").splitlines() if x.startswith("@@STEP")]
    result = [x for x in (out or "").splitlines()
              if x.startswith("@@RESULT")]
    ok = (not timed_out and rc == expect_rc and result
          and result[-1].split(None, 1)[1].strip() in ("success",
                                                       "init-only"))
    if timed_out:
        summary = f"TIMEOUT after {timeout}s (likely deadlock)"
    elif ok:
        summary = f"rc={rc} {result[-1]} in {dt:.1f}s"
    else:
        summary = (f"rc={rc}{' (' + _signame(rc) + ')' if _signame(rc) else ''}"
                   f" expected {expect_rc}; result={result[-1:]}")
    last = steps[-1] if steps else "(no step reached)"
    summary += f"; last step: {last.replace('@@STEP', '').strip()[:120]}"
    # Ignore our own log lines (launcher / step markers) when looking for
    # hints; keep guard-bypass reports.
    hint_text = "\n".join(
        x for x in text.splitlines()
        if not x.startswith(("@@STEP", "[mpython-native]"))
        or "[guard-bypass]" in x
    )
    hints = [h for rx, h in HINTS
             if re.search(rx, hint_text, re.IGNORECASE)
             and not (timed_out and h == "native crash")]  # our SIGABRT
    details = "\n".join([
        f"description: {description}",
        f"command    : {cmd}",
        f"env extra  : {env_extra}",
        f"duration   : {dt:.1f}s  returncode: {rc}  timeout: {timed_out}",
        f"hints      : {hints}",
        "--- stdout ---", clip(out),
        "--- stderr ---", clip(err),
    ] + (["--- native stacks (sample) ---", clip(native, 150, 150)]
         if native else []))
    record(name, "PASS" if ok else "FAIL", summary, details)


def experiments(args, workdir):
    script = op.join(workdir, "diag_experiment.py")
    with open(script, "w") as f:
        f.write(EXPERIMENT_SCRIPT)
    trivial = op.join(workdir, "diag_trivial.py")
    with open(trivial, "w") as f:
        f.write(TRIVIAL_SCRIPT)

    py = sys.executable
    rel = CTX.get("release") or ""
    root = CTX.get("mcrroot")
    pkg = CTX.get("pkgdir") or ""
    base = {
        "DIAG_VARIANT": rel,
        "DIAG_TEST_PACKAGE": pkg,
        "DIAG_TEST_FUNCTION": args.test_function,
        "DIAG_OPTIONS": "",
        "MATLAB_RUNTIME_STARTUP_OPTIONS": None,
        "MATLAB_RUNTIME_BYPASS_GUARD": None,
        "MATLAB_RUNTIME_DIAGNOSE_GUARD": None,
    }

    def env(**kw):
        e = dict(base)
        e.update(kw)
        return e

    def lenv(**kw):  # launcher: point it at the located runtime
        return env(MATLAB_RUNTIME_PATH=root, **kw)

    launcher = [py, "-m", "matlab_runtime.native", "-verbose"]
    exps = [
        ("R0", "reference: mpython (MathWorks mwpython.app), JVM",
         [py, "-c", "import sys; from matlab_runtime.cli import mpython; "
          "sys.exit(mpython(sys.argv[1:]))",
          *(["-variant", rel] if rel else []), script],
         env(), 0),
        ("A0", "option 2 baseline: plain python, JVM, no guard bypass",
         [py, script], env(), 0),
        ("A1", "option 2: plain python, -nojvm, no guard bypass",
         [py, script], env(DIAG_OPTIONS="-nojvm"), 0),
        ("A2", "option 2: plain python, -nojvm, guard bypass",
         [py, script], env(DIAG_OPTIONS="-nojvm",
                           MATLAB_RUNTIME_BYPASS_GUARD="1"), 0),
        ("A3", "option 2 + DYLD env: launcher -no-runloop, -nojvm, bypass",
         launcher + ["-no-runloop", "-nojvm", "-bypass-guard", script],
         lenv(), 0),
        ("B0", "option 1 mechanism only: mclRunMain runs a trivial "
               "callback (expects exit code 7)",
         launcher + [trivial], lenv(DIAG_TEST_PACKAGE=""), 7),
        ("B1", "option 1: launcher (mclRunMain), JVM, no guard bypass",
         launcher + [script], lenv(), 0),
        ("B2", "option 1: launcher (mclRunMain), JVM, guard bypass",
         launcher + ["-bypass-guard", script], lenv(), 0),
        ("B3", "option 1: launcher (mclRunMain), -nojvm, guard bypass",
         launcher + ["-nojvm", "-bypass-guard", script], lenv(), 0),
    ]
    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    for name, desc, cmd, e, rc in exps:
        if only and name not in only:
            continue
        if not root:
            record(name, "SKIP", "no runtime located")
            continue
        print(f"... running {name}: {desc} (timeout {args.timeout}s)",
              flush=True)
        try:
            run_experiment(name, desc, cmd, e, args.timeout, rc)
        except Exception:
            record(name, "FAIL", "harness error", traceback.format_exc())


# ----------------------------------------------------------------------
#   report
# ----------------------------------------------------------------------

def write_report(path):
    lines = [
        "matlab-runtime macOS mwpython-bypass diagnostic report",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"python: {sys.version.split()[0]} ({sys.executable})",
        f"platform: {platform.platform()}",
        "",
        "SUMMARY",
        "=======",
    ]
    for r in RESULTS:
        lines.append(f"[{r['status']:4s}] {r['name']}: {r['summary']}")
    lines += ["", "DETAILS", "======="]
    for r in RESULTS:
        lines += ["", "#" * 72, f"# [{r['status']}] {r['name']}",
                  "#" * 72, r["summary"], r["details"]]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=textwrap.dedent(__doc__.split("Steps")[0]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--variant", help="Runtime release (default: release of "
                   "the test package CTF, else latest installed)")
    p.add_argument("--mcrroot", help="Explicit runtime root directory")
    p.add_argument("--test-package", default=DEFAULT_TEST_PACKAGE,
                   help="Directory of a MATLAB-compiled python package "
                        "(containing <name>.ctf); '' to only init the "
                        "runtime. Default: tests/_test_runtime (R2024b)")
    p.add_argument("--test-function", default="test_runtime")
    p.add_argument("--timeout", type=float, default=300,
                   help="Per-experiment timeout in seconds (default 300)")
    p.add_argument("--only", help="Comma-separated experiments to run "
                   "(R0,A0,A1,A2,A3,B0,B1,B2,B3)")
    p.add_argument("--no-experiments", action="store_true")
    p.add_argument("--output", default="native_diagnose_report.txt")
    p.add_argument("--allow-non-mac", action="store_true",
                   help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if sys.platform != "darwin" and not args.allow_non_mac:
        print("This diagnostic is meant to be run on macOS "
              f"(this is {sys.platform}). Nothing to do.")
        return 0

    CTX["pkgdir"] = op.abspath(args.test_package) if args.test_package \
        else None

    step_platform()
    step_package()
    if "mr" not in CTX:
        write_report(args.output)
        print(f"\nReport written to {op.abspath(args.output)}")
        return 1
    step_ctf(CTX["pkgdir"])
    step_runtime(args.variant, args.mcrroot)
    if CTX.get("mcrroot"):
        step_files()
        step_guard_sources()
        step_guard_binaries()
        step_symbols()
    else:
        record("b* guard inspection", "SKIP", "no runtime located")

    if not args.no_experiments:
        with tempfile.TemporaryDirectory() as workdir:
            experiments(args, workdir)

    write_report(args.output)
    print(f"\nReport written to {op.abspath(args.output)}")
    print("Please paste its content (or attach the file) back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
