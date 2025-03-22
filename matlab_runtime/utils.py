import os
import os.path as op
import platform
import shutil
import stat
import sys
import tempfile
import zipfile
import tarfile
from datetime import datetime
from urllib import error, parse, request
from xml.etree import ElementTree


# ----------------------------------------------------------------------
#   EXCEPTIONS
# ----------------------------------------------------------------------


class VersionNotFoundError(RuntimeError):
    ...


class DownloadError(RuntimeError):
    ...


class UnsupportedArchError(RuntimeError):
    ...


class UserInterruptionError(RuntimeError):
    ...


# ----------------------------------------------------------------------
#   USER INPUT
# ----------------------------------------------------------------------


def askuser(question, default="yes", auto_answer=False, raise_if_no=False):
    options = "([yes]/no)" if default == "yes" else "(yes/[no])"
    if auto_answer:
        yesno = True if default == "yes" else False
    else:
        yesno = input(f"{question} {options}").strip()
        yesno = (not yesno) if default == "yes" else False
        yesno = yesno or yesno[:1].lower() == "y"
    if not yesno and raise_if_no:
        raise UserInterruptionError(question)
    return yesno


# ----------------------------------------------------------------------
#   UNZIP WITH EXEC PERMISSION + SYMLINKS
# ----------------------------------------------------------------------

# Running the matlab installer fails if I naively unzip using ZipFile.
# That's because (unlike the `unzip` tool on unix), ZipFile does not
# preserve executable permissions, and does not preserve symlinks.
# This somehow breaks the linkking of the dylibs (on mac -- but probably
# also on linux). This patched ZipFile fixes these two issues.


class ZipFileWithExecPerm(zipfile.ZipFile):

    def _extract_member(self, member, targetpath, pwd):
        if not isinstance(member, zipfile.ZipInfo):
            member = self.getinfo(member)

        targetpath = super()._extract_member(member, targetpath, pwd)

        attr = member.external_attr >> 16

        # https://bugs.python.org/issue27318
        if (
            platform.system() != "Windows" and
            stat.S_ISLNK(attr) and
            hasattr(os, "symlink")
        ):
            link = self.open(member, pwd=pwd).read()
            shutil.move(targetpath, targetpath + ".__backup__")
            try:
                return os.symlink(link, targetpath)
            except OSError:     # No permission to create symlink
                shutil.move(targetpath + ".__backup__", targetpath)
                pass

        # https://stackoverflow.com/questions/39296101
        if attr != 0:
            os.chmod(targetpath, attr)

        return targetpath


# ----------------------------------------------------------------------
#   URL REQUESTS
# ----------------------------------------------------------------------


class NoRedirection(request.HTTPErrorProcessor):
    # https://stackoverflow.com/questions/29327674
    def http_response(self, request, response):
        return response
    https_response = http_response


def url_exists(url):
    opener = request.build_opener(NoRedirection)
    req = request.Request(url, method="HEAD")
    try:
        with opener.open(req) as res:
            status = res.status
        return status < 400
    except error.HTTPError:
        return False


def url_download(url, out, retry=5):
    if op.isdir(out):
        basename = op.basename(parse.urlparse(url).path)
        out = op.join(out, basename)

    res = exc = None
    for _ in range(retry):
        try:
            res = request.urlretrieve(url, out)
            break
        except Exception as e:
            exc = e

    if res is None:
        raise DownloadError(str(exc))

    return out


_HOMEBREW_VERSIONS = {"openssl": "3.4.1"}
_HOMEBREW_DIGESTS = {
    "openssl": {
        "3.4.1": {
            "maca64": {
                15: "b20c7d9b63e7b320cba173c11710dee9888c77175a841031d7a245bb37355b98",  # noqa: E501
                14: "cdc22c278167801e3876a4560eac469cfa7f86c6958537d84d21bda3caf6972c",  # noqa: E501
                13: "51383da8b5d48f24b1d7a7f218cce1e309b6e299ae2dc5cfce5d69ff90c6e629",  # noqa: E501
            },
            "maci64": {
                15: "e8a8957f282b27371283b8c7a17e743c1c4e4e242ea7ee68bbe23f883da4948f",  # noqa: E501
                14: "36a85e5161befce49de6e03c5f710987bd5778a321151e011999e766249e6447",  # noqa: E501
                13: "523d64d10d1d44d6e39df3ced3539e2526357eab8573d2de41d4e116d7c629c8",  # noqa: E501
            },
        }
    }
}


