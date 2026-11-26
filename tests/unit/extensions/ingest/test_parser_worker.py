"""Parsers in a worker process: hard limits, no network, a named gap, the runtime unharmed.

Every test runs a real worker process. The hostile parsers are stand-ins
(``hostile_parsers.py``) for what a hostile file can make a real parser do; the zip bomb
and the normal formats go through the real parsers. The bubblewrap tests skip, with the
probe's reason, on a host where the backend does not probe available.
"""

from __future__ import annotations

import io
import os
import socket
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.app import RuntimeConfig, parser_pool
from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.ingest import Ingested, ingest
from aksha.extensions.ingest.parse import PARSER_CRASHED, PARSER_MEMORY, PARSER_TIMEOUT
from aksha.extensions.ingest.types import Caps
from aksha.extensions.ingest.worker import ParserPool
from aksha.uploads import UploadIngester
from tests.support import ingest_fixtures as fx
from tests.unit.extensions.ingest import hostile_parsers

H = hostile_parsers.HERE
FAST = Caps(parser_time_ms=3_000, parser_cpu_s=2, parser_memory_bytes=768 * 1024 * 1024)
BOX = BwrapSandbox()
PROBE = BOX.probe()
needs_box = pytest.mark.skipif(not PROBE.available, reason=f"bwrap: {PROBE.reason}")


@pytest.fixture
def pool() -> Iterator[ParserPool]:
    p = ParserPool()
    yield p
    p.close()


def hostile(target: str) -> ParserPool:
    return ParserPool(target=f"{H}:{target}")


def run(name: str, data: bytes, parse: ParserPool, caps: Caps = FAST) -> Ingested:
    got = ingest(name, data, doc_key="DOC-0001", key_for=fx.keys(), caps=caps, parse=parse)
    assert isinstance(got, Ingested)
    return got


def idle(p: ParserPool) -> int:
    return sum(len(w) for w in p._idle.values())


def _frames(got: Ingested) -> list[tuple[str, tuple[int, int], bytes, str, bool]]:
    return [(f.pixels.mode, f.pixels.size, f.pixels.tobytes(), f.source_format,
             f.passthrough is not None) for f in got.frames]  # fmt: skip


def _same(a: Ingested, b: Ingested) -> None:
    assert a.document == b.document
    assert a.representation == b.representation
    assert a.gap == b.gap and a.header == b.header and a.route == b.route
    assert _frames(a) == _frames(b)
    assert [c.document for c in a.walk()] == [c.document for c in b.walk()]
    assert [c.record_text for c in a.walk()] == [c.record_text for c in b.walk()]


# --- the normal formats -----------------------------------------------------------------


def _formats() -> list[tuple[str, bytes]]:
    fx.need("pypdf")
    fx.need("docx")
    fx.need("PIL")
    return [
        ("terms.pdf", fx.text_pdf(["Clause 1: the price is fixed for a year." * 2, None])),
        ("brief.docx", fx.docx_bytes()),
        ("deck.pptx", fx.pptx_bytes()),
        ("book.xlsx", fx.xlsx_bytes()),
        ("mail.eml", fx.eml_bytes(fx.text_pdf(["The attached schedule, page one." * 2]))),
        ("photo.jpg", fx.jpeg_rotated_with_gps()),
        ("chart.png", fx.png_bytes()),
        ("scan.tiff", fx.multipage_tiff()),
        ("loop.gif", fx.animated_gif()),
        ("notes.md", b"# SYN-1 notes\n\nA line.\n"),
        ("rows.csv", b"a;b\n1;2\n"),
        ("doc.json", b'{"id": "SYN-2", "n": 1}'),
        ("page.html", b"<html><body><h1>SYN-3</h1><script>x()</script></body></html>"),
        ("latin.txt", "caf\xe9".encode("latin-1")),
        ("plain.zip", fx.plain_zip()),
        ("locked.pdf", fx.encrypted_pdf()),
    ]


def test_every_normal_format_parses_in_the_worker_exactly_as_in_process(
    pool: ParserPool,
) -> None:
    for name, data in _formats():
        in_worker = run(name, data, pool, Caps())
        here = ingest(name, data, doc_key="DOC-0001", key_for=fx.keys())
        assert isinstance(here, Ingested)
        _same(in_worker, here)
    assert idle(pool) >= 1  # the pool kept a worker for the next input
    assert pool.isolation  # the worker said what isolation it got


