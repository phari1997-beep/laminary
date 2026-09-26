"""Smoke test so CI exercises install, import, and pytest end to end."""

import sys

import laminary_pipeline


def test_package_imports_with_version() -> None:
    assert laminary_pipeline.__version__


def test_python_version_is_pinned_line() -> None:
    assert sys.version_info[:2] == (3, 12)
