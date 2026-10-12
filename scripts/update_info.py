"""Bump RELEASE_TO_UPDATE in matlab_runtime/info.json to the latest updates.

Probes the glnxa64 MATLAB Runtime installers on ssd.mathworks.com and
records, for each release (plus any release following the newest known
one), the highest update number that exists.

Usage: python scripts/update_info.py [path/to/info.json]
"""
import json
import os.path as op
import re
import sys

ROOT = op.dirname(op.dirname(op.abspath(__file__)))
sys.path.insert(0, ROOT)
from matlab_runtime.utils import (  # noqa: E402
    TEMPLATE2, TEMPLATE2_UPDATE, url_exists
)

INFO = op.join(ROOT, "matlab_runtime", "info.json")
ARCH = "glnxa64"


def installer_exists(release, update):
    tpl = TEMPLATE2_UPDATE if update else TEMPLATE2
    url = tpl.format(release=release, update=update, arch=ARCH, ext="zip")
    return url_exists(url)


def latest_update(release, current=None, exists=installer_exists):
    """Highest available update (>= current), or None if none exists."""
    if current is None:
        # New release: look for the base release or its first update
        best = next((u for u in (0, 1) if exists(release, u)), None)
        if best is None:
            return None
    else:
        best = int(current)
    while exists(release, best + 1):
        best += 1
    return best


def next_release(release):
    """'R2026a' -> 'R2026b', 'R2026b' -> 'R2027a'."""
    year, half = int(release[1:5]), release[5]
    return f"R{year}b" if half == "a" else f"R{year + 1}a"


def rewrite_release_to_update(text, mapping):
    """Replace the RELEASE_TO_UPDATE block of info.json text.

    `mapping` must be ordered newest first. Everything else in the file
    (including its compact 4-space layout) is left untouched.
    """
    body = ",\n".join(f'        "{k}": "{v}"' for k, v in mapping.items())
    block = '"RELEASE_TO_UPDATE": {\n' + body + "\n    }"
    new, n = re.subn(r'"RELEASE_TO_UPDATE":\s*\{[^}]*\}', block, text)
    if n != 1:
        raise ValueError("RELEASE_TO_UPDATE block not found")
    json.loads(new)  # sanity check
    return new


def find_updates(old, exists=installer_exists):
    """Return an updated {release: update} mapping, newest first."""
    new = {}
    candidate = next_release(max(old))
    while True:
        u = latest_update(candidate, exists=exists)
        if u is None:
            break
        new = {candidate: str(u), **new}
        candidate = next_release(candidate)
    for release, current in old.items():
        new[release] = str(latest_update(release, current, exists=exists))
    return new


def main(path=INFO):
    with open(path) as f:
        text = f.read()
    old = json.loads(text)["RELEASE_TO_UPDATE"]
    new = find_updates(old)
    for release, update in new.items():
        if old.get(release) != update:
            print(f"{release}: {old.get(release, 'new')} -> {update}")
    with open(path, "w") as f:
        f.write(rewrite_release_to_update(text, new))


if __name__ == "__main__":
    main(*sys.argv[1:])
