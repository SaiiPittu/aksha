"""The model interface and routing by model class (ADR-P07).

Three model classes the runtime owns, each a first-class contract: extraction and
classification (the fast classes, System 1, narrow and thresholdable) and generation (the
slow class, System 2). An agent declares model aliases; the run mode binds each to one
adapter of the same class, checked against its capability record.

Built: the values (``types.py``), the three Protocols (``ports.py``), fixed-point numbers
(``fixed.py``), thresholds and the abstention band (``thresholds.py``), the loader's alias
refusals (``checks.py``), binding and the cascade rules (``routing.py``), model calls as
durable steps (``steps.py``), options from a saved artifact (``options.py``), the adapted
classifier (``adapted.py``), the rule-based fakes (``rules.py``) and the recorded model
steps of a run (``replay.py``). Planned: generation fallback, the run inspector's list
of unfitted thresholds (P21), the fast-model verifier (P13 D6) and the router alias (P23).
"""
