"""The correction loop end to end on a world, offline (ADR-P05 C1, ADR-P17 C1, ADR-P04 C1).

A person or an agent proposes a correction to a source record or an output value, with
evidence; it waits on the domain owner; an approved correction to an uploaded file becomes
that file's next version, one to a shared source goes out through the outbox to its system
of record, and one to an output value is saved as the slot's next version. Each time the
work built on the old value is rebuilt, and the correction shows what replaced it and the
replacement's lineage. The research agent runs on the ``tilldew`` world (synthetic).
"""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

from aksha import data
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from aksha.runtime.canonical import thaw
from tests.support.events import START
from tests.support.session import env

TILLDEW = data.world_path("tilldew") / "sources"
REQUEST = (data.suites() / "tilldew" / "g-research-happy.toml").read_text("utf-8")
PROMPT = re.search(r'^opening = "(.*)"$', REQUEST, re.M).group(1)  # type: ignore[union-attr]
REVIEWS = "exports/farmer-reviews.csv"
OLD_ROW = "Too expensive for a two-acre plot."
NEW_ROW = "Too expensive, and the battery died."


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    settings = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
                "AKSHA_SEARCH": "world:tilldew", **extra}  # fmt: skip
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **settings))
    return code, out.getvalue()


def events(tmp: Path, type_: str | None = None) -> list[tuple[str | None, dict[str, object]]]:
    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        return [(e.run, thaw(e.payload)) for e in store.read("o1")
                if type_ is None or e.type == type_]  # fmt: skip
    finally:
        store.close()


def steps(tmp: Path, run: str) -> set[str]:
    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        return {str(e.step).split("/")[1] for e in store.read("o1")
                if e.type == "step.completed" and e.run == run}  # fmt: skip
    finally:
        store.close()


def first_run(tmp: Path, folder: Path, **extra: str) -> None:
    code, out = aksha(tmp, "run", PROMPT, "--agent", "research", "--input", str(folder), **extra)
    assert code == wire.EXIT_OK and "R-0001 research ended complete" in out, out


