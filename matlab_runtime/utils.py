import glob
import importlib.util
import json
import math
import os
import os.path as op
import platform
import re
import shutil
import stat
import sys
import tempfile
import zipfile
import tarfile
import threading
from collections import namedtuple
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


class RuntimeNotFoundError(VersionNotFoundError, FileNotFoundError):
    """No installed MATLAB Runtime matches the request."""
    ...


class UnsupportedPythonError(VersionNotFoundError):
    """The requested MATLAB release does not support this Python version."""
    ...


# ----------------------------------------------------------------------
#   PYTHON INTERPRETER
# ----------------------------------------------------------------------


def mwpython_version_args(version=None):
    """
    Trailing arguments required by MathWorks' `mwpython` binary.

    Parameters
    ----------
    version : str, optional
        Python version "X.Y". Default: the current interpreter's.

    Returns
    -------
    args : list[str]
        `["-mwpythonver", "X.Y"]`
    """
    if version is None:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return ["-mwpythonver", version]


def running_under_mwpython(executable=None):
    """
    Whether `executable` (default: `sys.executable`) is MathWorks'
    `mwpython` binary (e.g., when running under `mpython`/`mwpython`).
    """
    executable = sys.executable if executable is None else executable
    if not executable:
        return False
    path = executable.replace("\\", "/")
    return (
        op.basename(path).lower().startswith("mwpython")
        or ".app/Contents/MacOS/mwpython" in path
    )


def python_argv(*args):
    """
    Command that runs the current interpreter with arguments `args`.

    MathWorks' `mwpython` binary refuses to start without a trailing
    `-mwpythonver X.Y` argument, which is appended when the current
    interpreter is `mwpython`.

    Returns
    -------
    argv : list[str]
        `[sys.executable, *args]` (+ `["-mwpythonver", "X.Y"]`)
    """
    argv = [sys.executable, *args]
    if running_under_mwpython():
        argv += mwpython_version_args()
    return argv


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
#   PRODUCTS
# ----------------------------------------------------------------------


def ctf_required_products(ctf):
    """
    List the MATLAB Runtime products required by a compiled CTF archive.

    Parameters
    ----------
    ctf : str
        Path to a CTF file (a zip archive produced by the MATLAB Compiler).

    Returns
    -------
    ids : list[str]
        Product IDs (e.g., '35010'), in the order they are listed.
        Optional dependencies are not included.
    """
    with zipfile.ZipFile(ctf) as f:
        names = f.namelist()

        # Recent releases: JSON build summary with (optional) dependencies
        for name in names:
            if name.endswith(".META/buildresult.json"):
                meta = json.loads(f.read(name).decode("utf-8"))
                deps = meta.get("RuntimeDependencies", [])
                return [
                    str(dep["ID"]) for dep in deps
                    if str(dep.get("Optional", "no")).lower() == "no"
                ]

        # All releases: whitespace-separated list of product IDs
        for name in names:
            if name.endswith(".META/requiredMCRProducts.txt"):
                return f.read(name).decode("utf-8").split()

    raise ValueError(f"No list of required products found in {ctf}")


def ctf_matlab_release(ctf):
    """
    MATLAB release that a compiled CTF archive targets.

    A CTF archive can only be run by the MATLAB Runtime of the release
    that compiled it.

    Parameters
    ----------
    ctf : str
        Path to a CTF file (a zip archive produced by the MATLAB Compiler).

    Returns
    -------
    release : str
        MATLAB release (e.g. 'R2024b').

    Raises
    ------
    ValueError
        If the archive does not contain release information.
    """
    with zipfile.ZipFile(ctf) as f:
        names = f.namelist()

        # Runtime version in the manifest, e.g.
        # <manifest mcr-major-version="24" mcr-minor-version="2" ...>
        for name in names:
            if name.endswith(".META/manifest.xml"):
                try:
                    root = ElementTree.fromstring(f.read(name))
                except ElementTree.ParseError:
                    continue
                major = root.get("mcr-major-version")
                minor = root.get("mcr-minor-version")
                if major and minor:
                    return matlab_release(f"{major}.{minor}")

        # Fallback: MathWorks version info of the compiler
        for name in names:
            if name.endswith("mwcorePropertiesReleaseInfo.xml"):
                try:
                    root = ElementTree.fromstring(f.read(name))
                except ElementTree.ParseError:
                    continue
                release = (root.findtext("release") or "").strip()
                if release:
                    return release

    raise ValueError(f"No MATLAB release information found in {ctf}")


