"""Diff reviews, the rebuild plan's modes and holds, F2, revocation and refused effects."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import InputRef, WriterId
from aksha.runtime.invalidation.commands import (
    CARD_HASH_MISMATCH,
    REVIEW_NOT_OPEN,
    accept_review,
    apply_source_change,
    reject_review,
)
from aksha.runtime.invalidation.confirm import REVISION_INVALIDATED
from aksha.runtime.invalidation.effects import (
    TARGET_DIRTY,
    TARGET_INVALIDATED,
    TARGET_PENDING_REVIEW,
    authority,
    effect_refusal,
)
from aksha.runtime.invalidation.plan import rebuild_plan, writer_has_rebuild
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.outbox.types import OutboxEntry
from tests.support.invalidation import OWNER, Bench, FakeSource, record, ref

PACKER = WriterId("p", "pack", None)


def a(text: str) -> InputRef:
    return ref(text).as_input()


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    """A form read by an approved packet; the form's record then changes and it rebuilds."""
    b = Bench(tmp_path)
    b.start("R-0001")
    b.save("R-0001", "p.form/C1", {"names": ["a"]}, reads=[record("K")])
    b.save("R-0001", "p.packet/C1", {"forms": 1}, reads=[a("p.form/C1@v1")], writer=PACKER)
    b.approve("A-0001", "p.packet/C1@v1")
    b.end("R-0001")
    yield b
    b.close()


def move(b: Bench, n: int = 1) -> None:
    source = FakeSource()
    source.versions[OWNER] = n - 1
    source.change(OWNER, {"refs/K": str(n + 1)})
    assert apply_source_change(b.store, OWNER, [source], f"cs{n}").accepted


def rebuild_form(b: Bench, names: list[str], version: str = "2") -> None:
    b.save("R-0002", "p.form/C1", {"names": names}, reads=[record("K", version)])


def propose(b: Bench, interrupt: str = "I-0001", forms: int = 2) -> str:
    b.save("R-0002", "p.packet/C1", {"forms": forms}, reads=[a("p.form/C1@v2")],
           writer=PACKER, review=interrupt)  # fmt: skip
    return open_reviews(b.view())["p.packet/C1@v1"].card_hash


@pytest.fixture
def stale(bench: Bench) -> Bench:
    move(bench)
    bench.rebuild_run("R-0002", ["R-0001"])
    return bench


def test_revocation_commits_with_cause(stale: Bench) -> None:
    """``approval.revoked`` commits in the save's own transaction, naming what moved."""
    rebuild_form(stale, ["a", "b"])
    saved = [e for e in stale.events() if e.type == "artifact.saved"][-1]
    (revoked,) = [e for e in stale.events() if e.type == "approval.revoked"]
    assert revoked.seq > saved.seq and revoked.step is None
    between = [e.type for e in stale.events() if saved.seq <= e.seq <= revoked.seq]
    assert between == ["artifact.saved", "approval.revoked"]
    assert revoked.payload["moved"][0]["id"] == "p.form/C1"


def test_an_equal_rebuild_revokes_nothing(stale: Bench) -> None:
    rebuild_form(stale, ["a"])
    assert "approval.revoked" not in stale.types()
    assert stale.iv().state("p.packet/C1@v1").freshness == "current_by_cutoff"


def test_human_reviewed_version_is_proposed_and_writer_is_done_after(stale: Bench) -> None:
    """F2: test_propose_writer_done_after_pending_revision."""
    rebuild_form(stale, ["a", "b"])
    (entry,) = rebuild_plan(stale.iv())
    assert (entry.mode, entry.writer) == ("propose", PACKER)
    assert writer_has_rebuild(rebuild_plan(stale.iv()), PACKER)
    propose(stale)
    assert not writer_has_rebuild(rebuild_plan(stale.iv()), PACKER)
    assert rebuild_plan(stale.iv()) == ()


def test_proposal_not_current_until_accepted(stale: Bench) -> None:
    rebuild_form(stale, ["a", "b"])
    card = propose(stale)
    current = stale.current("p.packet/C1")
    assert current is not None and current.ref == ref("p.packet/C1@v1")
    iv = stale.iv()
    assert effect_refusal(iv, ["p.packet/C1@v2"]) == TARGET_PENDING_REVIEW
    assert effect_refusal(iv, ["p.packet/C1@v1"]) == TARGET_INVALIDATED
    assert accept_review(stale.store, OWNER, "I-0001", card, stale.ids).accepted
    current = stale.current("p.packet/C1")
    assert current is not None and current.ref == ref("p.packet/C1@v2")
    assert effect_refusal(stale.iv(), ["p.packet/C1@v2"]) is None


def test_card_hash_mismatch_refused(stale: Bench) -> None:
    rebuild_form(stale, ["a", "b"])
    propose(stale)
    receipt = accept_review(stale.store, OWNER, "I-0001", "sha256:" + "0" * 64, stale.ids)
    assert receipt.refused == CARD_HASH_MISMATCH
    assert "review.accepted" not in stale.types()


