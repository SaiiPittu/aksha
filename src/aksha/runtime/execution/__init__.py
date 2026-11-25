"""Durable execution (ADR-P02): resume and replay.

Each step is journalled under a deterministic step ID, so a restarted process returns
recorded results instead of re-running them. Built: ``ports.py`` (``Clock``) and
``types.py`` (``StepSemantics``). Planned: the durable step executor, which appends each
gate decision (``policy/events.py``), recovery on startup and the replay check.
"""
