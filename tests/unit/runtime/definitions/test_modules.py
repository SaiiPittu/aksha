from pathlib import Path

from aksha.runtime.definitions.modules import import_value, scan_sources


def _pkg(tmp_path: Path, files: dict[str, str]) -> Path:
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "__init__.py").write_text('"""Demo."""\n')
    for name, text in files.items():
        (folder / name).write_text(text)
    return folder


def test_import_value_loads_a_fresh_copy_each_time(tmp_path: Path) -> None:
    folder = _pkg(
        tmp_path, {"agent.py": "from . import helper\nAGENT = helper.X\n", "helper.py": "X = 1\n"}
    )
    assert import_value(folder, "_aksha_test_demo", "agent", "AGENT") == 1
    (folder / "helper.py").write_text("X = 2\n")
    assert import_value(folder, "_aksha_test_demo", "agent", "AGENT") == 2


def test_import_value_of_the_package_itself(tmp_path: Path) -> None:
    folder = _pkg(tmp_path, {})
    (folder / "__init__.py").write_text("EXTENSION = 'e'\n")
    assert import_value(folder, "_aksha_test_pkg", None, "EXTENSION") == "e"


def test_import_value_returns_none_for_a_missing_name(tmp_path: Path) -> None:
    folder = _pkg(tmp_path, {"agent.py": "OTHER = 1\n"})
    assert import_value(folder, "_aksha_test_missing", "agent", "AGENT") is None


def test_scan_sources_finds_runtime_imports_and_stop_policy_classes(tmp_path: Path) -> None:
    folder = _pkg(
        tmp_path,
        {
            "agent.py": "from aksha.runtime.api import Agent\nimport json\n",
            "bad.py": "from aksha.runtime.tasks.types import Task\n",
            "policy.py": (
                "class MyPolicy:\n    def prepare(self, v): ...\n    def judge(self, v): ...\n"
            ),
            "sub.py": "class Director(StopPolicy):\n    pass\n",
        },
    )
    (folder / "tests").mkdir()
    (folder / "tests" / "t.py").write_text("from aksha.runtime.tasks import types\n")
    found = sorted((f.file, f.code) for f in scan_sources(folder))
    assert found == [
        ("bad.py", "import_outside_api"),
        ("policy.py", "stop_policy_class"),
        ("sub.py", "stop_policy_class"),
    ]


def test_scan_sources_reads_every_aksha_import_form(tmp_path: Path) -> None:
    folder = _pkg(
        tmp_path,
        {
            "a.py": "from aksha import runtime\n",
            "b.py": "import aksha.adapters.storage.sqlite\n",
            "c.py": "from aksha.app import open_event_store\n",
            "d.py": "import importlib\nimportlib.import_module('aksha.app')\n",
            "e.py": "from aksha.extensions.echo import EXTENSION\n",
            "ok.py": "from aksha.runtime import api\nfrom aksha.runtime.api import Agent\n",
        },
    )
    found = {f.file: f for f in scan_sources(folder)}
    assert sorted(found) == ["a.py", "b.py", "c.py", "d.py", "e.py"]
    assert found["a.py"].target == "aksha.runtime"
    assert found["c.py"].target == "aksha.app.open_event_store"
    assert found["e.py"].named_extension() == "echo"
    assert found["a.py"].named_extension() is None


def test_scan_sources_refuses_network_modules_in_definitions(tmp_path: Path) -> None:
    """ADR-P26 D2: a tool body reaches the network only through ``ctx.http``."""
    folder = _pkg(
        tmp_path,
        {
            "a.py": "import socket\n",
            "b.py": "from urllib import request\n",
            "c.py": "import urllib.request\n",
            "d.py": "from http.client import HTTPSConnection\n",
            "e.py": "import requests\n",
            "f.py": "importlib.import_module('httpx')\n",
            "ok.py": (
                "from urllib.parse import urlsplit\nfrom http import HTTPStatus\nimport json\n"
            ),
        },
    )
    found = sorted((f.file, f.code) for f in scan_sources(folder))
    assert found == [(name, "network_import") for name in
                     ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py")]  # fmt: skip