def test_an_upload_runs_its_parsers_in_the_worker_pool_by_default() -> None:
    ingester = UploadIngester()
    assert isinstance(ingester.parse, ParserPool)
    served = ingester.ingest({"terms.pdf": fx.text_pdf(["SYN-4 price list, page one." * 2])},
                             {}, 1, None)  # fmt: skip
    assert b"[p.1]" in served[0].data
    assert idle(ingester.parse) == 1
    ingester.parse.close()


# --- hard limits ------------------------------------------------------------------------


def test_a_slow_parser_is_stopped_at_the_wall_clock_and_its_type_is_kept() -> None:
    p = hostile("sleeps")
    start = time.monotonic()
    got = run("notes.md", b"# heading\n", p, Caps(parser_time_ms=1_500))
    took = time.monotonic() - start
    assert got.gap is not None and got.gap.reason == "unreadable"
    assert got.gap.detail == f"{PARSER_TIMEOUT}: no result within 1,500 ms; the parser was stopped"
    assert got.document.detected_type == sniff.MARKDOWN  # the type check reported first
    assert 1.4 < took < 10
    assert idle(p) == 0  # the worker that hit the limit was discarded
    assert "unreadable" in got.header and PARSER_TIMEOUT in got.header


def test_a_parser_spinning_on_the_cpu_hits_the_cpu_cap() -> None:
    p = hostile("spins")
    start = time.monotonic()
    got = run("notes.txt", b"plain words\n", p, Caps(parser_time_ms=30_000, parser_cpu_s=1))
    assert got.gap is not None
    assert got.gap.detail == f"{PARSER_TIMEOUT}: over the CPU cap of 1 s; the parser was stopped"
    assert time.monotonic() - start < 10
    assert idle(p) == 0


def test_a_memory_hog_is_a_parser_memory_gap() -> None:
    p = hostile("hogs")
    got = run("notes.txt", b"plain words\n", p,
              Caps(parser_time_ms=30_000, parser_memory_bytes=512 * 1024 * 1024))  # fmt: skip
    assert got.gap is not None
    assert (
        got.gap.detail == f"{PARSER_MEMORY}: over the memory cap of 512 MiB; the parser was stopped"
    )
    assert got.document.detected_type == sniff.TEXT
    assert idle(p) == 0


def test_a_crashing_parser_is_a_parser_crashed_gap_and_the_runtime_goes_on() -> None:
    p = hostile("segfaults")
    got = run("notes.txt", b"plain words\n", p)
    assert got.gap is not None
    assert got.gap.detail == f"{PARSER_CRASHED}: the parser process died (signal SIGSEGV)"
    assert idle(p) == 0
    again = run("notes.txt", b"plain words\n", p)  # a fresh worker, the same clean gap
    assert again.gap == got.gap


def test_a_reply_the_protocol_does_not_allow_is_parser_crashed() -> None:
    got = run("notes.txt", b"plain words\n", hostile("lies"))
    assert got.gap is not None
    assert got.gap.detail == (f"{PARSER_CRASHED}: the parser sent a reply the protocol does "
                              "not allow")  # fmt: skip


