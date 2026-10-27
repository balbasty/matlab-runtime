import json
import os.path as op
import zipfile

import pytest

from matlab_runtime import cli
from matlab_runtime.utils import (
    ctf_required_products,
    installer_products,
    make_installer_input,
    normalize_product_name,
    resolve_products,
)

TEST_CTF = op.join(op.dirname(__file__), "_test_runtime", "_test_runtime.ctf")
AVAILABLE = {
    "35010": "MATLAB Runtime - Numerics",
    "35055": "MATLAB Runtime - Python",
}


def _productdata(name, files=("ddux_mapi_win64_1786212496.enc",)):
    return "\n".join([f"#product_name = {name}", *files]) + "\n"


def test_ctf_products_from_test_runtime():
    assert ctf_required_products(TEST_CTF) == ["35010", "35055"]


def test_ctf_products_from_buildresult(tmp_path):
    ctf = tmp_path / "pkg.ctf"
    meta = {"RuntimeDependencies": [
        {"ID": 35000},
        {"ID": "35010", "Optional": "no"},
        {"ID": "35099", "Optional": "yes"},
    ]}
    with zipfile.ZipFile(ctf, "w") as f:
        f.writestr("fsroot/.META/buildresult.json", json.dumps(meta))
        f.writestr("fsroot/.META/requiredMCRProducts.txt", "1\t2\t")
    assert ctf_required_products(str(ctf)) == ["35000", "35010"]


def test_ctf_products_missing(tmp_path):
    ctf = tmp_path / "empty.ctf"
    with zipfile.ZipFile(ctf, "w") as f:
        f.writestr("foo.txt", "")
    with pytest.raises(ValueError):
        ctf_required_products(str(ctf))


def test_productdata(tmp_path):
    assert installer_products(str(tmp_path)) is None
    (tmp_path / "productdata").mkdir()
    (tmp_path / "productdata" / "35010.txt").write_text(
        _productdata("MATLAB Runtime - Numerics", [
            "ddux_mapi_win64_1786212496.enc",
            "bin/win64/libmwfoo.dll",
        ])
    )
    # a product file without any header is skipped
    (tmp_path / "productdata" / "35099.txt").write_text(
        "ddux_mapi_win64_1.enc\nbin/win64/a.dll\n"
    )
    (tmp_path / "productdata" / "readme.md").write_text("ignored")
    assert installer_products(str(tmp_path)) == {
        "35010": "MATLAB Runtime - Numerics"
    }


def test_productdata_plain_name_key(tmp_path):
    (tmp_path / "productdata").mkdir()
    (tmp_path / "productdata" / "35010.txt").write_text(
        "Name = MATLAB Runtime - Numerics\nfoo.enc\n"
    )
    assert installer_products(str(tmp_path)) == {
        "35010": "MATLAB Runtime - Numerics"
    }


def test_installer_products_encodings(tmp_path):
    (tmp_path / "productdata").mkdir()
    (tmp_path / "productdata" / "35010.txt").write_bytes(
        ("\ufeff" + _productdata("MATLAB Runtime - Numerics"))
        .replace("\n", "\r\n").encode("utf-8")
    )
    (tmp_path / "productdata" / "35055.txt").write_bytes(
        _productdata("MATLAB Runtime - Python").encode("utf-16")
    )
    assert installer_products(str(tmp_path)) == AVAILABLE


def test_normalize_product_name():
    assert normalize_product_name(" MATLAB  Runtime - Numerics ") \
        == "MATLAB Runtime - Numerics"
    assert normalize_product_name("product.MATLAB Runtime - Numerics") \
        == "MATLAB Runtime - Numerics"


@pytest.mark.parametrize("query", [
    "MATLAB Runtime - Numerics",
    "matlab runtime - numerics",
    "MATLAB_Runtime_Numerics",
    "MATLAB_Runtime_-_Numerics",
    "product.MATLAB Runtime - Numerics",
    "Numerics",
    "numerics",
    "35010",
])
def test_resolve_products_lenient(query):
    assert resolve_products([query], AVAILABLE) \
        == ["MATLAB Runtime - Numerics"]


def test_resolve_products_known():
    names = resolve_products(
        ["python", "35055", "MATLAB_Runtime_Numerics", "35010"], AVAILABLE
    )
    assert names == ["MATLAB Runtime - Python", "MATLAB Runtime - Numerics"]


def test_resolve_products_unknown():
    with pytest.raises(ValueError, match="Unknown products") as e:
        resolve_products(["Not_A_Product"], AVAILABLE)
    assert "35010: 'MATLAB Runtime - Numerics'" in str(e.value)
    with pytest.raises(ValueError, match="Unknown products"):
        resolve_products(["99999"], AVAILABLE)


def test_resolve_products_ambiguous():
    available = dict(AVAILABLE, **{"35999": "MATLAB_Runtime_Numerics"})
    with pytest.raises(ValueError, match="Ambiguous products"):
        resolve_products(["Numerics"], available)


def test_resolve_products_passthrough():
    assert resolve_products(["product.MATLAB Runtime - Numerics"]) \
        == ["MATLAB Runtime - Numerics"]
    with pytest.raises(ValueError, match="product IDs"):
        resolve_products(["35010"])