def test_accept_requests_never_grants(stale: Bench) -> None:
    rebuild_form(stale, ["a", "b"])
    card = propose(stale)
    before = stale.types().count("approval.granted")
    assert accept_review(stale.store, OWNER, "I-0001", card, stale.ids).accepted
    assert stale.types().count("approval.granted") == before
    (req,) = [
        e for e in stale.events() if e.type == "approval.requested" and "replaces" in e.payload
    ]
    assert req.payload["targets"] == ("p.packet/C1@v2",) and req.payload["replaces"] == "A-0001"
    # ADR-P17 D7: the fresh request is a whole approval: a body, a new signature, its interrupt.
    from aksha.runtime.interrupts.approvals import approvals as approval_views
    from aksha.runtime.interrupts.reducers import interrupts

    views = approval_views(stale.view())
    fresh = views[str(req.payload["approval"])]
    assert fresh.state == "requested" and fresh.signature != views["A-0001"].signature
    assert fresh.body.targets[0][0] == "p.packet/C1@v2"
    assert interrupts(stale.view())[fresh.approval_id].kind == "approval"
    again = accept_review(stale.store, OWNER, "I-0001", card, stale.ids, command_id="other")
    assert again.refused == REVIEW_NOT_OPEN  # one decision per review


def test_accept_refused_when_proposal_stale(stale: Bench) -> None:
    """A change after the review opened voids it in the change's own transaction."""
    rebuild_form(stale, ["a", "b"])
    card = propose(stale)
    move(stale, 2)  # the form's record moves again: the revision read a stale form
    (voided,) = [e for e in stale.events() if e.type == "interrupt.voided"]
    assert voided.payload["reason"] == REVISION_INVALIDATED
    changed = [e for e in stale.events() if e.type == "source.changed"][-1]
    assert changed.seq < voided.seq
    receipt = accept_review(stale.store, OWNER, "I-0001", card, stale.ids)
    assert receipt.refused == REVIEW_NOT_OPEN
    current = stale.current("p.packet/C1")
    assert current is not None and current.ref == ref("p.packet/C1@v1")


def test_reject_blocks_the_writer_and_leaves_the_old_version_invalidated(stale: Bench) -> None:
    rebuild_form(stale, ["a", "b"])
    card = propose(stale)
    assert reject_review(stale.store, OWNER, "I-0001", card, stale.ids).accepted
    iv = stale.iv()
    assert iv.state("p.packet/C1@v1").freshness == "invalidated"
    assert "p.packet/C1@v1" in iv.rejected and rebuild_plan(iv) == ()
    assert iv.summary().blocked == ("p.packet/C1@v1",)
    assert effect_refusal(iv, ["p.packet/C1@v1"]) == TARGET_INVALIDATED


def test_reject_of_a_choice_raises_a_fresh_choice(tmp_path: Path) -> None:
    """S4: the person can choose again; the plan does not raise the same review again."""
    b = Bench(tmp_path)
    try:
        chooser = WriterId("p", "select", None)
        b.start("R-0001")
        b.save("R-0001", "p.table", {"order": [1]}, reads=[record("K")])
        b.save("R-0001", "p.selection", {"chosen": [1]}, reads=[a("p.table@v1")],
               writer=chooser, origin="choice")  # fmt: skip
        b.end("R-0001")
        move(b)
        b.rebuild_run("R-0002", ["R-0001"])
        b.save("R-0002", "p.table", {"order": [2]}, reads=[record("K", "2")])
        (entry,) = rebuild_plan(b.iv())
        assert entry.mode == "copy_choice"
        b.save("R-0002", "p.selection", {"chosen": [1]}, reads=[a("p.table@v2")],
               writer=chooser, origin="copy_choice", review="I-0001")  # fmt: skip
        card = open_reviews(b.view())["p.selection@v1"].card_hash
        assert reject_review(b.store, OWNER, "I-0001", card, b.ids).accepted
        choice = [e for e in b.events() if e.type == "interrupt.raised"][-1]
        assert (choice.payload["kind"], choice.payload["reason"]) == ("choice", "review_rejected")
        assert rebuild_plan(b.iv()) == ()
        # The person's new choice is a new version: the writer is unblocked and current.
        b.save("R-0002", "p.selection", {"chosen": [2]}, reads=[a("p.table@v2")],
               writer=chooser, origin="choice", instance="select-2")  # fmt: skip
        iv = b.iv()
        assert iv.rejected == frozenset() and iv.state("p.selection@v3").freshness == "current"
    finally:
        b.close()


def test_reviewed_equal_revision_still_reviewed(stale: Bench) -> None:
    """D5: an equal revision of an approved version still gets a review, led by the reads."""
    rebuild_form(stale, ["a", "b"])
    propose(stale, forms=1)
    raised = [e for e in stale.events() if e.type == "interrupt.raised"][-1]
    assert raised.payload["hunks"] == ()
    assert raised.payload["changed_reads"][0]["state"] == "moved"


def test_effects_refused_on_dirty_targets_and_the_outbox_authority(stale: Bench) -> None:
    iv = stale.iv()
    assert effect_refusal(iv, ["p.packet/C1@v1"]) == TARGET_DIRTY
    check = authority(lambda e: (str(e.request["document"]),))
    entry = OutboxEntry(
        key="k", owner=OWNER, tool="send", system="mailer", semantics="reconcile",
        on_unknown="interrupt", request={"document": "p.packet/C1@v1"},
        request_hash="sha256:" + "1" * 64, seq=1,
    )  # fmt: skip
    assert check(entry, stale.view()) == TARGET_DIRTY
    rebuild_form(stale, ["a"])  # equal: the packet is current again by cutoff
    assert check(entry, stale.view()) is None
