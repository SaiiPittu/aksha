"""Build a projection: a full ``rebuild``, an incremental ``apply_events``, and ``project``.

``project`` always goes through the projection cache, which the runtime owns and passes
in. On a miss it rebuilds from sequence 0; with ``incremental=True`` it instead applies
only the events after an older cached state (ADR-P01 D3, switched on by measurement per
S2). A view that is not complete (``LogView.complete``) is rebuilt and never cached.
Both paths show each event the artifact rows committed up to that event, so they agree
by construction, and ``test_incremental_equals_full_rebuild`` checks it.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable

from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.cache import ProjectionCache
from aksha.runtime.projections.ports import Reducer
from aksha.runtime.projections.view import ArtifactsView, LogView


def apply_events[S](
    reducer: Reducer[S], state: S, events: Iterable[Event], artifacts: ArtifactsView
) -> S:
    """Apply ``events`` in order to ``state``, skipping types outside ``reducer.reads``.

    ``artifacts`` must cover the last event; each event sees it narrowed to its own seq.
    """
    for event in events:
        if event.type in reducer.reads:
            state = reducer.apply(state, event, artifacts.upto(event.seq))
    return state


def rebuild[S](reducer: Reducer[S], view: LogView, args: Hashable = ()) -> S:
    """A full rebuild: the reducer's initial state with every event of ``view`` applied."""
    return apply_events(reducer, reducer.initial(args), view.events, view.artifacts)


def project[S](
    reducer: Reducer[S],
    view: LogView,
    args: Hashable = (),
    *,
    cache: ProjectionCache,
    incremental: bool = False,
) -> S:
    """The projection of ``view`` through ``cache``.

    A hit returns the cached state. A miss rebuilds, or, with ``incremental``, continues
    from the newest older cached state. The result is cached at ``view.seq_read``. A view
    that is not complete bypasses the cache both ways, so a filtered read at the same
    sequence can never be served the full view's state (or poison it).
    """
    if not view.complete:
        return rebuild(reducer, view, args)
    found, hit = cache.get(view.owner, reducer.name, args, view.seq_read)
    if found:
        state: S = hit
        return state
    older = (
        cache.latest_before(view.owner, reducer.name, args, view.seq_read) if incremental else None
    )
    if older is not None:
        seq, cached = older
        start: S = cached
        later = (e for e in view.events if e.seq > seq)
        result: S = apply_events(reducer, start, later, view.artifacts)
    else:
        result = rebuild(reducer, view, args)
    cache.put(view.owner, reducer.name, args, view.seq_read, result)
    return result
