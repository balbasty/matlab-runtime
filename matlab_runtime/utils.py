import json
import math
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


try:
    import tqdm
except ImportError:
    tqdm = None


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
        yesno = input(f"{question} {options} ").strip()
        if not yesno:
            yesno = True if default == "yes" else False
        else:
            yesno = yesno[:1].lower() == "y"
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


if tqdm:
    def _download_hook():
        data = {"bar": None, "nb_bytes": 0}

        def callback(nb_blocks, block_size, file_size):
            if not data["bar"]:
                if file_size < 0:
                    file_size = float('inf')
                data["bar"] = tqdm.tqdm(total=file_size)
            nb_bytes = nb_blocks * block_size
            data["bar"].update(nb_bytes - data["nb_bytes"])
            data["nb_bytes"] = nb_bytes

        return callback, data
else:
    def _download_hook():
        data = {"started": False}

        def callback(nb_blocks, block_size, file_size):
            nb_bytes = nb_blocks * block_size
            if not data["started"]:
                if file_size < 0:
                    print(f"{0:>3d} B ", end="")
                else:
                    print(f"{0:>3d} %", end="")
                data["started"] = True
            if file_size < 0:
                mag = int(math.floor(math.log2(nb_bytes)))
                unit = ["B", "KB", "MB", "GB", "TB", "PB"]
                unit = unit[int(math.floor(mag/3))]
                nb_units = int(math.floor(nb_bytes / (2 ** mag)))
                print("\b" * 6 + f"{nb_units:>3d} {unit:2s}", end="")
            else:
                nb_pct = int(math.floor(100 * nb_bytes / file_size))
                print("\b" * 5 + f"{nb_pct:>3d} %", end="")
            if nb_bytes >= file_size:
                print("")

        return callback, data


def url_exists(url):
    opener = request.build_opener(NoRedirection)
    req = request.Request(url, method="HEAD")
    try:
        with opener.open(req) as res:
            status = res.status
        return status < 400
    except error.HTTPError:
        return False