def deployed_ctf(package):
    """
    Find the CTF archive of a compiled (deployed) Python package.

    Like the MATLAB SDK, look for `<package_dir>/<name>.ctf`, where
    `name` is the last component of the package name. If there is no
    such file but the package folder contains a single CTF archive,
    return it.

    Parameters
    ----------
    package : module | str
        Package (or its importable name). A name is located without
        importing the package itself (only its parents).

    Returns
    -------
    ctf : str | None
        Path to the CTF archive, or None if not found.
    """
    if isinstance(package, str):
        name = package
        module = sys.modules.get(name)
        if module is not None:
            file = getattr(module, "__file__", None)
        else:
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            file = getattr(spec, "origin", None)
    else:
        name = package.__name__
        file = getattr(package, "__file__", None)
    if not file:
        return None
    pkgdir = op.dirname(op.abspath(file))
    for basename in (name.rpartition(".")[2], name):
        ctf = op.join(pkgdir, basename + ".ctf")
        if op.isfile(ctf):
            return ctf
    try:
        ctfs = [x for x in os.listdir(pkgdir) if x.endswith(".ctf")]
    except OSError:
        return None
    if len(ctfs) == 1:
        return op.join(pkgdir, ctfs[0])
    return None


def installer_products(installer_dir):
    """
    List the products available in an (unpacked) MATLAB Runtime installer.

    Each `productdata/<ID>.txt` file starts with a header line such as
    `#product_name = MATLAB Runtime - Numerics`, followed by the list of
    files that the product installs.

    Parameters
    ----------
    installer_dir : str
        Folder that contains the unpacked installer.

    Returns
    -------
    products : dict[str, str] | None
        Mapping from product ID to installer product name
        (e.g., `{'35010': 'MATLAB Runtime - Numerics'}`).
        None if the installer does not ship product metadata.
    """
    productdata = op.join(installer_dir, "productdata")
    if not op.isdir(productdata):
        return None
    products = {}
    for fname in sorted(os.listdir(productdata)):
        if not fname.endswith(".txt"):
            continue
        with open(op.join(productdata, fname), "rb") as f:
            raw = f.read()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        else:
            text = raw.decode("utf-8-sig", errors="replace")
        for line in text.splitlines():
            if "=" not in line:
                # file list entries -- never product names
                continue
            key, _, value = line.partition("=")
            key = key.strip().lstrip("#").strip().lower()
            value = value.strip()
            if key in ("name", "product_name") and value:
                products[fname[:-4]] = value
                break
    return products


_PRODUCT_PREFIX_KEY = "matlab_runtime_"


def normalize_product_name(name):
    """
    Clean up a user-provided product name: strip whitespace and any
    leading `product.`, and collapse runs of whitespace.
    """
    name = str(name).strip()
    if name.lower().startswith("product."):
        name = name[8:]
    return " ".join(name.split())


def _product_key(name):
    """Canonical matching key: lowercase, spaces/_/- equivalent."""
    name = normalize_product_name(name).lower()
    return "_".join(part for part in re.split(r"[\s_-]+", name) if part)


