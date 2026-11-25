"""The event model of the event store (ADR-P01): what one recorded fact looks like.

Holds the event envelope, the closed catalogue of event types with their JSON Schemas,
and the closed list of transaction boundaries. Storage itself is behind the ports in
:mod:`aksha.runtime.storage`; this package never touches a database.
"""
