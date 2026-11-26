"""Record and replay: every model answer, search and fetch kept, and answered again exactly.

A replay never invents: a generation step it has no recording of is ``replay_missing``,
one asked something else ``replay_mismatch``, a fast-class request never asked
``replay_missing``, and so is a search or a fetch the run never made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha import data
from aksha.adapters.models.scripted import (
    ScriptedClassificationModel,
    ScriptedExtractionModel,
    ScriptedGenerationModel,
)
from aksha.adapters.recording.models import (
    Recorded,
    RecordedGeneration,
    recorded_adapters,
    recording_adapters,
)
from aksha.adapters.recording.sink import JsonlSink, read_stream
from aksha.adapters.recording.web import RecordingFetch, RecordingSearch, ReplayWeb
from aksha.adapters.search.world import WorldWeb
from aksha.runtime.egress.client import RefusingHttpClient
from aksha.runtime.egress.errors import EgressRefused
from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.models.errors import PermanentModelError
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelAlias,
    QuestionSpec,
    ToolCall,
)
from aksha.runtime.web.types import FetchedPage, SearchHit, SearchResults, WebError

STEP = "R-0001/scope/t01"
ASK = GenerationRequest(STEP, "generate", (Message("user", "Who sells probes?"),))
REPLY = GenerationResult(None, (ToolCall("c1", "finish", {"summary": "done"}),), "tool_calls")


def test_a_generation_is_recorded_and_answered_again_by_step_and_hash(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path)
    wrapped = recording_adapters({"generate": ScriptedGenerationModel([REPLY])}, sink)["generate"]
    assert wrapped.generate(ASK).tool_calls == REPLY.tool_calls  # type: ignore[attr-defined]
    (line,) = read_stream(tmp_path, "models")
    assert (line["class"], line["key"], line["hash"]) == ("generation", "scope/t01",
                                                           ASK.context_hash)  # fmt: skip
    recorded = Recorded(read_stream(tmp_path, "models"))
    again = RecordedGeneration("generate", recorded)
    # a replay has a new run ID but walks the same step keys
    replayed = again.generate(GenerationRequest("R-0009/scope/t01", "generate", ASK.messages))
    assert replayed.tool_calls == REPLY.tool_calls and replayed.usage.input_tokens == 0


def test_a_replay_refuses_a_step_it_has_no_answer_for_or_one_asked_otherwise(
    tmp_path: Path,
) -> None:
    sink = JsonlSink(tmp_path)
    recording_adapters({"generate": ScriptedGenerationModel([REPLY])}, sink)["generate"].generate(  # type: ignore[attr-defined]
        ASK
    )
    again = RecordedGeneration("generate", Recorded(read_stream(tmp_path, "models")))
    with pytest.raises(PermanentModelError) as e:
        again.generate(GenerationRequest("R-0001/scope/t02", "generate", ASK.messages))
    assert e.value.code == "replay_missing"
    with pytest.raises(PermanentModelError) as e:
        again.generate(GenerationRequest(STEP, "generate", (Message("user", "Other?"),)))
    assert e.value.code == "replay_mismatch"


def test_fast_class_calls_are_answered_by_alias_and_request_hash(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path)
    question = QuestionSpec("kind", "What kind?", "choice", ("a", "b"))
    ext = ExtractionRequest("fetch_page/page_facts", "page_facts", (), (FieldSpec("company"),))
    cls = ClassificationRequest("fetch_page/doc_kind", "doc_kind", "state", (question,))
    extract = ScriptedExtractionModel([ExtractionResult((("company", FieldAnswer(None)),))])
    classify = ScriptedClassificationModel(
        [ClassificationResult((("kind", ClassAnswer("a", (("a", 1_000_000), ("b", 0)))),))]
    )
    wrapped = recording_adapters({"page_facts": extract, "doc_kind": classify}, sink)
    first_e = wrapped["page_facts"].extract(ext)  # type: ignore[attr-defined]
    first_c = wrapped["doc_kind"].classify(cls)  # type: ignore[attr-defined]
    recorded = Recorded(read_stream(tmp_path, "models"))
    aliases = (
        ModelAlias("page_facts", "extraction", fields=(FieldSpec("company"),)),
        ModelAlias("doc_kind", "classification", questions=(question,)),
    )
    replay = recorded_adapters(aliases, recorded)
    assert replay["page_facts"].extract(ext).answers == first_e.answers  # type: ignore[attr-defined]
    assert replay["doc_kind"].classify(cls).answers == first_c.answers  # type: ignore[attr-defined]
    assert replay["doc_kind"].capabilities.offline  # type: ignore[attr-defined]
    other = ClassificationRequest("fetch_page/doc_kind", "doc_kind", "another state", (question,))
    with pytest.raises(PermanentModelError) as e:
        replay["doc_kind"].classify(other)  # type: ignore[attr-defined]
    assert e.value.code == "replay_missing"


def test_searches_and_fetches_are_recorded_and_answered_again(tmp_path: Path) -> None:
    world = WorldWeb.of_world(data.worlds() / "stillroom")
    sink = JsonlSink(tmp_path)
    search, fetch = RecordingSearch(world, sink), RecordingFetch(world, sink)
    found = search.search("occupancy sensor", limit=5)
    page = fetch.fetch(found.hits[0].url, RefusingHttpClient())
    with pytest.raises(WebError):
        fetch.fetch("https://kelvra.example/nothing", RefusingHttpClient())
    assert sink.counts == {"search": 1, "fetch": 2}
    replay = ReplayWeb(read_stream(tmp_path, "search"), read_stream(tmp_path, "fetch"))
    assert replay.search("occupancy sensor", limit=5) == found
    assert replay.fetch(found.hits[0].url, RefusingHttpClient()) == page
    with pytest.raises(WebError) as e:
        replay.fetch("https://kelvra.example/nothing", RefusingHttpClient())
    assert e.value.code == "not_found"  # the recorded refusal, not a guess
    elsewhere = "https://veyra.example/elsewhere"
    asks = (
        lambda: replay.search("occupancy sensor", limit=6),
        lambda: replay.fetch(elsewhere, RefusingHttpClient()),
    )
    for ask in asks:
        with pytest.raises(WebError) as e:
            ask()
        assert e.value.code == "replay_missing"


def test_a_sink_appends_one_sorted_line_per_answer(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "rec")
    sink.append("search", {"b": 1, "a": 2})
    sink.append("search", {"c": 3})
    assert (tmp_path / "rec" / "search.jsonl").read_text().splitlines() == [
        '{"a":2,"b":1}', '{"c":3}',
    ]  # fmt: skip
    with pytest.raises(ValueError):
        sink.append("secrets", {})
    assert list(read_stream(tmp_path / "rec", "fetch")) == []


class Flaky:
    """A web that changes between two fetches and fails a search once, then answers."""

    name = "flaky"

    def __init__(self) -> None:
        self.fetched = 0
        self.searched = 0

    def search(self, query: str, *, limit: int) -> SearchResults:
        self.searched += 1
        if self.searched == 1:
            raise TransientStepError("transient", "the provider answered 503")
        return SearchResults(query, self.name, (SearchHit(1, "P", "https://a.example/p", "s"),))

    def fetch(self, url: str, http: HttpClient) -> FetchedPage:
        if "private" in url:
            raise EgressRefused("10.0.0.1", "private_address", url)
        self.fetched += 1
        return FetchedPage(url, url, 200, "text/plain", f"version {self.fetched}")


def test_answers_replay_in_the_order_the_run_got_them(tmp_path: Path) -> None:
    inner, sink, http = Flaky(), JsonlSink(tmp_path), RefusingHttpClient()
    search, fetch = RecordingSearch(inner, sink), RecordingFetch(inner, sink)
    with pytest.raises(TransientStepError):
        search.search("p", limit=3)
    found = search.search("p", limit=3)
    first, second = (fetch.fetch("https://a.example/p", http) for _ in range(2))
    with pytest.raises(EgressRefused):
        fetch.fetch("https://private.example/x", http)
    assert first.text != second.text
    replay = ReplayWeb(read_stream(tmp_path, "search"), read_stream(tmp_path, "fetch"))
    with pytest.raises(TransientStepError):  # retried as the run retried it
        replay.search("p", limit=3)
    assert replay.search("p", limit=3) == found
    assert [replay.fetch("https://a.example/p", http).text for _ in range(3)] == [
        "version 1", "version 2", "version 2",
    ]  # fmt: skip
    with pytest.raises(EgressRefused) as refused:  # the same refusal the body turns into Err
        replay.fetch("https://private.example/x", http)
    assert (refused.value.host, refused.value.reason) == ("10.0.0.1", "private_address")
