"""Loading of the release registry (info.json)."""
import io
import json
import threading
import time

import pytest

from matlab_runtime import utils


@pytest.fixture
def bundled():
    return utils._get_matlab_info_from_file()


@pytest.fixture
def urlopen(monkeypatch):
    """Fake `urlopen`, configured through the returned dict."""
    state = dict(calls=[], content=None, error=None, block=None)

    def fake_urlopen(url, timeout=None):
        state["calls"].append(dict(url=url, timeout=timeout))
        if state["block"] is not None:
            state["block"].wait(10)
        if state["error"] is not None:
            raise state["error"]
        return io.BytesIO(json.dumps(state["content"]).encode("utf-8"))

    monkeypatch.setattr(utils.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("MATLAB_RUNTIME_OFFLINE", raising=False)
    monkeypatch.delenv("MATLAB_RUNTIME_INFO_TIMEOUT", raising=False)
    yield state
    if state["block"] is not None:
        state["block"].set()


def test_merge_per_key(bundled, urlopen):
    urlopen["content"] = {
        "SUPPORTED_PYTHON_VERSIONS": {
            "R2099a": ["3.99"],
            "R2024b": ["3.12"],
        },
        "RELEASE_TO_UPDATE": {"R2099a": "1"},
        "NEW_TABLE": {"a": "b"},
        "VERSION_TO_RELEASE": "malformed",
    }
    info = utils.load_info()
    assert urlopen["calls"][0]["url"] == utils.INFO_URL
    assert urlopen["calls"][0]["timeout"] == utils.DEFAULT_INFO_TIMEOUT
    pyver = info["SUPPORTED_PYTHON_VERSIONS"]
    assert pyver["R2099a"] == ["3.99"]           # new in remote
    assert pyver["R2024b"] == ["3.12"]           # remote wins
    assert pyver["R2019b"] == bundled["SUPPORTED_PYTHON_VERSIONS"]["R2019b"]
    assert info["RELEASE_TO_UPDATE"]["R2099a"] == "1"
    assert info["RELEASE_TO_UPDATE"]["R2019b"] \
        == bundled["RELEASE_TO_UPDATE"]["R2019b"]
    assert info["VERSION_TO_RELEASE"] == bundled["VERSION_TO_RELEASE"]
    assert info["NEW_TABLE"] == {"a": "b"}


def test_offline(bundled, urlopen, monkeypatch):
    assert utils.load_info(offline=True) == bundled
    monkeypatch.setenv("MATLAB_RUNTIME_OFFLINE", "1")
    assert utils.load_info() == bundled
    monkeypatch.setenv("MATLAB_RUNTIME_INFO_TIMEOUT", "0")
    monkeypatch.setenv("MATLAB_RUNTIME_OFFLINE", "0")
    assert utils.load_info() == bundled
    assert not urlopen["calls"]


def test_failure_falls_back(bundled, urlopen):
    urlopen["error"] = OSError("no network")
    assert utils.load_info() == bundled
    urlopen["error"] = None
    urlopen["content"] = ["not", "a", "dict"]
    assert utils.load_info() == bundled


def test_timeout(bundled, urlopen, monkeypatch):
    # A fetch that hangs (e.g. DNS resolution) is abandoned
    urlopen["block"] = threading.Event()
    monkeypatch.setenv("MATLAB_RUNTIME_INFO_TIMEOUT", "0.2")
    tic = time.monotonic()
    assert utils.load_info() == bundled
    assert time.monotonic() - tic < 5
    assert urlopen["calls"][0]["timeout"] == 0.2


def test_timeout_env(monkeypatch):
    monkeypatch.setenv("MATLAB_RUNTIME_INFO_TIMEOUT", "0.5")
    assert utils._info_timeout() == 0.5
    monkeypatch.setenv("MATLAB_RUNTIME_INFO_TIMEOUT", "junk")
    assert utils._info_timeout() == utils.DEFAULT_INFO_TIMEOUT
    monkeypatch.delenv("MATLAB_RUNTIME_INFO_TIMEOUT")
    assert utils._info_timeout() == utils.DEFAULT_INFO_TIMEOUT
