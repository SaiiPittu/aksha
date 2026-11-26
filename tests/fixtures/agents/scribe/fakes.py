"""The scribe's fake model: a rule over the request and the tool results it was shown."""

from __future__ import annotations

import json

from aksha.runtime.api import GenerationRequest, GenerationResult, RuleGenerationModel, ToolCall


def _call(name: str, n: int, **args: object) -> ToolCall:
    return ToolCall(f"call-{name}-{n}", name, dict(args))


def _status(text: str) -> str:
    """The ``status`` of a tool result as the model was shown it (its first JSON line)."""
    try:
        shown = json.loads(text.split("\n", 1)[0])
    except ValueError:
        return "unreadable"
    if not isinstance(shown, dict):
        return "unreadable"
    data = shown.get("data")
    if isinstance(data, dict) and data.get("status") == "waiting":
        return "waiting"
    return str(shown.get("status", "unreadable"))


def _note_text(said: str) -> str:
    """The note's text: what follows the last ``note text:`` the person wrote, else ``noted``."""
    text = "noted"
    marker = "note text:"
    for line in said.splitlines():
        at = line.lower().rfind(marker)
        rest = line[at + len(marker) :].strip() if at >= 0 else ""
        if rest:
            text = rest
    return text


class ScribePolicy(RuleGenerationModel):
    """Ask first when the request says ``ask``; publish when it says ``publish``; then save.

    The note says ``noted``, unless a person's message holds a ``note text: ...`` line
    (a steer, a follow-up or an amended answer): then the last such line.

    The publish's result decides what follows, as a model would read it: ``waiting`` (the
    approval is pending) calls it again once approved; ``ok`` saves the note; an error
    finishes without saving, so the run cannot complete on a delivery it was told failed.
    """

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The next reply, read from the messages alone."""
        raw = "\n".join(m.text or "" for m in request.messages if m.role == "user")
        said = raw.lower()
        results = {m.call_id: m.text or "" for m in request.messages if m.role == "tool"}
        turn = sum(1 for m in request.messages if m.role == "assistant") + 1
        save = _call("save_artifact", turn, kind="note", content={"text": _note_text(raw)})
        finish = _call("finish", turn, summary="done")
        answered = "answers from the person" in said or any("answer" in r for r in results.values())
        if "ask" in said and not answered and turn == 1:
            return GenerationResult(None, (_call("ask", turn, question="Which detail?"),),
                                    "tool_calls")  # fmt: skip
        publishes = [c for m in request.messages for c in m.tool_calls if c.name == "publish"]
        if "publish" in said:
            last = _status(results.get(publishes[-1].call_id, "")) if publishes else "waiting"
            if last == "waiting":
                return GenerationResult(None, (_call("publish", turn, text="noted"),),
                                        "tool_calls")  # fmt: skip
            if last != "ok":
                failed = _call("finish", turn, summary=f"publish failed: {last}")
                return GenerationResult(None, (failed,), "tool_calls")
        return GenerationResult(None, (save, finish), "tool_calls")


ADAPTERS = {"generate": ScribePolicy()}