def resolve_products(products, available=None):
    """
    Convert product names or IDs to installer product names.

    Matching is lenient: case, a leading `product.`, and the difference
    between spaces, underscores and hyphens are ignored. The short name
    that follows "MATLAB Runtime - " (e.g., 'Numerics') is also accepted
    when unambiguous.

    Parameters
    ----------
    products : list[str]
        Product names (e.g., 'MATLAB Runtime - Numerics',
        'MATLAB_Runtime_Numerics' or 'Numerics') or product IDs
        (e.g., '35010').
    available : dict[str, str], optional
        Mapping from product ID to installer product name, as returned
        by `installer_products`. If None, names are passed through and
        IDs cannot be resolved.

    Returns
    -------
    names : list[str]
        Unique installer product names (exactly as listed by the
        installer), in input order.

    Raises
    ------
    ValueError
        If a product is unknown or ambiguous, or an ID cannot be resolved.
    """
    by_key, by_short = {}, {}
    for name in (available or {}).values():
        key = _product_key(name)
        by_key[key] = name
        if key.startswith(_PRODUCT_PREFIX_KEY):
            short = key[len(_PRODUCT_PREFIX_KEY):]
            by_short.setdefault(short, set()).add(name)
    names, unknown, ambiguous = [], [], []
    for product in products:
        product = normalize_product_name(product)
        if not product:
            continue
        key = _product_key(product)
        if product.isdigit():
            if available is None or product not in available:
                unknown.append(product)
                continue
            name = available[product]
        elif available is None:
            name = product
        elif key in by_key:
            name = by_key[key]
        elif len(by_short.get(key, ())) == 1:
            name, = by_short[key]
        elif key in by_short:
            ambiguous.append(product)
            continue
        else:
            unknown.append(product)
            continue
        if name not in names:
            names.append(name)
    if unknown or ambiguous:
        if available is None:
            raise ValueError(
                f"Cannot resolve product IDs {unknown}: the installer "
                f"does not provide product metadata. Use product names."
            )
        listing = ", ".join(
            f"{pid}: {name!r}" for pid, name in sorted(available.items())
        )
        msg = []
        if unknown:
            msg.append(f"Unknown products: {unknown}.")
        if ambiguous:
            msg.append(f"Ambiguous products: {ambiguous}.")
        raise ValueError(" ".join(msg) + f" Available products: {listing}")
    return names


def make_installer_input(products, prefix, logfile=None, agree="yes"):
    """
    Build the content of an installer input file (`-inputFile`) that
    installs a subset of products in silent mode.

    Parameters
    ----------
    products : list[str]
        Installer product names (e.g., 'MATLAB Runtime - Numerics').
    prefix : str
        Destination folder.
    logfile : str, optional
        Installer log file.
    agree : {"yes", "no"}
        Agree to the license.

    Returns
    -------
    content : str
    """
    lines = [
        "mode=silent",
        f"destinationFolder={prefix}",
        f"agreeToLicense={agree}",
    ]
    if logfile:
        lines += [f"outputFile={logfile}"]
    # The installer identifies products by their name with every
    # non-alphanumeric character replaced by "_" (e.g. "MATLAB Runtime -
    # Numerics" -> "MATLAB_Runtime___Numerics"), as in the
    # `archives/productdata_<key><ver>_<arch>.xml` files it ships.
    # Older installers (<= R2023b) ignore any other spelling.
    lines += ["product." + re.sub(r"\W", "_", name) for name in products]
    return "\n".join(lines) + "\n"


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


# Standard installation locations. `{release}` is a glob placeholder: it
# matches release folders (`R2024b`), legacy runtime folders (`v97`) and
# full MATLAB installations (`MATLAB_R2024b.app`). The actual release is
# always read from the installation's `VersionInfo.xml`.
CANDIDATE_LOCATIONS_BY_OS = {
    "win": [
        "C:\\Program Files\\MATLAB\\MATLAB Runtime\\{release}",
        "C:\\Program Files (x86)\\MATLAB\\MATLAB Runtime\\{release}",
        "C:\\Program Files\\MATLAB\\{release}",
        "C:\\Program Files (x86)\\MATLAB\\{release}",
    ],
    "gln": [
        "/usr/local/MATLAB/MATLAB_Runtime/{release}",
        "/usr/local/MATLAB/{release}",
    ],
    "mac": [
        "/Applications/MATLAB/MATLAB_Runtime/{release}",
        "/Applications/MATLAB_{release}.app",
        "/Applications/MATLAB_{release}",
        "/Applications/MATLAB/{release}",
    ],
}

_VERSION_INFO = "VersionInfo.xml"

Installation = namedtuple("Installation", ["release", "path", "source"])
Installation.__doc__ = """
An installed MATLAB Runtime (or full MATLAB).

Attributes
----------
release : str
    Release read from the installation's `VersionInfo.xml` (e.g. 'R2024b').
path : str
    Root of the installation (the folder that contains `VersionInfo.xml`).
source : {'prefix', 'MATLAB_RUNTIME_PATH', 'MATLAB_PATH', 'standard', 'which'}
    Where the installation was found.
"""