def test_make_input_file():
    content = make_installer_input(
        ["MATLAB Runtime - Numerics"], "/opt/mcr", "/tmp/log.txt"
    )
    assert content.splitlines() == [
        "mode=silent",
        "destinationFolder=/opt/mcr",
        "agreeToLicense=yes",
        "outputFile=/tmp/log.txt",
        "product.MATLAB_Runtime___Numerics",
    ]


def test_cli_products(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "install", lambda *a, **k: calls.append((a, k))
    )
    cli.main([
        "-v", "R2024b", "-p", "/opt/mcr", "-y",
        "--products", "Numerics", "MATLAB Runtime - Python", "35055",
        "--ctf", "a.ctf", "b.ctf",
    ])
    (args, kwargs), = calls
    assert args == (["R2024b"], "/opt/mcr", True, False)
    assert kwargs == dict(
        products=["Numerics", "MATLAB Runtime - Python", "35055"],
        ctf=["a.ctf", "b.ctf"],
        default_scope="system",
    )


def test_cli_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "install", lambda *a, **k: calls.append((a, k))
    )
    cli.main([])
    (args, kwargs), = calls
    assert args == (None, None, False, False)
    assert kwargs == dict(products=None, ctf=None, default_scope="system")


def test_cli_rejects_products_on_removal():
    with pytest.raises(SystemExit):
        cli.main(["-u", "--products", "Numerics"])


# --- record of the installed products ---------------------------------


def test_installed_products_record(tmp_path):
    from matlab_runtime.utils import (
        installed_products, missing_products, record_installed_products,
    )
    # No record: all products are assumed to be installed
    assert installed_products(str(tmp_path)) is None
    assert missing_products(["35055"], None) == []
    record_installed_products(str(tmp_path), {"35010": AVAILABLE["35010"]})
    installed = installed_products(str(tmp_path))
    assert installed == {"35010": "MATLAB Runtime - Numerics"}
    # IDs, names and short names are matched
    assert missing_products(
        ["35010", "Numerics", "MATLAB_Runtime_Numerics", "35055", "Python"],
        installed,
    ) == ["35055", "Python"]
    record_installed_products(str(tmp_path))
    assert installed_products(str(tmp_path)) is None
    (tmp_path / "matlab_runtime_products.json").write_text("garbage")
    assert installed_products(str(tmp_path)) is None


@pytest.fixture
def fake_installer(tmp_path, monkeypatch):
    """`install` runs a fake installer that creates a fake runtime."""
    import subprocess
    import types
    from matlab_runtime import impl
    from .test_runtime_discovery import fake_runtime

    monkeypatch.setattr(impl, "guess_arch", lambda: "glnxa64")
    monkeypatch.setattr(impl, "guess_release", lambda version, arch: version)
    monkeypatch.setattr(impl, "guess_installer", lambda *a: "url/i.zip")
    monkeypatch.setattr(impl, "find_runtime", lambda *a, **k: None)

    def url_download(url, folder):
        path = op.join(folder, "i.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("install", "")
            for pid, name in AVAILABLE.items():
                z.writestr(f"productdata/{pid}.txt", _productdata(name))
        return path
    monkeypatch.setattr(impl, "url_download", url_download)

    calls = []

    def run(call, **kwargs):
        if "-inputFile" in call:
            with open(call[call.index("-inputFile") + 1]) as f:
                lines = f.read().splitlines()
            options = dict(x.split("=", 1) for x in lines if "=" in x)
            dest = options["destinationFolder"]
            products = [x[8:] for x in lines if x.startswith("product.")]
            with open(options["outputFile"], "w") as f:
                f.write("End - Successful")
        else:
            dest = call[call.index("-destinationFolder") + 1]
            products = None
        calls.append(products)
        fake_runtime(op.join(dest, "R2024b"), "R2024b")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(impl.subprocess, "run", run)
    assert impl.subprocess is subprocess
    return dict(calls=calls, prefix=str(tmp_path / "prefix"))


def test_install_adds_missing_products(fake_installer):
    from matlab_runtime import impl
    from matlab_runtime.utils import installed_products
    calls, prefix = fake_installer["calls"], fake_installer["prefix"]
    root = op.join(op.realpath(prefix), "R2024b")
    numerics = "MATLAB_Runtime___Numerics"
    python = "MATLAB_Runtime___Python"

    impl.install("R2024b", prefix, True, products=["Numerics"])
    assert calls == [[numerics]]
    assert installed_products(root) == {"35010": AVAILABLE["35010"]}

    # The CTF also needs the Python product: both are (re)installed
    impl.install("R2024b", prefix, True, ctf=[TEST_CTF])
    assert calls[1:] == [[numerics, python]]
    assert installed_products(root) == AVAILABLE

    # Nothing is missing: "Reinstall?" is answered "no"
    impl.install("R2024b", prefix, True, ctf=[TEST_CTF])
    assert len(calls) == 2

    # All products are requested: they are all installed
    impl.install("R2024b", prefix, True)
    assert calls[2:] == [None]
    assert installed_products(root) is None
    impl.install("R2024b", prefix, True)
    assert len(calls) == 3
