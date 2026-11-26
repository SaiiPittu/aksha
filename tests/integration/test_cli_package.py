"""What an installed ``aksha`` offers before any agent runs: version, doctor, demo."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import aksha
from aksha.interfaces.cli.main import main
from aksha.interfaces.demo import __main__ as demo_main


def run(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out, env=env or {})
    return code, out.getvalue()


def test_version_prints_the_installed_distributions_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"aksha {aksha.__version__}"
    assert aksha.__version__ not in ("", "0.0.0", "0+unknown")  # read from the metadata


def test_help_lists_the_demo_and_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    assert "demo" in text and "doctor" in text


def test_doctor_names_the_paths_in_use_and_how_to_add_a_missing_extra(tmp_path: Path) -> None:
    code, out = run("doctor", env={"AKSHA_HOME": str(tmp_path)})
    assert code == 0, out
    assert f"aksha {aksha.__version__}" in out
    assert str(tmp_path / "aksha.sqlite") in out and str(tmp_path / "blobs") in out
    for extra in ("server", "otel", "postgres"):
        assert f"{extra}:" in out
    for line in out.splitlines():
        if "missing" in line:  # every missing extra says how to install it, quoted
            assert 'pip install "aksha-harness[' in line, line
    assert not (tmp_path / "aksha.sqlite").exists()  # read-only: it opens nothing


def test_the_demo_defaults_to_a_folder_under_aksha_home(tmp_path: Path) -> None:
    assert demo_main.default_work({"AKSHA_HOME": str(tmp_path)}) == tmp_path / "demo"


def test_aksha_demo_is_the_demo_and_refuses_a_folder_it_did_not_write(tmp_path: Path) -> None:
    keep = tmp_path / "notes.txt"
    keep.write_text("mine", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "aksha.interfaces.cli", "demo", "--work", str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "AKSHA_HOME": str(tmp_path / "home")},
    )  # fmt: skip
    assert out.returncode == 2 and "not written by the demo" in out.stderr
    assert keep.read_text(encoding="utf-8") == "mine"


def test_aksha_demo_help_names_its_options() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "aksha.interfaces.cli", "demo", "--help"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert out.returncode == 0 and "--work" in out.stdout and "--json" in out.stdout
