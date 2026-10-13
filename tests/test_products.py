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
    )


def test_cli_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "install", lambda *a, **k: calls.append((a, k))
    )
    cli.main([])
    (args, kwargs), = calls
    assert kwargs == dict(products=None, ctf=None)


def test_cli_rejects_products_on_removal():
    with pytest.raises(SystemExit):
        cli.main(["-u", "--products", "Numerics"])