def download_bottle(package, version=None, digest=None, variant=None, out="."):
    # Download a Homebrew bottle (= build package)
    headers = (
        ("Authorization", "Bearer QQ=="),
        ("Accept", "application/vnd.oci.image.layer.v1.tar+gzip"),
    )
    opener = request.build_opener()
    opener.addheaders = headers
    request.install_opener(opener)

    try:
        arch = guess_arch()
        macver = macos_version()[0]
        version = version or _HOMEBREW_VERSIONS[package]
        if not digest:
            digesters = _HOMEBREW_DIGESTS[package][version][arch]
            if macver not in digesters:
                if macver < min(digesters):
                    digest = digesters[min(digesters)]
                elif macver > max(digesters):
                    digest = digesters[max(digesters)]
                else:
                    assert False
            else:
                digest = digesters[macver]

        if variant:
            package_path = f"{package}/{variant}"
        else:
            package_path = package
        url = f"https://ghcr.io/v2/homebrew/core/{package_path}/blobs/sha256:{digest}"  # noqa: E501
        name = f"{package}-{version}.bottle.tar.gz"
        if op.isdir(out):
            out = op.join(out, name)
        return url_download(url, out)

    finally:
        request.install_opener(None)


def patch_libcrypto(matlab_path):
    # Required on MacOS
    arch = guess_arch()
    libcrypto_path = op.join(matlab_path, "bin", arch, "libcrypto.3.dylib")

    version = _HOMEBREW_VERSIONS["openssl"]

    shutil.move(libcrypto_path, libcrypto_path + ".tmp", )
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            gzipfile = download_bottle("openssl", variant="3", out=tmpdir)
            with tarfile.open(gzipfile, "r:gz") as f:
                f.extractall(tmpdir)
            libcrypto_path_new = op.join(
                tmpdir, "openssl@3", version, "lib", "libcrypto.3.dylib"
            )
            shutil.move(libcrypto_path_new, libcrypto_path)

    except Exception as e:
        shutil.move(libcrypto_path + ".tmp", libcrypto_path)
        raise e


def patch_runtime(matlab_path):
    arch = guess_arch()
    if arch[:3] == "mac":
        patch_libcrypto(matlab_path)


# ----------------------------------------------------------------------
#   SYSTEM/ARCH
# ----------------------------------------------------------------------


def guess_arch():

    try:
        arch = {
            "Darwin": "mac",
            "Windows": "win",
            "Linux": "glnx",
        }[platform.system()]
    except KeyError:
        raise UnsupportedArchError(sys.platform)

    if arch == "mac":
        if platform.processor() == "arm":
            arch += "a"
        else:
            arch += "i"
        arch += "64"
    elif arch == "win":
        if sys.maxsize > 2**32:
            arch += "64"
        else:
            arch += "32"
    elif arch == "glnx":
        if sys.maxsize > 2**32:
            arch += "a64"
        else:
            arch += "86"

    return arch


def macos_version():
    ver = platform.platform().split("-")[1]
    ver = tuple(map(int, ver.split(".")))
    return ver


CANDIDATE_LOCATIONS_BY_OS = {
    "win": [
        "C:\\Program Files\\MATLAB\\MATLAB Runtime\\{release}",
        "C:\\Program Files (x86)\\MATLAB\\MATLAB Runtime\\{release}",
        "C:\\Program Files\\MATLAB\\{release}",
        "C:\\Program Files (x86)\\MATLAB\\{release}",
    ],
    "gln": [
        "/usr/local/MATLAB/MATLAB_Runtime/{release}",
        "/usr/local/MATLAB/{release}"
    ],
    "mac": [
        "/Applications/MATLAB/MATLAB_Runtime/{release}",
        "/Applications/MATLAB_{release}.app",
        "/Applications/MATLAB/{release}"
    ]
}


def iter_existing_installations(variant='latest_installed'):
    """
    Iterate over MATLAB and MATLAB Runtime installations in common location.

    If variant is "latest_installed", the function will return the latest
    installed version. Otherwise, it will return the versions that
    matches the variant.

    Yields
    ------
    path : str
        Path to the installation
    variant : str
        Version of the installation
    """
    arch = guess_arch()
    bases = CANDIDATE_LOCATIONS_BY_OS[arch[:3]]

    if os.environ.get("MATLAB_RUNTIME_PATH", ""):
        yield (os.environ["MATLAB_RUNTIME_PATH"], variant)

    import re
    import glob

    if variant == "latest_installed":
        pattern = re.compile(r"R\d{4}[ab]")
    else:
        pattern = re.compile(variant)

    paths = []
    for base in bases:
        try:
            for path in glob.glob(base.format(release="*")):
                search = re.search(pattern, path)
                if search:
                    paths.append((base, path, search.group()))
        except FileNotFoundError:
            continue

    def sort_paths(path_tuple):
        return (path_tuple[2], bases[::-1].index(path_tuple[0]))

    for _, path, ver in sorted(paths, reverse=True, key=sort_paths):
        yield path, ver


