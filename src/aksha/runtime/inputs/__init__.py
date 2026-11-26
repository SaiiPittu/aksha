"""Steering and follow-up input (ADR-P27): a person's message to an open run, in a lane.

Built: ``types.py`` (the three lanes, ``QueuedInput``), ``reducers.py`` (pending input as
a projection of ``input.queued`` and ``input.consumed``) and ``commands.py`` (``steer``,
the ``Steer`` command's facts). The context renders a pending steer before its target's
next model call (``context/assembly.py``); stop-policy evaluation turns a finish into
CONTINUE while a follow-up or a steer is pending (``stop_policy/core.py``); an amendment
supersedes the answered statement (``interrupts/amend.py``). Planned: a steer delivered
inside a running tool call; the inputs of a resumed re-entry's earlier session.

Uploaded documents (the ingest extension, ADR-P31 draft): ``ports.py`` (``DocumentIngester``,
the port an upload's bytes go through), ``uploads.py`` (``UploadInputs`` text and base64
binary as one map) and ``known_documents`` in ``reducers.py`` (each input path's latest
``document.stored``). The session's upload writes what the ingester returns and commits its
``document.*`` facts with the source change.
"""
