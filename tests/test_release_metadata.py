"""Lock the runtime `__version__` and the three packaging files in sync.

Drift between `pyproject.toml`, `src/voiceagent/__init__.py`, the local
`PKGBUILD`, the AUR-publish `packaging/PKGBUILD.aur`, and the AUR
`.SRCINFO` ships installs that report wrong versions or rebuild against
stale tags. Failing this test on every commit is cheaper than catching
it after a release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _runtime_version() -> str:
    init_text = _read("src/voiceagent/__init__.py")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    assert match, "src/voiceagent/__init__.py must define __version__"
    return match.group(1)


@pytest.fixture(scope="module")
def runtime_version() -> str:
    return _runtime_version()


def test_pyproject_version_matches_runtime(runtime_version):
    text = _read("pyproject.toml")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml must define a version"
    assert match.group(1) == runtime_version


def test_local_pkgbuild_matches_runtime(runtime_version):
    text = _read("PKGBUILD")
    match = re.search(r"^pkgver=([^\s#]+)", text, re.MULTILINE)
    assert match, "PKGBUILD must define pkgver"
    assert match.group(1) == runtime_version


def test_aur_pkgbuild_matches_runtime(runtime_version):
    text = _read("packaging/PKGBUILD.aur")
    match = re.search(r"^pkgver=([^\s#]+)", text, re.MULTILINE)
    assert match, "packaging/PKGBUILD.aur must define pkgver"
    assert match.group(1) == runtime_version


def test_srcinfo_matches_runtime(runtime_version):
    text = _read(".SRCINFO")
    match = re.search(r"^\s*pkgver\s*=\s*(\S+)", text, re.MULTILINE)
    assert match, ".SRCINFO must define pkgver"
    assert match.group(1) == runtime_version
