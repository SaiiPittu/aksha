"""Web search adapters (ADR-P26 A3): a world's offline web, and a live search provider.

``world.py`` is a world's search proxy (and fetcher) over the pages its ``web.json``
declares, noise mixed in, deterministic for a query; ``brave.py`` is a live search API,
its key read from the environment. ``AKSHA_SEARCH`` picks one (``aksha.app``).
"""
