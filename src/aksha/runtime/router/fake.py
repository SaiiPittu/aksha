"""``PurposeClassifier``: the router's offline fake, scoring options by purpose words (D1).

The ``fake`` run mode's classifier for ``runtime.route``. It reads only the request: the
message (the state) and each option's purpose line (``question.option_texts``). An
option's score is the number of distinct message words found in its purpose line, after
case folding, dropping words under three letters and a short list of function words, and
a plural ``s`` on words over four letters. The distribution is each score plus
:data:`SMOOTHING`, normalised to parts per million, so a clear lead clears the thresholds
and a tie sits in the abstention band. No word in any purpose line is a refusal, which the
thresholds judge an abstention. The choice is the top option; a tie keeps declared order.
No network, no randomness: two runs write the same bytes.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import ClassVar, Literal

from aksha.runtime.models.fixed import distribution_ppm
from aksha.runtime.models.types import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ModelCapabilities,
    QuestionSpec,
)
from aksha.runtime.router.question import option_texts

SMOOTHING = Decimal("0.1")
"""Added to every option's score, so no option's probability is zero."""

FUNCTION_WORDS: frozenset[str] = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "who",
        "are",
        "what",
        "which",
        "this",
        "that",
        "from",
        "into",
        "about",
        "how",
        "can",
        "you",
        "your",
        "our",
        "its",
        "was",
        "were",
        "has",
        "have",
        "had",
        "not",
        "but",
        "all",
        "any",
        "some",
    ]
)

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> frozenset[str]:
    """The scored words of ``text`` (module doc)."""
    out: set[str] = set()
    for w in _WORD.findall(text.casefold()):
        if len(w) < 3 or w in FUNCTION_WORDS:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return frozenset(out)


class PurposeClassifier:
    """Classification by overlap between the message and each option's purpose line."""

    model_class: ClassVar[Literal["classification"]] = "classification"
    capabilities: ClassVar[ModelCapabilities] = ModelCapabilities(
        native=True, question_types=frozenset({"choice"}), returns_distribution=True, offline=True
    )

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One answer per question, from :meth:`answer`."""
        return ClassificationResult(
            tuple((q.name, self.answer(q, request.state)) for q in request.questions)
        )

    def answer(self, spec: QuestionSpec, state: str) -> ClassAnswer:
        """The scored answer to one choice question (module doc)."""
        said = words(state)
        texts = option_texts(spec)
        scores = {o: len(said & words(texts.get(o, ""))) for o in spec.options}
        if not any(scores.values()):
            return ClassAnswer(None)
        best = max(spec.options, key=lambda o: (scores[o], -spec.options.index(o)))
        weights = {o: Decimal(scores[o]) + SMOOTHING for o in spec.options}
        return ClassAnswer(best, distribution_ppm(weights))