def url_download(url, out, retry=5, verbose=True):
    if op.isdir(out):
        basename = op.basename(parse.urlparse(url).path)
        out = op.join(out, basename)

    if verbose:
        hook, hookdata = _download_hook()
    else:
        hook, hookdata = None, {}

    res = exc = None
    for _ in range(retry):
        try:
            res = request.urlretrieve(url, out, hook)
            break
        except Exception as e:
            exc = e

    if "bar" in hookdata:
        hookdata["bar"].close()

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
            "C:\\Program Files (x86)\\MATLAB\\MATLAB Runtime\\{release}",
            "C:\\Program Files\\MATLAB\\MATLAB Runtime\\{release}",
            "C:\\Program Files\\MATLAB\\{release}",
            "C:\\Program Files (x86)\\MATLAB\\{release}"
        ]
    elif arch[:3] == "gln":
        bases = [
            "/usr/local/MATLAB/MATLAB_Runtime/{release}",
            "/usr/local/MATLAB/{release}"
        ]
    elif arch[:3] == "mac":
        bases = [
            "/Applications/MATLAB/MATLAB_Runtime/{release}",
            "/Applications/MATLAB_{release}.app",
            "/Applications/MATLAB_{release}",
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

        # Find most recent version
        year = datetime.now().year
        while year >= 2012:
            for letter in ("b", "a"):
                maybe_version = "R" + str(year) + letter
                try:
                    guess_installer(maybe_version)
                    version = maybe_version
                    break
                except VersionNotFoundError:
                    continue
            if version.lower() != "latest":
                break
            year -= 1

        assert version.lower() != "latest", "Could not find any version ???"

    return matlab_release(version)


# ----------------------------------------------------------------------
#   RETRIEVE INSTALLERS
# ----------------------------------------------------------------------


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
TEMPLATE2_UPDATE = (
    "https://ssd.mathworks.com/supportfiles/downloads/{release}"
    "/Release/{update}/deployment_files/installer/complete/{arch}"
    "/MATLAB_Runtime_{release}_Update_{update}_{arch}.{ext}"
)
TEMPLATE2 = (
    "https://ssd.mathworks.com/supportfiles/downloads/{release}"
    "/Release/{update}/deployment_files/installer/complete/{arch}"
    "/MATLAB_Runtime_{release}_{arch}.{ext}"
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


def guess_installer(release, arch=None, max_update=10):
    """Find installer URL from version or release, for an arch."""
    A = arch or guess_arch()
    R = matlab_release(release)
    Y = int(release[3:5])
    E = "zip"

    if R in INSTALLERS[A]:
        return INSTALLERS[A][R]

    U = RELEASE_TO_UPDATE.get(R, "0")

    def not_available():
        raise VersionNotFoundError(
            f"Installer not available for MATLAB {R} on {A}"
        )

    def url1():
        E = "exe" if A in ("win32", "win64") else "zip"
        return TEMPLATE1.format(release=R, arch=A, ext=E)

    def url2():
        # We know that this installer exists
        tpl = TEMPLATE2_UPDATE if U != "0" else TEMPLATE2
        url = tpl.format(release=R, update=U, arch=A, ext=E)
        if not url_exists(url):
            not_available()

        # Try to find a more recent update if possible
        maybe_u = int(U)
        while True:
            maybe_u += 1
            tpl = TEMPLATE2_UPDATE
            maybe_url = tpl.format(release=R, update=maybe_u, arch=A, ext=E)
            if url_exists(maybe_url):
                url = maybe_url
            else:
                break

        return url

    if A == "win64":
        if Y < 12:
            not_available()
        elif Y < 19:
            E = "exe"
            INSTALLERS[A][R] = url1()
        else:
            INSTALLERS[A][R] = url2()

    elif A == "win32":
        if 12 <= Y < 16:
            INSTALLERS[A][R] = url1()
        else:
            not_available()

    elif A == "glnxa64":
        if Y < 12:
            not_available()
        elif Y < 19:
            INSTALLERS[A][R] = url1()
        else:
            INSTALLERS[A][R] = url2()

    elif A == "glnx86":
        if R == "2012a":
            INSTALLERS[A][R] = url1()
        else:
            not_available()

    elif A == "maci64":
        if Y < 12:
            not_available()
        elif Y < 19:
            INSTALLERS[A][R] = url1()
        else:
            INSTALLERS[A][R] = url2()

    elif A == "maca64":
        if R == "2023b" or Y > 23:
            INSTALLERS[A][R] = url2()
        else:
            not_available()

    else:
        raise ValueError(f"Arch not supported: {A}")

    return INSTALLERS[A][R]


def _get_matlab_info_from_web():
    URL = (
        "https://raw.githubusercontent.com/balbasty/matlab-runtime/"
        "refs/heads/main/matlab_runtime/info.json"
    )
    content = None
    with request.urlopen(URL) as response:
        content = response.read().decode("utf-8")
    return json.loads(content)


def _get_matlab_info_from_file():
    PATH = op.join(op.dirname(__file__), "info.json")
    with open(PATH) as content:
        return json.load(content)


try:
    info = _get_matlab_info_from_web()
    VERSION_TO_RELEASE = info["VERSION_TO_RELEASE"]
    SUPPORTED_PYTHON_VERSIONS = info["SUPPORTED_PYTHON_VERSIONS"]
    RELEASE_TO_UPDATE = info["RELEASE_TO_UPDATE"]
except Exception:
    info = _get_matlab_info_from_file()
    VERSION_TO_RELEASE = info["VERSION_TO_RELEASE"]
    SUPPORTED_PYTHON_VERSIONS = info["SUPPORTED_PYTHON_VERSIONS"]
    RELEASE_TO_UPDATE = info["RELEASE_TO_UPDATE"]
