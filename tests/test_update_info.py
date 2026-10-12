import json
import os.path as op
import sys

sys.path.insert(0, op.join(op.dirname(op.dirname(__file__)), "scripts"))
import update_info  # noqa: E402

INFO = op.join(op.dirname(op.dirname(__file__)), "matlab_runtime", "info.json")


def fake_exists(available):
    return lambda release, update: update in available.get(release, ())


def test_next_release():
    assert update_info.next_release("R2026a") == "R2026b"
    assert update_info.next_release("R2026b") == "R2027a"


def test_find_updates():
    old = {"R2026a": "2", "R2025b": "5"}
    available = {
        "R2026b": {0, 1},
        "R2026a": {0, 1, 2, 3, 4, 5},
        "R2025b": {0, 1, 2, 3, 4, 5},
    }
    new = update_info.find_updates(old, fake_exists(available))
    assert list(new.items()) == [
        ("R2026b", "1"), ("R2026a", "5"), ("R2025b", "5")
    ]


def test_find_updates_new_release_without_update0():
    available = {"R2027a": {1, 2}}
    new = update_info.find_updates({"R2026b": "0"}, fake_exists(available))
    assert list(new.items()) == [("R2027a", "2"), ("R2026b", "0")]


def test_rewrite_preserves_rest_of_file():
    with open(INFO) as f:
        text = f.read()
    old = json.loads(text)["RELEASE_TO_UPDATE"]
    new = {"R2099a": "3", **{k: "42" for k in old}}
    out = update_info.rewrite_release_to_update(text, new)
    data = json.loads(out)
    assert list(data["RELEASE_TO_UPDATE"].items()) == list(new.items())
    # Everything before the block is untouched
    head = text.split('"RELEASE_TO_UPDATE"')[0]
    assert out.startswith(head)
    assert '        "R2099a": "3",\n' in out
    # Round-trip with same mapping is a no-op
    assert update_info.rewrite_release_to_update(text, old) == text
