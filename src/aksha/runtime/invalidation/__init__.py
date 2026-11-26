"""Invalidation (ADR-P05): a change invalidates exactly the work whose lineage reaches it.

Early cutoff stops the cascade where rebuilt content is equal; machines rebuild silently
and people review a diff. Modules: ``types.py`` (values), ``ports.py`` (``VersionedSource``,
``QueryRunner``), ``sources.py`` (the one writer of ``source.changed`` and
``query.reexecuted``), ``view.py`` (the invalidation view, the sibling rule), ``plan.py``
(the rebuild plan and F2), ``rebuild.py`` (rebuild runs and the rebuild brief),
``revision.py`` and ``redline.py`` (pending revisions and their diff reviews),
``reviews.py`` (reviews and approvals read from the log), ``confirm.py`` (revocations and
review voids in the confirming transaction), ``effects.py`` (effects refused on stale
targets) and ``commands.py`` (``apply_source_change``, ``accept_review``,
``reject_review`` and recovery's source check), ``modes.py`` (the four modes an agent
opts into, decision 38) and ``guard.py`` (the edit-time check of ``edit_guard`` and up).
"""
