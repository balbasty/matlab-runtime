import os
import os.path as op
import shutil
import subprocess
import tempfile

from .utils import (
    askuser,
    guess_arch,
    guess_prefix,
    guess_installer,
    guess_version,
    macos_version,
    translate_version,
    url_download,
    ZipFileWithExecPerm,
)


def install(version=None, prefix=None, auto_answer=False):
    """
    Install the matlab runtime.

    Parameters
    ----------
    version : [list of] str, default="latest"
        MATLAB version.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    default_answer : bool
        Default always to all questions.
        **This entails accepting the MATLAB Runtime license agreement.**

    Returns
    -------
    prefix : [list of] str
        Installation prefix

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
    """
    if isinstance(version, (list, tuple, set)):
        return type(version)(
            map(lambda x: install(x, prefix, auto_answer), version)
        )

    license = "matlabruntime_license_agreement.pdf"
    arch = guess_arch()
    version = guess_version(version or "latest", arch)
    url = guess_installer(version, arch)

    if prefix is None:
        prefix = guess_prefix()

    # --- check already exists -----------------------------------------
    if op.exists(op.join(prefix, version, license)):
        ok = askuser("Runtime already exists. Reinstall?", "no", auto_answer)
        if not ok:
            print("Do not reinstall:", op.join(prefix, version))
            return prefix
        print("Runtime already exists. Reinstalling...")

    # --- download -----------------------------------------------------

    askuser(f"Download installer from {url}?", "yes", auto_answer, True)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(tmpdir, exist_ok=True)

        print(f"Downloading from {url} ...")
        installer = url_download(url, tmpdir)
        print("done ->", installer)

        # --- unzip ----------------------------------------------------
        if installer.endswith(".zip"):

            askuser(f"Unzip {installer}?", "yes", auto_answer, True)

            with ZipFileWithExecPerm(installer) as zip:
                zip.extractall(tmpdir)

            if arch[:3] == "win":
                installer = op.join(tmpdir, "setup.exe")
            else:
                installer = op.join(tmpdir, "install")

        if not op.exists(installer):
            raise FileNotFoundError("No installer found in archive")

        # --- install --------------------------------------------------

        question = (
            "By running this code, you agree to the MATLAB Runtime "
            "license agreement:\n"
            f"\t{op.join(tmpdir, 'matlabruntime_license_agreement.pdf')}\n"
        )
        askuser(question, "yes", auto_answer, True)
        print("License agreed.")

        if arch[:3] == "mac" and macos_version() > (10, 14):
            print(
                "Running the MATLAB installer requires signing off its "
                "binaries, which requires sudo:"
            )
            subprocess.call([
                "sudo", "xattr", "-r", "-d", "com.apple.quarantine", tmpdir
            ])

        subprocess.call([
            installer,
            "-destinationFolder", prefix,
            "-tmpdir", tmpdir,
            "-mode", "silent",
            "-agreeToLicense", "yes"
        ])

        # --- check ----------------------------------------------------
        if not op.exists(op.join(prefix, version, license)):
            raise RuntimeError("Runtime not found where it is expected.")

        license = op.join(prefix, version, license)
        print("Runtime succesfully installed at:", op.join(prefix, version))
        print("License agreement available at:", license)

    # --- all done! ----------------------------------------------------
    return prefix


def uninstall(version=None, prefix=None, yes=False):
    """
    Uninstall the matlab runtime.

    Parameters
    ----------
    version : [list of] str, default="all"
        MATLAB version.
    prefix : str, optional
        Install location. Default:
        * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
        * Linux:    /usr/local/MATLAB/MATLAB_Runtime
        * MacOS:    /Applications/MATLAB/MATLAB_Runtime
    yes : bool
        Always say yes when asked a question.
        **This entails accepting the MATLAB Runtime license agreement.**

    Raises
    ------
    UserInterruptionError
        If the user answers no to a question.
    """
    if isinstance(version, (list, tuple, set)):
        for a_version in version:
            try:
                uninstall(a_version, prefix, yes)
            except Exception as e:
                print(f"[{type(e)}] Failed to uninstall runtime:", version, e)
        return

    arch = guess_arch()
    version = version or "all"
    if version != "all":
        version = translate_version(version)

    if prefix is None:
        prefix = guess_prefix()

    if version.lower() == "all":
        rmdir = prefix
    else:
        rmdir = op.join(rmdir, version)

    askuser(f"Remove directory {rmdir} and its content?", "yes", yes, True)

    if arch[:3] == "win":
        if version == "all":
            versions = [op.join(prefix, ver) for ver in os.listdir(prefix)]
        else:
            versions = [version]
        for ver in versions:
            subprocess.call(op.join(
                prefix, ver, "bin", arch, "Uninstall_MATLAB_Runtime.exe"
            ))
    else:
        shutil.rmtree(rmdir)

    print("Runtime succesfully uninstalled from:", rmdir)
