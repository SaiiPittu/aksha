"""The built distributions, installed the way a user installs them (``make release-check``).

Skipped unless ``AKSHA_DIST`` names a folder holding one wheel and one sdist (``make dist``
builds them; the release workflow runs this before any upload). The wheel goes into a fresh
virtual environment in a temporary folder, outside the repository, and ``aksha`` runs there
with a throwaway home and every proxy pointed at a closed port, so a command that reached
for the network would fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEAD = "http://127.0.0.1:9"
"""A closed port: any HTTP request through the proxy fails at once."""
PRUNED = (".claude/", ".gemini/", ".github/", "scratch-pad/", "web/", ".coderabbit.yaml",
          "AGENTS.md", "CLAUDE.md", "REVIEWING.md")  # fmt: skip
"""What MANIFEST.in keeps out of the sdist."""


@pytest.fixture(scope="module")
def dist() -> Path:
    raw = os.environ.get("AKSHA_DIST")
    if not raw:
        pytest.skip("set AKSHA_DIST to a folder with the built wheel and sdist (make dist)")
    return Path(raw).resolve()


@pytest.fixture(scope="module")
def wheel(dist: Path) -> Path:
    (found,) = sorted(dist.glob("aksha_harness-*.whl"))
    return found


@pytest.fixture(scope="module")
def sdist(dist: Path) -> Path:
    (found,) = sorted(dist.glob("aksha_harness-*.tar.gz"))
    return found


@pytest.fixture(scope="module")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A fresh virtual environment with only the wheel (and its dependency) installed."""
    base = tmp_path_factory.mktemp("installed")
    venv, cwd, home = base / "venv", base / "cwd", base / "home"
    cwd.mkdir()
    home.mkdir()
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(venv)], check=True)
        subprocess.run([uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
                        str(wheel)], check=True)  # fmt: skip
    else:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv / "bin" / "pip"), "install", "-q", str(wheel)], check=True)
    return {"venv": venv, "cwd": cwd, "home": home}


def aksha(
    installed: dict[str, Path], *argv: str, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    """Run the installed ``aksha`` as a user would: its own PATH, a throwaway home, no repo."""
    env = {
        "PATH": f"{installed['venv'] / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(installed["home"]),
        "LANG": "C.UTF-8",
        "HTTP_PROXY": DEAD, "HTTPS_PROXY": DEAD, "ALL_PROXY": DEAD,
        "http_proxy": DEAD, "https_proxy": DEAD, "all_proxy": DEAD,
    }  # fmt: skip
    return subprocess.run(["aksha", *argv], capture_output=True, text=True, env=env,
                          cwd=installed["cwd"], timeout=timeout, check=False)  # fmt: skip


def test_the_wheel_carries_the_prompts_worlds_and_suites(wheel: Path) -> None:
    names = set(zipfile.ZipFile(wheel).namelist())
    for needed in ("aksha/py.typed", "aksha/agents/research/prompts/instructions.md",
                   "aksha/agents/patent/prompts/instructions.md",
                   "aksha/data/worlds/stillroom/key.json",
                   "aksha/data/worlds/stillroom/sources/notes/C1-self-calibration.md",
                   "aksha/data/worlds/stillroom-v2/key.json",
                   "aksha/data/suites/scripted/suite.toml"):  # fmt: skip
        assert needed in names, needed
    assert not [n for n in names if n.startswith(("tests/", "docs/", "fixtures/", "evals/"))]


def test_the_metadata_names_the_distribution_and_no_dev_extra(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        (meta,) = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        headers = Parser().parsestr(z.read(meta).decode("utf-8"))
        entry = next(n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt"))
        assert "aksha = aksha.interfaces.cli.main:entry" in z.read(entry).decode("utf-8")
    assert headers["Name"] == "aksha-harness"
    assert headers["Requires-Python"] == ">=3.12"
    assert set(headers.get_all("Provides-Extra") or ()) == {"postgres", "server", "otel"}
    assert headers["Version"] in wheel.name


def test_the_sdist_leaves_out_the_repositorys_working_files(sdist: Path) -> None:
    with tarfile.open(sdist) as t:
        names = [n.split("/", 1)[1] for n in t.getnames() if "/" in n]
    leaked = [n for n in names if n.startswith(PRUNED) or n in PRUNED]
    assert leaked == []
    assert "src/aksha/data/worlds/stillroom/key.json" in names and "pyproject.toml" in names


def test_help_and_version_work_from_the_install(installed: dict[str, Path], wheel: Path) -> None:
    out = aksha(installed, "--help")
    assert out.returncode == 0 and "demo" in out.stdout, out.stderr
    version = aksha(installed, "--version")
    assert version.returncode == 0 and version.stdout.split()[1] in wheel.name


def test_the_installed_package_is_imported_not_the_checkout(installed: dict[str, Path]) -> None:
    python = installed["venv"] / "bin" / "python"
    code = "import aksha; print(aksha.__file__)"
    found = subprocess.run([str(python), "-c", code], capture_output=True, text=True,
                           cwd=installed["cwd"], check=True)  # fmt: skip
    path = Path(found.stdout.strip()).resolve()
    assert installed["venv"] in path.parents and REPO not in path.parents


def test_doctor_reports_the_per_user_data_folder(installed: dict[str, Path]) -> None:
    out = aksha(installed, "doctor")
    assert out.returncode == 0, out.stdout + out.stderr
    home = installed["home"] / ".local" / "share" / "aksha"
    assert f"home:     {home}" in out.stdout
    assert 'pip install "aksha-harness[server]"' in out.stdout  # no extras in this install


def test_aksha_demo_runs_offline_from_the_install(installed: dict[str, Path]) -> None:
    report_file = installed["home"] / "demo.json"
    out = aksha(installed, "demo", "--json", str(report_file))
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["chain"]["ok"] and report["kill"]["killed"]
    assert report["mailbox"]["deliveries"] == report["mailbox"]["keys"] == 2
    assert (installed["home"] / ".local" / "share" / "aksha" / "demo").is_dir()
    assert list(installed["cwd"].iterdir()) == []  # nothing lands where the command was typed


def test_aksha_eval_runs_a_bundled_suite_from_the_install(installed: dict[str, Path]) -> None:
    listed = aksha(installed, "eval", "list")
    assert listed.returncode == 0 and "scripted:" in listed.stdout, listed.stderr
    out = aksha(installed, "eval", "run", "scripted", "--no-selftest", "--case", "r-prices")
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert (installed["home"] / ".local" / "share" / "aksha" / "evals").is_dir()