def guess_prefix():
    """
    Guess the MATLAB Runtime installation prefix.

    If the environment variable `"MATLAB_RUNTIME_PATH"` is set, return it.

    Otherwise, the default prefix is platform-specific:

    * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
    * Linux:    /usr/local/MATLAB/MATLAB_Runtime
    * MacOS:    /Applications/MATLAB/MATLAB_Runtime

    Returns
    -------
    prefix : str
    """
    if os.environ.get("MATLAB_RUNTIME_PATH", ""):
        return os.environ["MATLAB_RUNTIME_PATH"]

    arch = guess_arch()
    if arch[:3] == "win":
        return "C:\\Program Files\\MATLAB\\MATLAB Runtime\\"
    if arch[:3] == "gln":
        return "/usr/local/MATLAB/MATLAB_Runtime"
    if arch[:3] == "mac":
        return "/Applications/MATLAB/MATLAB_Runtime"
    assert False


def find_runtime(version, prefix=None):
    """
    Find an installed MATLAB runtime with a specific version.
    """
    version = matlab_release(version)
    version_info = "VersionInfo.xml"

    # Check under prefix
    if prefix is None:
        prefix = guess_prefix()
    if op.exists(op.join(prefix, version, version_info)):
        return op.join(prefix, version)

    # Check if MATLAB_PATH is set
    if os.environ.get("MATLAB_PATH", ""):
        path = os.environ["MATLAB_PATH"].rstrip(op.sep)
        if guess_matlab_release(path) == version:
            return path

    # Look for other known locations
    arch = guess_arch()
    if arch[:3] == "win":
        bases = [
            "C:\\Program Files (x86)\\MATLAB\\MATLAB Runtime\\{release}"
            "C:\\Program Files\\MATLAB\\MATLAB Runtime\\{release}"
            "C:\\Program Files\\MATLAB\\{release}"
            "C:\\Program Files (x86)\\MATLAB\\{release}"
        ]
    elif arch[:3] == "gln":
        bases = [
            "/usr/local/MATLAB/MATLAB_Runtime/{release}"
            "/usr/local/MATLAB/{release}"
        ]
    elif arch[:3] == "mac":
        bases = [
            "/Applications/MATLAB/MATLAB_Runtime/{release}"
            "/Applications/MATLAB_{release}.app"
            "/Applications/MATLAB_{release}"
            "/Applications/MATLAB/{release}"
        ]
    for base in bases:
        base = base.format(release=version)
        if op.exists(op.join(base, "VersionInfo.xml")):
            return base

    # Check whether a matlab binary is on the path
    path = shutil.which("matlab")
    if path:
        path = op.realpath(path)
        path = op.realpath(op.join(op.dirname(path), ".."))
        if guess_matlab_release(path) == version:
            return path

    # Nothing -> return
    return None


# ----------------------------------------------------------------------
#   MATLAB SDK
# ----------------------------------------------------------------------


def guess_pymatlab_version(matlab):
    """Guess dot-version of loaded matlab module."""
    return _guess_pymatlab_version(matlab, "version")


def guess_pymatlab_release(matlab):
    """Guess release of loaded matlab module."""
    return _guess_pymatlab_version(matlab, "release")


def _guess_pymatlab_version(matlab, key):
    return _guess_matlab_version(matlab.get_arch_filename(), key)


def guess_matlab_version(path):
    """Guess dot-version of matlab package installed at path."""
    return _guess_matlab_version(path, "version")


def guess_matlab_release(path):
    """Guess release of matlab package installed at path."""
    return _guess_matlab_version(path, "release")


def _guess_matlab_version(path, key):
    while path:
        if op.exists(op.join(path, 'VersionInfo.xml')):
            path = op.join(path, 'VersionInfo.xml')
            tree = ElementTree.parse(path)
            version = tree.find(key).text
            return version
        else:
            path = op.dirname(path)
    raise ValueError(f"Could not guess matlab {key} from python module")


# ----------------------------------------------------------------------
#   INSTALLERS
# ----------------------------------------------------------------------


