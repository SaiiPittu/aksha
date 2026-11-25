"""The tool registry and schemas (ADR-P10): every tool is declared data with one effect class.

The schema the model sees is the schema the policy gate checks. ``types`` holds the values
(``ToolSpec``, ``ToolDecl``, ``ToolOutcome`` and the views); ``registry`` builds the frozen
per-run ``ToolRegistry``; ``checks`` holds the loader's refusals of an unsafe registry;
``arguments`` validates arguments and lists every failing path; ``bodies`` calls a body
and turns its failures into results; ``loader_added`` declares ``finish``, ``ask`` and
``save_artifact``; ``results`` renders the capped model view of a result.
"""
