__all__ = [
    "install",
    "uninstall",
    "init_sdk",
    "import_deployed",
    "init_runtime",
    "terminate_runtime",
    "guess_prefix",
    "guess_arch",
]
import atexit
import importlib
import os
import os.path as op
import shutil
import subprocess
import sys
import tempfile
import warnings
import weakref

from .utils import (
    askuser,
    guess_arch,
    guess_prefix,
    guess_installer,
    guess_release,
    macos_version,
    matlab_release,
    matlab_version,
    guess_pymatlab_version,
    url_download,
    ZipFileWithExecPerm,
)


def install(version=None, prefix=None, auto_answer=False):
    """
    Install the matlab runtime.

    !!! warning
        BY SETTING `default_answer=True`, YOU ACCEPT THE TERMS OF THE
        MATLAB RUNTIME LICENSE. THE MATLAB RUNTIME INSTALLER WILL BE
        RUN WITH THE ARGUMENT `-agreeToLicense yes`.
        IF YOU ARE NOT WILLING TO DO SO, DO NOT CALL THIS FUNCTION.

        https://mathworks.com/help/compiler/install-the-matlab-runtime.html

    Parameters
    ----------
    version : [list of] str, default="latest"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    default_answer : bool
        Default answer to all questions.

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
    """
    # --- iterate if multiple versions  --------------------------------
    if isinstance(version, (list, tuple, set)):
        map(lambda x: install(x, prefix, auto_answer), version)
        return

    # --- prepare  -----------------------------------------------------
    license = "matlabruntime_license_agreement.pdf"
    arch = guess_arch()
    version = guess_release(version or "latest", arch)
    url = guess_installer(version, arch)
    raise_if_no = True

    if prefix is None:
        prefix = guess_prefix()
    prefix = op.realpath(op.abspath(prefix))

    # --- check already exists -----------------------------------------
    if op.exists(op.join(prefix, version, license)):
        # Do not raise_if_no so that we can exit peacefully
        ok = askuser("Runtime already exists. Reinstall?", "no", auto_answer)
        if not ok:
            print("Do not reinstall:", op.join(prefix, version))
            return
        print("Runtime already exists. Reinstalling...")

    # --- download -----------------------------------------------------

    askuser(f"Download installer from {url}?", "yes", auto_answer, raise_if_no)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = op.realpath(op.abspath(tmpdir))

        print(f"Downloading from {url} ...")
        installer = url_download(url, tmpdir)
        print("done ->", installer)

        # --- unzip ----------------------------------------------------
        if installer.endswith(".zip"):

            askuser(f"Unzip {installer}?", "yes", auto_answer, raise_if_no)
            print(f"Unzipping {installer} ...")

            with ZipFileWithExecPerm(installer) as zip:
                zip.extractall(tmpdir)

            if arch[:3] == "win":
                installer = op.join(tmpdir, "setup.exe")
            else:
                installer = op.join(tmpdir, "install")
            print("done ->", installer)

        if not op.exists(installer):
            print("No installer found in archive:", os.listdir(tmpdir))
            raise FileNotFoundError("No installer found in archive")

        # --- install --------------------------------------------------

        question = (
            "BY ENTERING 'YES', YOU ACCEPT THE TERMS OF THE MATLAB RUNTIME "
            "LICENSE, LINKED BELOW. THE MATLAB RUNTIME INSTALLER WILL BE "
            "RUN WITH THE ARGUMENT `-agreeToLicense yes`. "
            "IF YOU ARE NOT WILLING TO DO SO, ENTER 'NO' AND THE "
            "INSTALLATION WILL BE ABORTED."
            f"\t{op.join(tmpdir, 'matlabruntime_license_agreement.pdf')}\n"
        )
        askuser(question, "yes", auto_answer, raise_if_no)
        print("License agreed.")

        if arch[:3] == "mac" and macos_version() > (10, 14):
            print(
                "Running the MATLAB installer requires signing off its "
                "binaries, which requires sudo:"
            )
            subprocess.call([
                "sudo", "xattr", "-r", "-d", "com.apple.quarantine", tmpdir
            ])

        call = [
            installer,
            "-agreeToLicense", "yes",
            "-mode", "silent",
            "-destinationFolder", prefix,
            "-tmpdir", tmpdir,
        ]
        print("Installing", call, "...")
        ret = subprocess.run(call).returncode
        if ret:
            print("Installation failed?")
        else:
            print("done ->", op.join(prefix, version))

        # --- check ----------------------------------------------------
        path_installed = op.join(prefix, version)
        if not op.exists(op.join(path_installed, license)):
            if op.exists(path_installed):
                print(
                    "Runtime not found where it is expected (v):",
                    os.listdir(path_installed)
                )
            elif op.exists(prefix):
                print(
                    "Runtime not found where it is expected (p):",
                    os.listdir(prefix)
                )
            raise FileNotFoundError("Runtime not found where it is expected.")

        license = op.join(prefix, version, license)
        print("Runtime succesfully installed at:", op.join(prefix, version))
        print("License agreement available at:", license)

    # --- all done! ----------------------------------------------------
    return