def _bomb(entry_bytes: int) -> bytes:
    """A real DOCX whose body part expands to ``entry_bytes`` bytes (written streaming)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fx.docx_bytes())) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:  # fmt: skip
        for info in src.infolist():
            if info.filename != "word/document.xml":
                z.writestr(info.filename, src.read(info.filename))
        with z.open("word/document.xml", "w", force_zip64=True) as f:
            chunk = b" " * (1 << 20)
            for _ in range(entry_bytes >> 20):
                f.write(chunk)
    return out.getvalue()


def test_a_zip_bomb_is_refused_by_the_caps_and_past_them_stopped_by_the_memory_cap(
    pool: ParserPool,
) -> None:
    fx.need("docx")
    bomb = _bomb(1024 * 1024 * 1024)  # 1 GiB of zeros in about 4.5 MB
    assert len(bomb) < Caps().max_bytes
    refused = run("bomb.docx", bomb, pool, Caps())
    assert refused.gap is not None and refused.gap.reason == "refused"
    assert refused.gap.detail.startswith("decompression_cap: expands to ")
    lifted = Caps(max_zip_bytes=2**40, parser_time_ms=30_000,
                  parser_memory_bytes=512 * 1024 * 1024)  # fmt: skip
    start = time.monotonic()
    got = run("bomb.docx", bomb, pool, lifted)
    assert got.gap is not None and got.gap.detail.startswith(PARSER_MEMORY)
    assert got.document.detected_type == sniff.DOCX
    assert time.monotonic() - start < 30
    assert run("brief.docx", fx.docx_bytes(), pool, Caps()).gap is None  # the pool goes on


# --- isolation and reuse ----------------------------------------------------------------


def test_a_worker_has_no_network() -> None:
    p = hostile("dials")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        got = run("port.txt", str(port).encode(), p)
        if not any("own namespace" in i for i in p.isolation):
            pytest.skip(f"no network namespace on this host: {p.isolation}")
        assert got.representation is not None
        assert got.representation.text.startswith("blocked: ")
        with pytest.raises(TimeoutError):
            listener.accept()
    p.close()


def test_a_worker_cannot_fork_or_write_files_and_gets_no_host_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKSHA_SYN_SECRET", "SYN-not-for-parsers")
    p = hostile("probes")
    got = run("probe.txt", str(tmp_path).encode(), p)
    assert got.representation is not None
    seen = got.representation.text.splitlines()
    assert "fork: BlockingIOError" in seen
    assert "write: OSError" in seen
    assert "env: LANG,PATH" in seen  # the host's environment, keys included, stays out
    p.close()


def test_a_worker_is_reused_and_one_that_hit_a_limit_is_discarded() -> None:
    p = ParserPool(target=f"{H}:pid", size=1)
    first = run("a.txt", b"one\n", p)
    second = run("b.txt", b"two\n", p)
    assert first.representation is not None and second.representation is not None
    assert first.representation.text == second.representation.text  # the same process
    assert idle(p) == 1
    p.target = f"{H}:segfaults"
    run("c.txt", b"three\n", p)
    assert idle(p) == 0
    p.target = f"{H}:pid"
    third = run("d.txt", b"four\n", p)
    assert third.representation is not None
    assert third.representation.text != first.representation.text  # a new process
    p.close()


def test_a_worker_is_retired_after_max_jobs() -> None:
    p = ParserPool(target=f"{H}:pid", max_jobs=2)
    pids = []
    for n in range(3):
        got = run(f"{n}.txt", b"x\n", p)
        assert got.representation is not None
        pids.append(got.representation.text)
    assert pids[0] == pids[1] != pids[2]
    p.close()


# --- inside bubblewrap ------------------------------------------------------------------


@needs_box
def test_a_jailed_worker_parses_the_normal_formats(tmp_path: Path) -> None:
    p = ParserPool(jail=BOX.jail_argv, scratch=tmp_path)
    for name, data in _formats()[:6]:
        here = ingest(name, data, doc_key="DOC-0001", key_for=fx.keys())
        assert isinstance(here, Ingested)
        _same(run(name, data, p, Caps()), here)
    assert p.isolation[0].startswith("bubblewrap")
    assert list(tmp_path.iterdir()) == []  # each box's input file was removed
    assert idle(p) == 0  # a jailed worker is never pooled


@needs_box
def test_a_jailed_worker_sees_only_its_input_read_only(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("SYN-host-only")
    p = ParserPool(jail=BOX.jail_argv, scratch=tmp_path, target=f"{H}:jailed_view")
    got = run("probe.txt", str(secret).encode(), p)
    assert got.representation is not None
    assert got.representation.text.splitlines() == [
        "input: PermissionError",  # bound read-only
        "host file: absent",  # nothing of the host but the system and Python
        "inputs: document",  # the one input file
    ]


@needs_box
def test_a_jailed_slow_parser_is_stopped_and_its_box_removed(tmp_path: Path) -> None:
    p = ParserPool(jail=BOX.jail_argv, scratch=tmp_path, target=f"{H}:sleeps")
    start = time.monotonic()
    got = run("notes.md", b"# heading\n", p, Caps(parser_time_ms=1_500))
    assert got.gap is not None and got.gap.detail.startswith(PARSER_TIMEOUT)
    assert time.monotonic() - start < 10
    assert list(tmp_path.iterdir()) == []


@needs_box
def test_the_upload_ingester_is_jailed_when_bubblewrap_is_configured(tmp_path: Path) -> None:
    on = parser_pool(RuntimeConfig.from_env({"AKSHA_SANDBOX": "bwrap",
                                             "AKSHA_SANDBOX_PATH": str(tmp_path)}))  # fmt: skip
    off = parser_pool(RuntimeConfig.from_env({"AKSHA_SANDBOX_PATH": str(tmp_path)}))
    assert on.jail is not None and on.scratch == tmp_path / "parse"
    assert off.jail is None


def test_no_worker_outlives_its_pool() -> None:
    p = ParserPool(target=f"{H}:pid")
    got = run("a.txt", b"x\n", p)
    assert got.representation is not None
    worker_pid = int(got.representation.text)
    p.close()
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
