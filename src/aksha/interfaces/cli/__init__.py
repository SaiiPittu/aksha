"""The command line: ``aksha`` in server mode or embedded mode (decision 22, ADR-P19).

``main.py`` is the parser and the commands, ``client.py`` the stdlib HTTP client of a
running server, ``pending.py`` the pending command IDs a retry reuses, and ``evals.py``
``aksha eval`` (ADR-P22), which runs offline with its own exit codes.
"""
