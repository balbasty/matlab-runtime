"""Removal of macOS' quarantine attribute (with a fake `xattr`)."""
import sys

import pytest

from matlab_runtime import utils
from matlab_runtime.utils import QUARANTINE, unquarantine


class Proc:
    def __init__(self, returncode):
        self.returncode = returncode


@pytest.fixture
def xattr(monkeypatch):
    """Fake `xattr`: `state` says whether the probe is quarantined, and
    `sudo` whether removing the attribute requires sudo."""
    fake = {"state": False, "sudo": False, "calls": []}

    def run(call, capture_output=False):
        fake["calls"].append(call)
        sudo = call[0] == "sudo"
        call = call[1:] if sudo else call
        assert call[0] == "xattr"
        if call[1] == "-p":
            return Proc(0 if fake["state"] else 1)
        assert call[1:4] == ["-r", "-d", QUARANTINE]
        if sudo or not fake["sudo"]:
            fake["state"] = False
            return Proc(0)
        return Proc(1)
    monkeypatch.setattr(utils.subprocess, "run", run)
    return fake


def test_not_quarantined(xattr):
    assert not unquarantine("/tmp/dir", probe="/tmp/dir/install")
    assert xattr["calls"] == [["xattr", "-p", QUARANTINE, "/tmp/dir/install"]]


def test_quarantined_without_sudo(xattr):
    xattr["state"] = True
    assert unquarantine("/tmp/dir", probe="/tmp/dir/install")
    assert not xattr["state"]
    assert not any(call[0] == "sudo" for call in xattr["calls"])


def test_quarantined_with_sudo(xattr):
    xattr["state"] = xattr["sudo"] = True
    assert unquarantine("/tmp/dir", probe="/tmp/dir/install")
    assert not xattr["state"]
    assert xattr["calls"][-1] == [
        "sudo", "xattr", "-r", "-d", QUARANTINE, "/tmp/dir"
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_download_is_not_quarantined(tmp_path):
    # Files downloaded with urllib do not get the quarantine attribute,
    # so that the MATLAB installer does not need to be unquarantined.
    path = utils.url_download(utils.INFO_URL, str(tmp_path), verbose=False)
    assert not utils.is_quarantined(path)
