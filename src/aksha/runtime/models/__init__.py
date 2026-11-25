"""The model interface and routing by model class (ADR-P07).

Three model classes the runtime owns (extraction, classification, generation), each
bound through a model alias. Built: ``types.py`` (``ModelClass``, ``ModelAlias``,
``ModelAdapterSpec``). Planned: the model ports and alias routing.
"""