def matlab_release(version):
    """Convert MATLAB version (e.g. 24.2) to release (e.g. R2024b)."""
    if isinstance(version, (list, tuple)):
        version = ".".join(map(str(version[:2])))
    if version[:1] == "R":
        return version
    if version in VERSION_TO_RELEASE:
        return VERSION_TO_RELEASE[version]
    year, release, *_ = version.split(".")
    return "R20" + year + ("abcdefghijklmnopqrstuvwxy"[int(release)+1])


def matlab_version(version):
    """Convert MATLAB release (e.g. R2024b) to version (e.g. 24.2)."""
    # 1. look for version in dict of known versions
    if isinstance(version, (list, tuple)):
        version = ".".join(map(str(version[:2])))
    for runtime_version, matlab_version in VERSION_TO_RELEASE.items():
        if version in (runtime_version, matlab_version):
            return runtime_version
    # 2. if does not look like a matlab version, hope it's a runtime version
    if version[:1] != "R":
        return version
    # 3. convert matlab version to runtime version using new scheme
    year, letter = version[3:5], version[5]
    return year + "." + str("abcdefghijklmnopqrstuvwxy".index(letter) + 1)


def guess_release(version, arch=None, prefix=None):
    """Guess version (if "latest") + convert to MATLAB release (e.g. R2024b)"""
    arch = arch or guess_arch()
    if version.lower() == "latest_installed":
        if prefix is None:
            prefix = guess_prefix()
        if op.exists(prefix):
            license = "matlabruntime_license_agreement.pdf"
            for name in sorted(os.listdir(prefix), reverse=True):
                if op.exists(op.join(prefix, name, license)):
                    return matlab_release(name)
        return guess_release("latest", arch)

    elif version.lower() == "latest":

        year = str(datetime.now().year)
        for letter in ("b", "a"):
            maybe_version = "R" + year + letter
            try:
                guess_installer(maybe_version)
                version = maybe_version
                break
            except VersionNotFoundError:
                continue
        if version == "latest":
            # No version found for current year, use latest known version
            version = next(iter(
                sorted(INSTALLERS[arch].keys(), reverse=True)
            ))

    return matlab_release(version)


def guess_installer(version, arch=None):
    """Find installer URL from version or release, for an arch."""
    arch = arch or guess_arch()
    error = VersionNotFoundError(f"No {version} installer found for Win{arch}")
    version = matlab_release(version)
    if version in INSTALLERS[arch]:
        return INSTALLERS[arch][version]
    else:
        A, R, E = arch, version, "zip"
        fmt = dict(release=R, arch=A, ext=E)
        for U in reversed(range(11)):
            maybe_installer = TEMPLATE2.format(update=U, **fmt)
            if url_exists(maybe_installer):
                INSTALLERS[A][R] = maybe_installer
                return maybe_installer
        raise error


# ----------------------------------------------------------------------
#   KNOWN VERSIONS AND OTHER INFO
# ----------------------------------------------------------------------


VERSION_TO_RELEASE = {
    # Starting with R2023b, the version scheme matches the release scheme,
    # i.e., 23.2 === R2023b.
    # This dictionary contains a "version to release" map for
    # releases prior to R2023b.
    "9.14": "R2023a",
    "9.13": "R2022b",
    "9.12": "R2022a",
    "9.11": "R2021b",
    "9.10": "R2021a",
    "9.9": "R2020b",
    "9.8": "R2020a",
    "9.7": "R2019b",
    "9.6": "R2019a",
    "9.5": "R2018b",
    "9.4": "R2018a",
    "9.3": "R2017b",
    "9.2": "R2017a",
    "9.1": "R2016b",
    "9.0.1": "R2016a",
    "9.0": "R2015b",
    "8.5.1": "R2015aSP1",
    "8.5": "R2015a",
    "8.4": "R2014b",
    "8.3": "R2014a",
    "8.2": "R2013b",
    "8.1": "R2013a",
    "8.0": "R2012b",
    "7.17": "R2012a",
}

RELEASE_TO_UPDATE = {
    "R2024b": "5",
    "R2024a": "7",
    "R2023b": "10",
    "R2023a": "7",
    "R2022b": "10",
    "R2022a": "8",
    "R2021b": "7",
    "R2021a": "8",
    "R2020b": "8",
    "R2020a": "8",
    "R2019b": "9",
    "R2019a": "9",
}

