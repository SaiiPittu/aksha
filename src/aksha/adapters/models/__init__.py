"""Model adapters (ADR-P07): scripted and replay adapters for every class, and a live one.

``scripted.py`` gives fixed replies for unit tests; ``replay.py`` answers from an earlier
run's recorded steps, keyed by the run-independent step key; ``live.py`` reaches a
chat-completions HTTP server and is imported only in ``live`` mode, never by an offline
run. The rule-based fakes agents extend are in :mod:`aksha.runtime.models.rules`, because
an agent imports only the extension API.
"""
