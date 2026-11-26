"""The ``run.started`` a route opens records the agent's re-entry setting (decision 36)
and its invalidation mode (decision 38)."""

from __future__ import annotations

import pytest

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, RUN_STARTED, Catalogue
from aksha.runtime.router.facts import run_opened
from aksha.runtime.router.types import RoutableAgent


@pytest.mark.parametrize(("setting", "recorded"), [(None, "fresh"), ("fresh", "fresh"),
                                                   ("resume", "resume")])  # fmt: skip
def test_a_request_run_records_the_agents_setting(setting: str | None, recorded: str) -> None:
    agent = RoutableAgent("demo", "Build bundles.", reentry=setting)  # type: ignore[arg-type]
    event = run_opened("S-0001", "R-0001", agent)
    assert event.payload["reentry"] == recorded
    Catalogue(CORE_EVENT_TYPES).validate_payload(RUN_STARTED.name, dict(event.payload))


def test_an_unknown_setting_is_refused() -> None:
    with pytest.raises(ValueError):
        RoutableAgent("demo", "Build bundles.", reentry="fork")  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["off", "edit_guard", "notify", "rebuild"])
def test_a_routed_run_records_the_agents_invalidation_mode(mode: str) -> None:
    """Without the field the run would read as ``rebuild``, the pre-decision-38 meaning."""
    agent = RoutableAgent("demo", "Build bundles.", invalidation=mode)  # type: ignore[arg-type]
    event = run_opened("S-0001", "R-0001", agent)
    assert event.payload["invalidation"] == mode
    Catalogue(CORE_EVENT_TYPES).validate_payload(RUN_STARTED.name, dict(event.payload))