SUPPORTED_PYTHON_VERSIONS = {
    "R2024b": ("3.9", "3.10", "3.11", "3.12"),
    "R2024a": ("3.9", "3.10", "3.11"),
    "R2023b": ("3.9", "3.10", "3.11"),
    "R2023a": ("3.8", "3.9", "3.10"),
    "R2022b": ("2.7", "3.8", "3.9", "3.10"),
    "R2022a": ("2.7", "3.8", "3.9"),
    "R2021b": ("2.7", "3.7", "3.8", "3.9"),
    "R2021a": ("2.7", "3.7", "3.8"),
    "R2020b": ("2.7", "3.6", "3.7", "3.8"),
    "R2020a": ("2.7", "3.6", "3.7"),
    "R2019b": ("2.7", "3.6", "3.7"),
    "R2019a": ("2.7", "3.5", "3.6", "3.7"),
    "R2018b": ("2.7", "3.5", "3.6"),
    "R2018a": ("2.7", "3.5", "3.6"),
    "R2017b": ("2.7", "3.4", "3.5", "3.6"),
    "R2017a": ("2.7", "3.4", "3.5"),
    "R2016b": ("2.7", "3.3", "3.4", "3.5"),
    "R2016a": ("2.7", "3.3", "3.4"),
    "R2015b": (),
    "R2015a": ("2.7", "3.3", "3.4"),
}

INSTALLERS = {
    "win64": {},        # Windows 64 bits
    "win32": {},        # Windows 32 bits
    "glnxa64": {},      # Linux 64 bits
    "glnx86": {},       # Linux 32 bits
    "maci64": {},       # Mac Intel 64 bits
    "maca64": {},       # Mac ARM 64  bits
}

# Links @ https://uk.mathworks.com/products/compiler/matlab-runtime.html

# Links for releases >= R2019a
TEMPLATE2 = (
    "https://ssd.mathworks.com/supportfiles/downloads/{release}"
    "/Release/{update}/deployment_files/installer/complete/{arch}"
    "/MATLAB_Runtime_{release}_Update_{update}_{arch}.{ext}"
)
# Links for releases < R2019a
TEMPLATE1 = (
    "https://ssd.mathworks.com/supportfiles/downloads/{release}"
    "/deployment_files/{release}/installers/{arch}"
    "/MCR_{release}_{arch}_installer.{ext}"
)

# NOTE:
#   The (recent) MacOS link point to .dmg files, or to zip files that
#   only contain a dmg. However, replacing .dmg (or .dmg.zip) with .zip
#   allows an archive that contain a binary installer to be obtained.
#   We need this installer to be able to pass command line arguments.

# ----------------------------------------------------------------------
#   WINDOWS INSTALLERS
# ----------------------------------------------------------------------


A = "win64"
E = "zip"
for R, U in RELEASE_TO_UPDATE.items():
    INSTALLERS[A][R] = TEMPLATE2.format(release=R, update=U, arch=A, ext=E)

E = "exe"
for Y in range(12, 19):
    for R in ("a", "b"):
        R = f"R20{Y}{R}"
        INSTALLERS[A][R] = TEMPLATE1.format(release=R, arch=A, ext=E)

A = "win32"
E = "exe"
for Y in range(12, 16):
    for R in ("a", "b"):
        R = f"R20{Y}{R}"
        INSTALLERS[A][R] = TEMPLATE1.format(release=R, arch=A, ext=E)


# ----------------------------------------------------------------------
#   LINUX INSTALLERS
# ----------------------------------------------------------------------


A = "glnxa64"
E = "zip"
for R, U in RELEASE_TO_UPDATE.items():
    INSTALLERS[A][R] = TEMPLATE2.format(release=R, update=U, arch=A, ext=E)

for Y in range(12, 19):
    for R in ("a", "b"):
        R = f"R20{Y}{R}"
        INSTALLERS[A][R] = TEMPLATE1.format(release=R, arch=A, ext=E)

A = "glnx86"
E = "zip"
INSTALLERS[A]["R2012a"] = TEMPLATE1.format(release=R, arch=A, ext=E)


# ----------------------------------------------------------------------
#   MACOS INSTALLERS
# ----------------------------------------------------------------------


A = "maci64"
E = "zip"
for R, U in RELEASE_TO_UPDATE.items():
    INSTALLERS[A][R] = TEMPLATE2.format(release=R, update=U, arch=A, ext=E)

for Y in range(12, 19):
    for R in ("a", "b"):
        R = f"R20{Y}{R}"
        INSTALLERS[A][R] = TEMPLATE1.format(release=R, arch=A, ext=E)

A = "maca64"
E = "zip"
for R in ("R2023b", "R2024a", "R2024b"):
    U = RELEASE_TO_UPDATE[R]
    INSTALLERS[A][R] = TEMPLATE2.format(release=R, update=U, arch=A, ext=E)