def release_key(release):
    """
    Sort key for MATLAB releases (newest compares greatest).

    Parameters
    ----------
    release : str
        MATLAB release (e.g. 'R2024b', 'R2015aSP1') or version
        (e.g. '24.2', '9.7').

    Returns
    -------
    key : tuple[int, int, str]
        `(year, half, suffix)`, e.g. `(2024, 2, '')` for 'R2024b'.
        `(0, 0, '')` if `release` cannot be parsed.
    """
    release = str(release)
    match = re.match(r"R(\d{4})([a-z])(.*)$", release)
    if not match:
        try:
            match = re.match(r"R(\d{4})([a-z])(.*)$", matlab_release(release))
        except Exception:
            match = None
    if not match:
        return (0, 0, "")
    year, letter, suffix = match.groups()
    return (int(year), ord(letter) - ord("a") + 1, suffix)


def installation_release(path):
    """
    Release of the MATLAB [Runtime] installation rooted at `path`.

    Unlike `guess_matlab_release`, parent folders are not searched:
    `path` must directly contain `VersionInfo.xml`.

    Parameters
    ----------
    path : str
        Candidate installation folder (e.g. '.../MATLAB_Runtime/R2024b',
        '.../MATLAB_Runtime/v97' or '/Applications/MATLAB_R2024b.app').

    Returns
    -------
    release : str | None
        Release (e.g. 'R2024b'), or None if `path` is not an installation.
    """
    info = op.join(path, _VERSION_INFO)
    if not op.isfile(info):
        return None
    try:
        tree = ElementTree.parse(info)
    except (ElementTree.ParseError, OSError):
        return None
    release = (tree.findtext("release") or "").strip()
    if release:
        return release
    version = (tree.findtext("version") or "").strip()
    try:
        return matlab_release(".".join(version.split(".")[:2]))
    except Exception:
        return None


def _scan_root(root, source):
    # A root is either an installation (release folder) or a prefix that
    # contains installations.
    release = installation_release(root)
    if release:
        yield Installation(release, root, source)
        return
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        path = op.join(root, name)
        release = installation_release(path)
        if release:
            yield Installation(release, path, source)


def _glob_location(template):
    head, sep, tail = template.partition("{release}")
    if not sep:
        return [template]
    pattern = glob.escape(head) + "*" + glob.escape(tail)
    return sorted(glob.glob(pattern))


def _python_version(python_version):
    # Normalize a python version (str, tuple, sys.version_info) to "X.Y"
    if python_version is None:
        return None
    if isinstance(python_version, str):
        return ".".join(python_version.strip().split(".")[:2])
    return ".".join(map(str, tuple(python_version)[:2]))


def iter_installations(prefix=None):
    """
    Iterate over installed MATLAB Runtimes and full MATLAB installations.

    Every location below may be a release folder (that contains
    `VersionInfo.xml`) or a prefix (whose subfolders are release
    folders). Locations are searched in this order:

    1. `prefix` if provided; otherwise `$MATLAB_RUNTIME_PATH` if set;
       otherwise the default prefix (`guess_prefix()`).
    2. `$MATLAB_PATH`, if set.
    3. Standard locations (`CANDIDATE_LOCATIONS_BY_OS`).
    4. The installation that contains the `matlab` executable on the PATH.

    Installations found in several locations are only yielded once,
    in the first (highest priority) location.

    Parameters
    ----------
    prefix : str, optional
        Installation prefix, or a release folder.

    Yields
    ------
    installation : Installation
        Named tuple `(release, path, source)`. The release is read from
        the installation's `VersionInfo.xml`.
    """
    seen = set()

    def unseen(installations):
        for installation in installations:
            key = op.normcase(op.realpath(installation.path))
            if key not in seen:
                seen.add(key)
                yield installation

    # 1. prefix / MATLAB_RUNTIME_PATH / default prefix
    if prefix is not None:
        root, source = prefix, "prefix"
    elif os.environ.get("MATLAB_RUNTIME_PATH", ""):
        root, source = os.environ["MATLAB_RUNTIME_PATH"], "MATLAB_RUNTIME_PATH"
    else:
        root, source = guess_prefix(), "prefix"
    yield from unseen(_scan_root(op.normpath(root), source))

    # 2. MATLAB_PATH
    if os.environ.get("MATLAB_PATH", ""):
        root = op.normpath(os.environ["MATLAB_PATH"])
        yield from unseen(_scan_root(root, "MATLAB_PATH"))

    # 3. Standard locations
    for template in CANDIDATE_LOCATIONS_BY_OS.get(guess_arch()[:3], []):
        for path in _glob_location(template):
            release = installation_release(path)
            if release:
                yield from unseen([Installation(release, path, "standard")])

    # 4. `matlab` executable on the PATH (<root>/bin/matlab)
    path = shutil.which("matlab")
    if path:
        path = op.dirname(op.dirname(op.realpath(path)))
        release = installation_release(path)
        if release:
            yield from unseen([Installation(release, path, "which")])


