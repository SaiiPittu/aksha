"""The Aksha runtime: the agent-neutral domain every agent runs on.

One package per primitive, each laid out the same way: ``types.py`` for the values it
declares, ``ports.py`` for the Protocols it needs from outside (implemented by adapters in
:mod:`aksha.adapters`), and one module per piece of behaviour. A package holds only what
is built; what is planned is listed in ``docs/architecture/codebase-structure.md``.
``api.py`` is the extension API, ``canonical.py`` and ``errors.py`` are shared by all.

The runtime imports the standard library and ``jsonschema`` only, and never imports an
adapter, an agent, an extension or an interface. Agents and extensions import only
:mod:`aksha.runtime.api`. ``tests/architecture`` enforces all three rules.
"""
