"""Storage ports of the event store (ADR-P01): the interfaces every storage adapter implements.

The runtime depends on these Protocols only. The SQLite and PostgreSQL adapters in
:mod:`aksha.adapters.storage` implement them, and one contract suite in
``tests/contract/storage/`` runs against both.
"""