def supported_python_versions(release):
    """
    Python versions supported by a MATLAB release.

    Parameters
    ----------
    release : str
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2').

    Returns
    -------
    versions : list[str] | None
        Supported versions (e.g. `['3.9', '3.10']`), or None if the
        release is not listed in `SUPPORTED_PYTHON_VERSIONS`.
    """
    try:
        release = matlab_release(release)
    except Exception:
        return None
    versions = SUPPORTED_PYTHON_VERSIONS.get(release, None)
    return None if versions is None else list(versions)


def python_supported(release, python_version=None):
    """
    Whether a MATLAB release supports a Python version.

    Parameters
    ----------
    release : str
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2').
    python_version : str | tuple, optional
        Python version (e.g. '3.12' or `sys.version_info`).
        Default: the running Python.

    Returns
    -------
    supported : bool | None
        None if the release is not listed in `SUPPORTED_PYTHON_VERSIONS`.
    """
    if python_version is None:
        python_version = sys.version_info
    versions = supported_python_versions(release)
    if versions is None:
        return None
    return _python_version(python_version) in versions


def _describe_installations(installations):
    lines = []
    for inst in installations:
        versions = supported_python_versions(inst.release)
        if versions is None:
            versions = "supported Python versions unknown"
        elif not versions:
            versions = "no supported Python version"
        else:
            versions = "Python " + ", ".join(versions)
        lines.append(f"  - {inst.release} at {inst.path} ({versions})")
    return "\n".join(lines)


_NOT_FOUND_HINT = (
    "Install a runtime with `install_matlab_runtime` (or `install()`), "
    "or, if it is installed in an unusual location, set the "
    "MATLAB_RUNTIME_PATH environment variable to its prefix or to the "
    "release folder itself."
)


def latest_release(python_version=None, arch=None):
    """
    Most recent release with an installer available online.

    !!! note
        This function queries the MathWorks servers.

    Parameters
    ----------
    python_version : str | tuple, optional
        Only consider releases that support this Python version.
    arch : str, optional
        Target architecture. Default: `guess_arch()`.

    Returns
    -------
    release : str

    Raises
    ------
    UnsupportedPythonError
        If no known release supports `python_version`.
    """
    python_version = _python_version(python_version)
    if python_version is None:
        return guess_release("latest", arch)
    releases = sorted(
        (release for release, versions in SUPPORTED_PYTHON_VERSIONS.items()
         if python_version in versions),
        key=release_key, reverse=True,
    )
    for release in releases:
        try:
            guess_installer(release, arch)
            return release
        except VersionNotFoundError:
            continue
    raise UnsupportedPythonError(
        f"No MATLAB Runtime installer supports Python {python_version}."
    )


