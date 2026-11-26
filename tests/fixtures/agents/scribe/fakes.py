"""The scribe's fake model: a rule over the request and the tool results it was shown."""

from __future__ import annotations

from aksha.runtime.api import GenerationRequest, GenerationResult, RuleGenerationModel, ToolCall


def _call(name: str, n: int, **args: object) -> ToolCall:
    return ToolCall(f"call-{name}-{n}", name, dict(args))


class ScribePolicy(RuleGenerationModel):
    """Ask first when the request says ``ask``; publish when it says ``publish``; then save.

    A publish that waited for approval is called again once, after the approval.
    """

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The next reply, read from the messages alone."""
        said = " ".join(m.text or "" for m in request.messages if m.role == "user").lower()
        results = [m.text or "" for m in request.messages if m.role == "tool"]
        turn = sum(1 for m in request.messages if m.role == "assistant") + 1
        save = _call("save_artifact", turn, kind="note", content={"text": "noted"})
        finish = _call("finish", turn, summary="done")
        if "ask" in said and not any("answer" in r for r in results) and turn == 1:
            return GenerationResult(None, (_call("ask", turn, question="Which detail?"),),
                                    "tool_calls")  # fmt: skip
        published = sum(1 for m in request.messages for c in m.tool_calls if c.name == "publish")
        waited = any('"status":"waiting"' in r for r in results)
        if "publish" in said and (published == 0 or (waited and published == 1)):
            return GenerationResult(None, (_call("publish", 1, text="noted"),), "tool_calls")
        return GenerationResult(None, (save, finish), "tool_calls")


ADAPTERS = {"generate": ScribePolicy()}
