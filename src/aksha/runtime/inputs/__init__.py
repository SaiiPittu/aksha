"""Steering and follow-up input (ADR-P27): a person's message to an open run, in a lane.

Built: ``types.py`` (the three lanes, ``QueuedInput``), ``reducers.py`` (pending input as
a projection of ``input.queued`` and ``input.consumed``) and ``commands.py`` (``steer``,
the ``Steer`` command's facts). The context renders a pending steer before its target's
next model call (``context/assembly.py``); stop-policy evaluation turns a finish into
CONTINUE while a follow-up or a steer is pending (``stop_policy/core.py``); an amendment
supersedes the answered statement (``interrupts/amend.py``). Planned: a steer delivered
inside a running tool call; the inputs of a resumed re-entry's earlier session.
"""
