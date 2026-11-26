"""Smoke tests for the package skeleton."""

import importlib

import aksha


def test_package_has_version() -> None:
    assert aksha.__version__ not in ("", "0+unknown")  # from the installed metadata


def test_runtime_package_imports() -> None:
    runtime = importlib.import_module("aksha.runtime")
    assert runtime.__doc__ is not None
