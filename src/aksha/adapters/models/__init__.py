"""Model adapters (ADR-P07): scripted and replay adapters for every class, and live ones.

``scripted.py`` gives fixed replies for unit tests; ``replay.py`` answers from an earlier
run's recorded steps, keyed by the run-independent step key; ``litellm.py`` reaches any
provider through LiteLLM (the default live provider) and ``live.py`` a chat-completions HTTP
server with the standard library only (the fallback). Both are imported only in ``live``
mode, never by an offline run. The rule-based fakes agents extend are in
:mod:`aksha.runtime.models.rules`, because an agent imports only the extension API.
"""
