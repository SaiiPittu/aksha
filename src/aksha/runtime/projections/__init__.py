"""Projections (ADR-P01): read views built by a reducer over the event store.

No projection is ever the truth; the projection cache is a snapshot keyed by the last
sequence number, and a full rebuild must equal it.
"""