def resolve_runtime(
    version="latest_installed",
    prefix=None,
    python_version=None,
    accept=None,
    missing_ok=False,
):
    """
    Choose an installed MATLAB Runtime (or MATLAB).

    Candidates are listed by `iter_installations(prefix)`. When several
    candidates match, the most recent release wins; on ties, the
    candidate found first (i.e., in `prefix` / `$MATLAB_RUNTIME_PATH`)
    wins. If `prefix` (or `$MATLAB_RUNTIME_PATH`) is itself a release
    folder, that installation is used with 'latest_installed'.

    Parameters
    ----------
    version : str, default="latest_installed"
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2', '9.13').
        If 'latest_installed', use the most recent installed release
        (that supports `python_version`, if provided).
        If 'latest', use the most recent release available online
        (queries the MathWorks servers).
    prefix : str, optional
        Installation prefix, or release folder, searched first.
        Default: `$MATLAB_RUNTIME_PATH` if set, else `guess_prefix()`.
    python_version : str | tuple, optional
        Only accept releases that support this Python version
        (e.g. '3.12' or `sys.version_info`), according to
        `SUPPORTED_PYTHON_VERSIONS`. Releases missing from that table
        are accepted.
    accept : callable(str) -> bool, optional
        Only accept installations whose path satisfies this predicate
        (e.g., `lambda path: op.exists(op.join(path, "bin", "mwpython"))`).
    missing_ok : bool, default=False
        If no installation matches, return `(release, None)` instead of
        raising, where `release` is the release to install. With
        'latest_installed', this queries the MathWorks servers for the
        most recent release (that supports `python_version`).

    Returns
    -------
    release : str
        MATLAB release (e.g. 'R2024b').
    path : str | None
        Root of the installation. None only if `missing_ok`.

    Raises
    ------
    RuntimeNotFoundError
        If no installation matches (unless `missing_ok`).
    UnsupportedPythonError
        If the requested release does not support `python_version`, or
        if no installed release does (unless `missing_ok`).
    """
    version = str(version or "latest_installed")
    python_version = _python_version(python_version)

    if version.lower() == "latest_installed":
        target = None
    elif version.lower() == "latest":
        target = guess_release("latest")
    else:
        target = matlab_release(version)

    if target and python_version:
        if python_supported(target, python_version) is False:
            versions = ", ".join(supported_python_versions(target)) or "none"
            raise UnsupportedPythonError(
                f"MATLAB {target} does not support Python {python_version} "
                f"(supported versions: {versions})."
            )

    found = list(iter_installations(prefix))
    candidates = [x for x in found if target in (None, x.release)]
    nb_matching = len(candidates)
    if accept is not None:
        candidates = [x for x in candidates if accept(x.path)]
    nb_rejected = nb_matching - len(candidates)
    if python_version:
        compatible = [
            x for x in candidates
            if python_supported(x.release, python_version) is not False
        ]
    else:
        compatible = candidates

    if compatible and target is None:
        # An explicit release folder (`prefix` or `$MATLAB_RUNTIME_PATH`
        # pointing at a release, not at a prefix) pins the installation.
        root = prefix
        if root is None:
            root = os.environ.get("MATLAB_RUNTIME_PATH", "") or None
        if root and op.exists(op.join(root, "VersionInfo.xml")):
            root = op.realpath(root)
            for x in compatible:
                if op.realpath(x.path) == root:
                    return x.release, x.path

    if compatible:
        # sorted() is stable, also with reverse=True: ties keep their
        # search order, so that `prefix` wins.
        best = sorted(compatible, key=lambda x: release_key(x.release),
                      reverse=True)[0]
        return best.release, best.path

    if missing_ok:
        return target or latest_release(python_version), None

    # --- explain what was found --------------------------------------
    what = target or "MATLAB Runtime"
    if found:
        listing = "Installations found:\n" + _describe_installations(found)
    else:
        listing = "No MATLAB Runtime or MATLAB installation found."
    if nb_rejected:
        listing += (
            f"\n{nb_rejected} matching installation(s) were rejected "
            f"(e.g., missing required files)."
        )
    if candidates and python_version:
        raise UnsupportedPythonError(
            f"No installed {what} supports Python {python_version}.\n"
            f"{listing}"
        )
    raise RuntimeNotFoundError(
        f"No installed {what} found.\n{listing}\n{_NOT_FOUND_HINT}"
    )


def iter_existing_installations(variant='latest_installed'):
    """
    Iterate over MATLAB and MATLAB Runtime installations.

    Thin wrapper around `iter_installations`, kept for backward
    compatibility.

    Parameters
    ----------
    variant : str, default='latest_installed'
        If 'latest_installed', yield all installations. Otherwise, only
        yield installations of this release (e.g. 'R2024b' or '24.2').

    Yields
    ------
    path : str
        Path to the installation, most recent release first.
    release : str
        Release of the installation (read from its `VersionInfo.xml`).
    """
    installations = list(iter_installations())
    if str(variant).lower() != "latest_installed":
        target = matlab_release(variant)
        installations = [x for x in installations if x.release == target]
    installations.sort(key=lambda x: release_key(x.release), reverse=True)
    for installation in installations:
        yield installation.path, installation.release


