"""The bundled worlds and suites are found inside the package, never beside a checkout."""

from __future__ import annotations

from pathlib import Path

import pytest

import aksha
from aksha import data


def test_the_data_root_is_inside_the_package() -> None:
    package = Path(aksha.__file__).resolve().parent
    assert data.root().resolve() == package / "data"
    assert data.worlds().parent == data.root() and data.suites().parent == data.root()


def test_every_bundled_world_has_sources_and_a_key() -> None:
    names = sorted(p.name for p in data.worlds().iterdir() if p.is_dir())
    assert {"stillroom", "stillroom-v2"} <= set(names)
    for name in names:
        world = data.world_path(name)
        assert (world / "sources").is_dir() and (world / "key.json").is_file()


def test_the_bundled_suites_are_there() -> None:
    suites = {p.name for p in data.suites().iterdir() if (p / "suite.toml").is_file()}
    assert {"change", "patent", "research", "scripted"} <= suites


def test_an_unknown_world_names_the_ones_there_are() -> None:
    with pytest.raises(KeyError) as e:
        data.world_path("nowhere")
    assert "stillroom" in str(e.value)
