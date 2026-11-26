"""Routing is configuration (design note 3.0, ADR D12): bindings and routes, both pure.

**Bindings.** System 1 models are optional. :func:`resolve_bindings` takes what the
configuration binds and says who fills each contract:

- **Setup 1** (``s2_only``): no System 1 model. ``decide`` and ``extract`` resolve to the
  general model in adapted mode (``native=False``), and there is no routing step: every
  request goes straight to the System 2 model. Adapted is the binding here, not a
  fallback, so no ``allow_adapted`` switch is needed.
- **Setup 2** (``s1_decide``): a System 1 decision model fills ``decide``; ``extract``
  resolves to the general model, adapted.
- **Setup 3** (``s1_decide_extract``): System 1 models fill both.

A configuration with an extraction model and no decision model is ``s1_extract``: the
general model decides, adapted, and the System 1 model extracts. The answer-writing step
(``generate``) is the general model in every setup.

**Routes.** :func:`resolve_route` picks what one consuming step receives for one
document: an image goes natively, as a sent copy sized for the bound model's image
profile, when that model takes images; to a model with no image input it is the header
alone with the named gap ``no vision-capable model configured``. Every other type is its
text representation; native pass-through of PDF and Office files is designed only
(ING-6). Intake itself calls no model in any setup, so it is the same in all three.
"""

from __future__ import annotations

from collections.abc import Callable

from aksha.extensions.ingest.sniff import IMAGE_TYPES
from aksha.extensions.ingest.types import Binding, Document, ImageProfile, Route, SentImage, Setup

NO_VISION = "no vision-capable model configured"


def setup_of(decide: str | None, extract: str | None) -> Setup:
    """The setup a configuration is: ``s2_only``, ``s1_decide``, ``s1_decide_extract``.

    (``s1_extract`` for an extraction model with no decision model.)
    """
    if decide is None and extract is None:
        return "s2_only"
    if extract is None:
        return "s1_decide"
    if decide is None:
        return "s1_extract"
    return "s1_decide_extract"


def resolve_bindings(
    general: str, *, decide: str | None = None, extract: str | None = None
) -> tuple[Binding, ...]:
    """Who fills ``decide``, ``extract`` and ``generate`` (module doc, bindings).

    ``general`` is the general model's alias; ``decide`` and ``extract`` are the System 1
    aliases bound, or None when the configuration binds none.
    """
    routed = setup_of(decide, extract) != "s2_only"
    return (
        Binding("decide", decide or general, "classification", decide is None, routed),
        Binding("extract", extract or general, "extraction", extract is None, routed),
        Binding("generate", general, "generation", False, routed),
    )


def resolve_route(
    document: Document,
    profile: ImageProfile | None,
    *,
    prepare: Callable[[ImageProfile], SentImage] | None = None,
) -> Route:
    """The route of ``document`` to a step whose bound model has image ``profile``.

    ``profile`` is None when the bound model takes no images. ``prepare`` makes the sent
    copy for a profile (:func:`~aksha.extensions.ingest.profiles.prepare_image` over the
    document's canonical frame); without it an image route carries no copy yet.
    """
    if document.detected_type in IMAGE_TYPES:
        if profile is None:
            return Route("header_only", None, NO_VISION)
        return Route("native_image", None if prepare is None else prepare(profile), None)
    return Route("text")