def uninstall(version=None, prefix=None, auto_answer=False):
    """
    Uninstall the matlab runtime.

    Parameters
    ----------
    version : [list of] str, default="all"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'all', uninstall all installed versions.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    auto_answer : bool
        Default answer to all questions.
    """
    # --- iterate if multiple versions  --------------------------------
    if isinstance(version, (list, tuple, set)):
        for a_version in version:
            try:
                uninstall(a_version, prefix, auto_answer)
            except Exception as e:
                print(f"[{type(e)}] Failed to uninstall runtime:", version, e)
        return

    # --- prepare  -----------------------------------------------------
    raise_if_no = True
    arch = guess_arch()
    version = version or "all"
    if version != "all":
        version = matlab_release(version)

    if prefix is None:
        prefix = guess_prefix()

    if version.lower() == "all":
        rmdir = prefix
    else:
        rmdir = op.join(rmdir, version)

    # --- uninstall  ---------------------------------------------------
    question = f"Remove directory {rmdir} and its content?"
    askuser(question, "yes", auto_answer, raise_if_no)

    if arch[:3] == "win":
        # --- Windows: call uninstaller ---
        if version == "all":
            versions = [op.join(prefix, ver) for ver in os.listdir(prefix)]
        else:
            versions = [version]
        for ver in versions:
            subprocess.call(op.join(
                prefix, ver, "bin", arch, "Uninstall_MATLAB_Runtime.exe"
            ))
    else:
        # --- Unix: remove folder ---
        shutil.rmtree(rmdir)

    print("Runtime(s) succesfully uninstalled from:", rmdir)


_DEPLOYED_MODULES = {}
_INITIALIZED = {"SDK": False, "RUNTIME": False}
_CPP = "matlabruntimeforpython_abi3"
_SDK = "matlab_pysdk.runtime"
_MLB = "matlab"


