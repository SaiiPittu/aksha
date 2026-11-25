"""The policy gate and permissions (ADR-P11): deny by default, one checker in code.

``gate.check`` is the one pure checker; ``types`` holds rules, permissions and the two
answers; ``rules`` and ``paths`` the closed set of argument matchers; ``permissions`` the
tool sets and routes; ``matrix`` the printed permission matrix; ``events`` the one event a
decision becomes. There is no mode that skips the gate, nothing the model can write is
an input to it, and the gate never appends: the durable step executor records each
decision with its reason.
"""