def guess_prefix():
    """
    Guess the MATLAB Runtime installation prefix.

    If the environment variable `"MATLAB_RUNTIME_PATH"` is set, return it
    (or its parent folder if it points to a release folder, i.e. a folder
    that contains `VersionInfo.xml`).

    Otherwise, the default prefix is platform-specific:

    * Windows:  C:\\Program Files\\MATLAB\\MATLAB Runtime\\
    * Linux:    /usr/local/MATLAB/MATLAB_Runtime
    * MacOS:    /Applications/MATLAB/MATLAB_Runtime

    Returns
    -------
    prefix : str
    """
    if os.environ.get("MATLAB_RUNTIME_PATH", ""):
        path = os.environ["MATLAB_RUNTIME_PATH"]
        if installation_release(path):
            return op.dirname(op.normpath(path))
        return path

    arch = guess_arch()
    if arch[:3] == "win":
        return "C:\\Program Files\\MATLAB\\MATLAB Runtime\\"
    if arch[:3] == "gln":
        return "/usr/local/MATLAB/MATLAB_Runtime"
    if arch[:3] == "mac":
        return "/Applications/MATLAB/MATLAB_Runtime"
    assert False


def find_runtime_in_prefix(version, prefix):
    """
    Find a runtime installed directly under `prefix`.

    Recent releases install into `<prefix>/<release>` (e.g. `R2024b`),
    older ones into `<prefix>/v<NN>` (e.g. `v97` for R2019b).
    `prefix` may also be the release folder itself.
    Returns the path, or None if not found.
    """
    version = matlab_release(version)
    if installation_release(prefix) == version:
        return prefix
    path = op.join(prefix, version)
    if installation_release(path) == version:
        return path
    for installation in _scan_root(prefix, "prefix"):
        if installation.release == version:
            return installation.path
    return None


def find_runtime(version, prefix=None):
    """
    Find an installed MATLAB runtime with a specific version.

    Thin wrapper around `resolve_runtime`, kept for backward compatibility.

    Parameters
    ----------
    version : str
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2'),
        or 'latest_installed'.
    prefix : str, optional
        Installation prefix (or release folder) searched first.

    Returns
    -------
    path : str | None
        Root of the installation, or None if not found.
    """
    try:
        return resolve_runtime(version, prefix)[1]
    except VersionNotFoundError:
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
    path0, path = path, op.abspath(op.realpath(path))
    trial = 0
    while path:
        if trial > 100:
            break
        trial += 1
        if op.exists(op.join(path, 'VersionInfo.xml')):
            path = op.join(path, 'VersionInfo.xml')
            tree = ElementTree.parse(path)
            version = tree.find(key).text
            return version
        else:
            path, prev_path = op.dirname(path), path
            if path in (prev_path, ''):
                break
    raise ValueError(
        f"Could not guess matlab {key} from python module. "
        f"Path: {path0}"
    )


# ----------------------------------------------------------------------
#   INSTALLERS
# ----------------------------------------------------------------------


def matlab_release(version):
    """Convert MATLAB version (e.g. 24.2) to release (e.g. R2024b)."""
    if isinstance(version, (list, tuple)):
        version = ".".join(map(str, version[:2]))
    if version[:1] == "R":
        return version
    if version in VERSION_TO_RELEASE:
        return VERSION_TO_RELEASE[version]
    year, release, *_ = version.split(".")
    return "R20" + year + ("abcdefghijklmnopqrstuvwxy"[int(release)-1])


def matlab_version(version):
    """Convert MATLAB release (e.g. R2024b) to version (e.g. 24.2)."""
    # 1. look for version in dict of known versions
    if isinstance(version, (list, tuple)):
        version = ".".join(map(str, version[:2]))
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
    """
    Guess version (if "latest") + convert to MATLAB release (e.g. R2024b).

    Parameters
    ----------
    version : str
        MATLAB release (e.g. 'R2024b') or version (e.g. '24.2').
        If 'latest_installed', the most recent installed release
        (see `resolve_runtime`).
        If 'latest', the most recent release available online
        (queries the MathWorks servers).
    arch : str, optional
        Target architecture (used with 'latest').
    prefix : str, optional
        Installation prefix searched first (used with 'latest_installed').

    Returns
    -------
    release : str

    Raises
    ------
    RuntimeNotFoundError
        If 'latest_installed' and no runtime is installed.
    """
    arch = arch or guess_arch()

    if version.lower() == "latest_installed":
        return resolve_runtime("latest_installed", prefix)[0]

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