def init_sdk(
    version="latest_installed",
    install_if_missing=False,
    prefix=None,
    auto_answer=False,
):
    """
    Set current environment so that the MATLAB Python SDK is properly
    linked and usable.

    Parameters
    ----------
    version : [list of] str, default="latest_installed"
        MATLAB version, such as 'latest' or 'R2022b' or '9.13'.
        If 'latest_installed', use the most recent currently installed
        version. If no version is installed, equivalent to 'latest'.
    install_if_missing : bool
        If target version is missing, run installer.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    default_answer : bool
        Default answer to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**
    """
    if _INITIALIZED["SDK"]:
        raise ValueError("MATLAB SDK already initialized")

    # --- prepare  -----------------------------------------------------
    arch = guess_arch()
    license = "matlabruntime_license_agreement.pdf"
    version = version or "latest_installed"
    if prefix is None:
        prefix = guess_prefix()

    # --- find version  ------------------------------------------------
    version = guess_release(version, arch)

    # --- install version  ---------------------------------------------
    if not op.exists(op.join(prefix, version, license)):
        if install_if_missing:
            install(version, prefix, auto_answer)
        else:
            raise FileNotFoundError(
                "Version not found in installed runtimes:",
                op.join(prefix, version)
            )

    # --- prepare paths  -----------------------------------------------
    prefix = op.join(prefix, version)
    ext = op.join(prefix, 'extern', 'bin', arch)
    sdk = op.join(prefix, 'toolbox', 'compiler_sdk', 'pysdk_py')
    mod = op.join(sdk, 'matlab_mod_dist')
    bin = op.join(prefix, 'bin', arch)

    # --- set paths  ---------------------------------------------------
    if arch[:3] == "win":
        os.environ['PATH'] = os.pathsep.join(ext, bin, os.environ['PATH'])

    sys.path.insert(0, bin)
    sys.path.insert(0, mod)
    sys.path.insert(0, sdk)
    sys.path.insert(0, ext)

    # --- imports  -----------------------------------------------------
    importlib.import_module(_CPP)
    importlib.import_module(_SDK)
    matlab = importlib.import_module(_MLB)

    # --- check version  -----------------------------------------------
    current_version = guess_pymatlab_version(matlab)
    target_version = matlab_version(version)
    current_version = tuple(map(int, current_version.split(".")[:2]))
    target_version = tuple(map(int, target_version.split(".")[:2]))

    if current_version != target_version:
        raise RuntimeError(
            f'Runtime version of package ({target_version}) does not match '
            f'runtime version of previously loaded package ({current_version})'
        )

    _INITIALIZED["SDK"] = True


class _PathInitializer:
    def __init__(self):
        if not _INITIALIZED["SDK"]:
            init_sdk()
        self.cppext_handle = importlib.import_module(_CPP)


def import_deployed(*packages):
    """
    Initialize compiled MATLAB packages so that they can be used from python.

    Parameters
    ----------
    *packages : module | str
        Python package that contains a compiled MATLAB package (a ctf file).

    Returns
    -------
    *modules : module
        Imported MATLAB modules.
    """
    if not _INITIALIZED["RUNTIME"]:
        init_runtime()

    sdk = importlib.import_module(_SDK)

    handles = []
    for package in packages:
        if isinstance(package, str):
            package = importlib.import_module(package)
        handle = _DEPLOYED_MODULES.get(package, lambda: None)()
        if handle is None:
            handle = sdk.DeployablePackage(
                _PathInitializer(), package.__name__, package.__file__
            )
            handle.initialize()
            _DEPLOYED_MODULES[package] = weakref.ref(handle)
        handles.append(handle)

    return handles[0] if len(handles) == 1 else tuple(handles)


def init_runtime(option_list=tuple()):
    """
    Initialize the MATLAB runtime. The SDK must have already been initialized.

    Parameters
    ----------
    option_list : list[str]
        Options passed to MATLAB.
    """
    if _INITIALIZED.get("RUNTIME", False):
        raise ValueError("MATLAB runtime already initialized")
    if not _INITIALIZED.get("SDK", False):
        init_sdk()

    cppext = importlib.import_module(_CPP)

    arch = guess_arch()
    if arch[:3] == "mac":
        ignored_option_found = False
        for option in option_list:
            if option in ('-nodisplay', '-nojvm'):
                ignored_option_found = True
                break
        if ignored_option_found:
            warnings.warn(
                'Options "-nodisplay" and "-nojvm" are ignored on Mac.'
                'They must be passed to mwpython in order to take effect.'
            )
    cppext.initializeApplication(option_list)
    _INITIALIZED["RUNTIME"] = True


def terminate_runtime():
    """
    Terminate runtime.
    """
    if _INITIALIZED["RUNTIME"]:
        cppext = importlib.import_module("matlabruntimeforpython_abi3")
        cppext.terminateApplication()
        _INITIALIZED["RUNTIME"] = False


@atexit.register
def __atexit():
    for package in _DEPLOYED_MODULES.values():
        if package() is not None:
            package().terminate()
    terminate_runtime()