def test_an_approved_correction_to_an_upload_versions_the_file_and_rebuilds_only_its_work(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "farm-data"
    shutil.copytree(TILLDEW, folder)
    first_run(tmp_path, folder)
    fixed = tmp_path / "fixed.csv"
    fixed.write_text((folder / REVIEWS).read_text("utf-8").replace(OLD_ROW, NEW_ROW), "utf-8")

    code, out = aksha(tmp_path, "correct", f"input/{REVIEWS}", "--file", str(fixed))
    assert code == wire.EXIT_REFUSED and "evidence_required" in out, out
    code, out = aksha(tmp_path, "correct", f"input/{REVIEWS}", "--file", str(fixed),
                      "--evidence", "SYN-RV-07's original post also reports a dead battery",
                      "--cite", "input/mail/dealer-note.eml.txt")  # fmt: skip
    assert code == wire.EXIT_OK, out
    assert "proposed K-0001: input/exports/farmer-reviews.csv (upload), waiting for person" in out
    # A proposal changes nothing: no file, no source change, no run.
    assert (folder / REVIEWS).read_text("utf-8").count(OLD_ROW) == 1
    assert [p for _, p in events(tmp_path, "source.changed")
            if "correction" in str(p.get("change_set"))] == []  # fmt: skip
    code, out = aksha(tmp_path, "corrections")
    assert code == wire.EXIT_OK and "K-0001 proposed input/exports/farmer-reviews.csv" in out

    code, out = aksha(tmp_path, "approve", "K-0001", "--by", "agent:research")
    assert code == wire.EXIT_REFUSED and "not_domain_owner" in out, out
    code, out = aksha(tmp_path, "approve", "K-0001")
    assert code == wire.EXIT_OK, out
    assert "K-0001 applied" in out and "R-0002 research ended complete" in out, out
    # The review export is read by voice alone; the report reads voice's needs.
    assert steps(tmp_path, "R-0002") == {"voice", "report"}
    assert "research.needs@v1 replaced by research.needs@v2" in out, out
    assert "read input/exports/farmer-reviews.csv" in out, out

    (change,) = [p for _, p in events(tmp_path, "source.changed")
                 if p.get("change_set") == "correction:K-0001"]  # fmt: skip
    (applied,) = [p for _, p in events(tmp_path, "correction.applied")]
    assert applied["route"] == "upload"
    assert applied["source_version"] == change["source_version"]
    assert str(applied["record_version"]).startswith("sha256:")
    code, out = aksha(tmp_path, "show", "research.needs")
    assert code == wire.EXIT_OK and json.loads(out)["freshness"] == "current", out

    code, out = aksha(tmp_path, "corrections", "K-0001", "--json")
    shown = json.loads(out)
    assert shown["state"] == "applied" and shown["decided_by"] == "person"
    needs = next(a for a in shown["affected"] if a["ref"].startswith("research.needs@"))
    assert needs["state"] == "replaced" and needs["replacement"] == "research.needs@v2"
    assert any(x.startswith(f"input/{REVIEWS}") for x in needs["lineage"]), needs
    # The scope and discovery read no review row: the correction did not reopen them.
    assert not any(a["ref"].startswith(("research.scope", "research.competitors"))
                   for a in shown["affected"])  # fmt: skip

    code, out = aksha(tmp_path, "approve", "K-0001")
    assert code == wire.EXIT_REFUSED and "correction_closed" in out, out


def test_a_rejected_correction_changes_nothing(tmp_path: Path) -> None:
    first_run(tmp_path, TILLDEW)
    code, out = aksha(tmp_path, "correct", f"input/{REVIEWS}", "--text", "id\n",
                      "--evidence", "the export is wrong")  # fmt: skip
    assert code == wire.EXIT_OK, out
    before = len(events(tmp_path))
    code, out = aksha(tmp_path, "reject", "K-0001", "--note", "the export is right")
    assert code == wire.EXIT_OK and "K-0001 rejected" in out, out
    added = events(tmp_path)[before:]
    assert {p.get("decision") for _, p in added if "decision" in p} >= {"rejected"}
    assert not [p for _, p in added if "source_version" in p]
    code, out = aksha(tmp_path, "corrections", "K-0001")
    assert "decided by person: the export is right" in out, out


def test_an_approved_correction_to_a_shared_source_goes_out_through_the_outbox(
    tmp_path: Path,
) -> None:
    """The record is not the owner's upload: the system of record owns it, so the approved
    text is written there once, through the outbox, and only then recorded as moved."""
    shared = tmp_path / "farm-db"
    shutil.copytree(TILLDEW, shared)
    settings = {"AKSHA_SOURCES": f"inputs={shared}"}
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", **settings)
    assert code == wire.EXIT_OK and "R-0001 research ended complete" in out, out
    fixed = (shared / REVIEWS).read_text("utf-8").replace(OLD_ROW, NEW_ROW)
    argv = ("correct", f"input/{REVIEWS}", "--text", fixed, "--evidence", "the dealer's note")

    code, out = aksha(tmp_path, *argv, **settings)
    assert code == wire.EXIT_REFUSED and "no_system_of_record" in out, out

    settings |= {"AKSHA_CORRECTIONS": "inputs=farm_db",
                 "AKSHA_SYSTEMS": f"farm_db=folder:{shared}",
                 "AKSHA_CORRECTION_APPROVER": "inputs=data-steward"}  # fmt: skip
    code, out = aksha(tmp_path, *argv, **settings)
    assert code == wire.EXIT_OK and "(outbox), waiting for data-steward" in out, out
    assert "aksha approve K-0001 --by data-steward" in out, out
    code, out = aksha(tmp_path, "approve", "K-0001", **settings)
    assert code == wire.EXIT_REFUSED and "not_domain_owner" in out, out
    assert (shared / REVIEWS).read_text("utf-8").count(OLD_ROW) == 1

    code, out = aksha(tmp_path, "approve", "K-0001", "--by", "data-steward", **settings)
    assert code == wire.EXIT_OK, out
    assert "K-0001 applied" in out and "R-0002 research ended complete" in out, out
    assert (shared / REVIEWS).read_text("utf-8").count(NEW_ROW) == 1  # the system's record
    assert steps(tmp_path, "R-0002") == {"voice", "report"}
    (entry,) = [p for _, p in events(tmp_path, "outbox.enqueued")
                if p.get("tool") == "correction.write"]  # fmt: skip
    (_done,) = [p for _, p in events(tmp_path, "outbox.completed")
               if p.get("key") == entry["key"]]  # fmt: skip
    (applied,) = [p for _, p in events(tmp_path, "correction.applied")]
    assert applied["route"] == "outbox" and applied["outbox_key"] == entry["key"]
    ledger = json.loads((tmp_path / "var" / "systems" / "farm_db" / "ledger.json").read_text())
    assert list(ledger) == [entry["key"]]  # written once
    code, out = aksha(tmp_path, "resume", **settings)  # nothing is written or recorded again
    assert code == wire.EXIT_OK, out
    assert len([1 for _, p in events(tmp_path, "correction.applied")]) == 1
    assert list(json.loads((tmp_path / "var" / "systems" / "farm_db" / "ledger.json")
                           .read_text())) == [entry["key"]]  # fmt: skip


def test_an_approved_correction_to_an_output_value_saves_a_version_and_rebuilds_its_readers(
    tmp_path: Path,
) -> None:
    first_run(tmp_path, TILLDEW)
    slot = "research.profile/c02-aquasense-labs"
    code, out = aksha(tmp_path, "correct", f"{slot}#/name", "--text", "AquaSense Labs",
                      "--evidence", "x")  # fmt: skip
    assert code == wire.EXIT_REFUSED and "correction_unchanged" in out, out
    code, out = aksha(tmp_path, "correct", f"{slot}#/nope", "--text", "x", "--evidence", "x")
    assert code == wire.EXIT_REFUSED and "target_unknown" in out, out
    code, out = aksha(tmp_path, "correct", f"{slot}@v1#/name", "--text", "AquaSense Agritech",
                      "--evidence", "the registry lists the company as AquaSense Agritech",
                      "--by", "agent:research")  # fmt: skip
    assert code == wire.EXIT_OK and "(artifact), waiting for person" in out, out
    code, out = aksha(tmp_path, "approve", "K-0001")
    assert code == wire.EXIT_OK and "K-0001 applied" in out, out
    assert "R-0002 research ended complete" in out, out
    for reader in ("matrix", "findings", "tests"):
        assert f"research.{reader}@v1 replaced by research.{reader}@v2" in out, out
    assert f"read {slot}@v2" in out, out
    # The corrected profile is not regenerated over: no evidence, discovery or scope step.
    rebuilt = steps(tmp_path, "R-0002")
    assert rebuilt and not any(s.startswith(("evidence", "discover", "scope", "voice"))
                               for s in rebuilt), rebuilt  # fmt: skip
    code, out = aksha(tmp_path, "show", slot)
    shown = json.loads(out)
    assert shown["ref"] == f"{slot}@v2" and shown["content"]["name"] == "AquaSense Agritech"
    (saved,) = [p for _, p in events(tmp_path, "artifact.saved") if p["ref"] == f"{slot}@v2"]
    assert saved["origin"] == "correction" and saved["correction"] == "K-0001"
    reads = [(r["kind"], r["id"], r["version"]) for r in saved["reads"]]  # type: ignore[attr-defined]
    assert ("artifact", slot, "v1") in reads  # lineage: the version it corrects
    code, out = aksha(tmp_path, "show", "research.matrix")
    assert json.loads(out)["freshness"] == "current", out


def _session(tmp: Path, **changes: object):  # type: ignore[no-untyped-def]
    from aksha.app import open_session
    from tests.support.session import config

    return open_session(config(tmp, agent_paths=(DEFAULT_AGENTS,),
                               extension_paths=(DEFAULT_EXTENSIONS,), search="world:tilldew",
                               **changes))  # fmt: skip


def test_a_decision_whose_upload_never_happened_is_applied_on_the_next_pass(
    tmp_path: Path,
) -> None:
    """The process stops between the owner's decision and the file write: the decision is
    a fact, so the next pass (or ``aksha resume``) writes and records it, once."""
    from aksha.runtime.corrections.commands import decide_correction
    from aksha.runtime.corrections.resolve import version_now
    from aksha.runtime.projections.view import load_view
    from aksha.runtime.session import named as n

    folder = tmp_path / "farm-data"
    shutil.copytree(TILLDEW, folder)
    first_run(tmp_path, folder)
    fixed = (folder / REVIEWS).read_text("utf-8").replace(OLD_ROW, NEW_ROW)
    session = _session(tmp_path)
    try:
        bad = session.dispatch(n.ProposeCorrection(owner="o1", target=f"input/{REVIEWS}",
                                                   value=fixed, evidence=("x",),
                                                   by="agent:nobody"))  # fmt: skip
        assert (bad.accepted, bad.refused) == (False, "proposer_unknown")
        proposed = n.ProposeCorrection(owner="o1", target=f"input/{REVIEWS}", value=fixed,
                                       evidence=("the dealer's note",), by="agent:research",
                                       command_id="c-propose-1")  # fmt: skip
        assert session.dispatch(proposed).accepted
        assert session.dispatch(proposed).duplicate  # a retry returns the first receipt
        sources = session._versioned()

        def now(target):  # type: ignore[no-untyped-def]
            return version_now(target, "o1", load_view(session.store, "o1"), sources)

        # The decision alone, as a process that stopped before applying it left it.
        decided = decide_correction(session.store, "o1", "K-0001", approve=True, by="person",
                                    version_now=now)  # fmt: skip
        assert decided.accepted
        assert (folder / REVIEWS).read_text("utf-8").count(NEW_ROW) == 0
        workspace = tmp_path / "var" / "inputs" / "o1" / REVIEWS
        assert workspace.read_text("utf-8").count(NEW_ROW) == 0
    finally:
        session.store.close()

    code, out = aksha(tmp_path, "resume")
    assert code == wire.EXIT_OK, out
    assert workspace.read_text("utf-8").count(NEW_ROW) == 1
    assert len(events(tmp_path, "correction.applied")) == 1
    assert steps(tmp_path, "R-0002") == {"voice", "report"}
    code, out = aksha(tmp_path, "resume")
    assert len(events(tmp_path, "correction.applied")) == 1  # applied once


def test_a_correction_whose_target_moved_since_it_was_proposed_is_refused(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "farm-data"
    shutil.copytree(TILLDEW, folder)
    first_run(tmp_path, folder)
    fixed = (folder / REVIEWS).read_text("utf-8").replace(OLD_ROW, NEW_ROW)
    code, out = aksha(tmp_path, "correct", f"input/{REVIEWS}", "--text", fixed,
                      "--evidence", "the dealer's note")  # fmt: skip
    assert code == wire.EXIT_OK, out
    (folder / REVIEWS).write_text(fixed.replace(NEW_ROW, "Another reading."), "utf-8")
    code, out = aksha(tmp_path, "upload", str(folder))
    assert code == wire.EXIT_OK, out
    code, out = aksha(tmp_path, "approve", "K-0001")
    assert code == wire.EXIT_REFUSED and "target_moved" in out, out
    assert events(tmp_path, "correction.decided") == []


def test_the_correction_commands_cross_the_wire_unchanged() -> None:
    from aksha.runtime.session import named as n

    proposed = n.ProposeCorrection(owner="o1", target="catalog.item/k1#/name", value={"a": [1]},
                                   evidence=("why",), cites=("input/a.md",), by="agent:x",
                                   command_id="c-1")  # fmt: skip
    back = wire.command_from_json("o1", json.loads(json.dumps(wire.command_to_json(proposed))))
    assert isinstance(back, n.ProposeCorrection)
    got = (back.target, back.value, tuple(back.evidence), tuple(back.cites), back.by)
    assert got == (proposed.target, proposed.value, proposed.evidence, proposed.cites, proposed.by)
    decided = n.DecideCorrection(owner="o1", correction_id="K-0001", approve=True,
                                 by="data-steward", command_id="c-2")  # fmt: skip
    assert wire.command_from_json("o1", wire.command_to_json(decided)) == decided
    bad = {**wire.command_to_json(decided), "args": {"correction_id": "K-0001", "approve": "yes"}}
    try:
        wire.command_from_json("o1", bad)
    except wire.WireInvalid:
        pass
    else:  # pragma: no cover
        raise AssertionError("a string is no bool")


def test_an_upload_made_after_the_decision_is_never_overwritten_by_it(tmp_path: Path) -> None:
    """The process stops between the decision and the file write, and the owner uploads a
    newer file before the next pass: the newer upload stands, and nothing is applied."""
    from aksha.runtime.corrections.commands import decide_correction
    from aksha.runtime.corrections.resolve import version_now
    from aksha.runtime.projections.view import load_view
    from aksha.runtime.session import named as n

    folder = tmp_path / "farm-data"
    shutil.copytree(TILLDEW, folder)
    first_run(tmp_path, folder)
    original = (folder / REVIEWS).read_text("utf-8")
    newer = original.replace(OLD_ROW, "Another reading.")
    session = _session(tmp_path)
    try:
        proposed = n.ProposeCorrection(owner="o1", target=f"input/{REVIEWS}",
                                       value=original.replace(OLD_ROW, NEW_ROW),
                                       evidence=("the dealer's note",))  # fmt: skip
        assert session.dispatch(proposed).accepted
        sources = session._versioned()

        def now(target):  # type: ignore[no-untyped-def]
            return version_now(target, "o1", load_view(session.store, "o1"), sources)

        assert decide_correction(session.store, "o1", "K-0001", approve=True, by="person",
                                 version_now=now).accepted  # fmt: skip
        session.rebuilds = False
        assert session.dispatch(n.UploadInputs(owner="o1", files={REVIEWS: newer})).accepted
    finally:
        session.store.close()

    code, out = aksha(tmp_path, "resume")
    assert code == wire.EXIT_OK, out
    workspace = tmp_path / "var" / "inputs" / "o1" / REVIEWS
    assert workspace.read_text("utf-8") == newer
    assert events(tmp_path, "correction.applied") == []
    code, out = aksha(tmp_path, "corrections")
    assert "K-0001 approved" in out, out


def test_a_run_that_recomputes_does_not_regenerate_over_a_corrected_value(
    tmp_path: Path,
) -> None:
    """A correction replaces the slot for every run: with reuse off, a later run redoes
    its work but takes the corrected version as done instead of saving over it."""
    first_run(tmp_path, TILLDEW)
    slot = "research.profile/c02-aquasense-labs"
    code, out = aksha(tmp_path, "correct", f"{slot}#/name", "--text", "AquaSense Agritech",
                      "--evidence", "the registry lists it as AquaSense Agritech")  # fmt: skip
    assert code == wire.EXIT_OK, out
    code, out = aksha(tmp_path, "approve", "K-0001")
    assert code == wire.EXIT_OK and "K-0001 applied" in out, out
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", AKSHA_REUSE="off")
    assert code == wire.EXIT_OK and "R-0003 research ended complete" in out, out
    assert steps(tmp_path, "R-0003")  # it recomputed
    assert not [p for r, p in events(tmp_path, "artifact.saved")
                if r == "R-0003" and str(p["ref"]).startswith(slot)]  # fmt: skip
    code, out = aksha(tmp_path, "show", slot)
    assert json.loads(out)["content"]["name"] == "AquaSense Agritech", out


def test_a_corrected_output_has_its_computed_counts_recomputed(tmp_path: Path) -> None:
    """ADR-P04 K1: a correction's save applies the kind's computed fields as any save does.

    Dropping a review from a need's evidence changes its "N of M" phrase with it: the phrase
    is computed from the evidence at save, never left as the old version said it.
    """
    first_run(tmp_path, TILLDEW)
    code, out = aksha(tmp_path, "show", "research.needs")
    need = next(n for n in json.loads(out)["content"]["needs"] if len(n["evidence"]) >= 2)
    i = json.loads(out)["content"]["needs"].index(need)
    m = int(need["phrase"].split(" of ")[1].split()[0])
    fewer = json.dumps(need["evidence"][:-1])
    code, out = aksha(tmp_path, "correct", f"research.needs#/needs/{i}/evidence", "--value",
                      fewer, "--evidence", "one review is under the wrong theme")  # fmt: skip
    assert code == wire.EXIT_OK, out
    # Only the corrected version is asserted here: how its readers rebuild is the test above.
    code, out = aksha(tmp_path, "approve", "K-0001")
    assert "K-0001 applied" in out, out
    code, out = aksha(tmp_path, "show", "research.needs")
    shown = json.loads(out)
    assert shown["ref"] == "research.needs@v2", out
    fixed = shown["content"]["needs"][i]
    n = len(need["evidence"]) - 1
    assert fixed["phrase"] == f"{n} of {m} reviews in this sample"
    assert (fixed.get("count"), fixed.get("of")) == (n, m)
