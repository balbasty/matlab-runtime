import os.path as op

from matlab_runtime.utils import find_runtime_in_prefix

VERSION_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<MathWorks_version_info>
  <version>{version}</version>
  <release>{release}</release>
  <description/>
  <date>Sep 18 2019</date>
  <checksum>0</checksum>
</MathWorks_version_info>
"""


def _fake_runtime(path, version, release):
    path.mkdir(parents=True)
    (path / "VersionInfo.xml").write_text(
        VERSION_INFO.format(version=version, release=release)
    )


def test_find_runtime_in_prefix_release_folder(tmp_path):
    _fake_runtime(tmp_path / "R2024b", "24.2.0.2712019", "R2024b")
    found = find_runtime_in_prefix("R2024b", str(tmp_path))
    assert found == op.join(str(tmp_path), "R2024b")


def test_find_runtime_in_prefix_legacy_vNN_folder(tmp_path):
    _fake_runtime(tmp_path / "v97", "9.7.0.1190202", "R2019b")
    _fake_runtime(tmp_path / "v98", "9.8.0.1323502", "R2020a")
    found = find_runtime_in_prefix("R2019b", str(tmp_path))
    assert found == op.join(str(tmp_path), "v97")


def test_find_runtime_in_prefix_missing(tmp_path):
    _fake_runtime(tmp_path / "v97", "9.7.0.1190202", "R2019b")
    (tmp_path / "junk").mkdir()
    assert find_runtime_in_prefix("R2020a", str(tmp_path)) is None
    assert find_runtime_in_prefix("R2019b", str(tmp_path / "nope")) is None