def _env_flag(name):
    """True if the environment variable `name` is set to a truthy value."""
    value = os.environ.get(name, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


INFO_URL = (
    "https://raw.githubusercontent.com/balbasty/matlab-runtime/"
    "refs/heads/main/matlab_runtime/info.json"
)
DEFAULT_INFO_TIMEOUT = 3.0


def _info_timeout():
    # Timeout (in seconds) of the remote registry fetch
    value = os.environ.get("MATLAB_RUNTIME_INFO_TIMEOUT", "").strip()
    try:
        return float(value) if value else DEFAULT_INFO_TIMEOUT
    except ValueError:
        return DEFAULT_INFO_TIMEOUT


def _get_matlab_info_from_web(timeout=None):
    if timeout is None:
        timeout = _info_timeout()
    with request.urlopen(INFO_URL, timeout=timeout) as response:
        content = response.read().decode("utf-8")
    return json.loads(content)


def _get_matlab_info_from_file():
    PATH = op.join(op.dirname(__file__), "info.json")
    with open(PATH) as content:
        return json.load(content)


def _call_with_deadline(func, timeout):
    # Run `func` in a daemon thread and give up after `timeout` seconds.
    # (The socket timeout of `urlopen` does not bound DNS resolution.)
    result = {}

    def target():
        try:
            result["value"] = func()
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(
        target=target, name="matlab_runtime.info", daemon=True
    )
    thread.start()
    thread.join(timeout)
    if "value" in result:
        return result["value"]
    if "error" in result:
        raise result["error"]
    raise TimeoutError(f"No answer after {timeout} seconds")


def merge_info(bundled, remote):
    """
    Merge the remote release registry over the bundled one, per key.

    Parameters
    ----------
    bundled, remote : dict[str, dict]
        Registries, such as the content of `info.json`.

    Returns
    -------
    info : dict[str, dict]
        For each table (e.g. 'SUPPORTED_PYTHON_VERSIONS'), entries of
        `remote` override those of `bundled`; entries that only exist
        in `bundled` are kept. Malformed remote tables are ignored.
    """
    info = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in bundled.items()
    }
    if not isinstance(remote, dict):
        return info
    for key, value in remote.items():
        if not isinstance(value, dict):
            continue
        base = info.get(key, {})
        info[key] = {**(base if isinstance(base, dict) else {}), **value}
    return info


def load_info(offline=None, timeout=None):
    """
    Load the registry of MATLAB releases (`info.json`).

    The registry bundled with the package is updated with the most
    recent registry from GitHub, unless offline. Failures (no network,
    timeout, malformed file) silently fall back to the bundled registry.

    Parameters
    ----------
    offline : bool, optional
        Do not fetch the remote registry.
        Default: `$MATLAB_RUNTIME_OFFLINE` (e.g., '1').
    timeout : float, optional
        Maximum time (in seconds) spent fetching the remote registry.
        A non-positive value means offline.
        Default: `$MATLAB_RUNTIME_INFO_TIMEOUT`, or 3 seconds.

    Returns
    -------
    info : dict[str, dict]
        Tables 'VERSION_TO_RELEASE', 'SUPPORTED_PYTHON_VERSIONS' and
        'RELEASE_TO_UPDATE'.
    """
    info = _get_matlab_info_from_file()
    if offline is None:
        offline = _env_flag("MATLAB_RUNTIME_OFFLINE")
    if timeout is None:
        timeout = _info_timeout()
    if offline or timeout <= 0:
        return info
    try:
        remote = _call_with_deadline(
            lambda: _get_matlab_info_from_web(timeout), timeout
        )
    except Exception:
        return info
    return merge_info(info, remote)


info = load_info()
VERSION_TO_RELEASE = info["VERSION_TO_RELEASE"]
SUPPORTED_PYTHON_VERSIONS = info["SUPPORTED_PYTHON_VERSIONS"]
RELEASE_TO_UPDATE = info["RELEASE_TO_UPDATE"]
