"""Agent definitions and the extension machinery (ADR-P20): load, check and hash agents.

``types`` holds the declared values (``Agent``, ``Extension``, ``Projection``); ``loader``
finds, imports and checks them in one fixed order (``validate`` and ``tree_checks`` hold
the pure checks, ``modules`` the import and source scan, ``names`` the namespacing,
``versions`` the API version); ``hashing`` computes the agent definition version hash;
``surface`` pins the public API for its snapshot test.

This package is the machinery that reads definitions. The extensions themselves live in
:mod:`aksha.extensions`, the agents in :mod:`aksha.agents`, and both reach the runtime
only through :mod:`aksha.runtime.api`.
"""
