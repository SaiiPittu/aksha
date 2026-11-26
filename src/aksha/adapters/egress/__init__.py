"""Egress adapters (ADR-P26): the resolver and transport under the runtime's HTTP client.

``network.py`` resolves with the operating system and sends one request to one address the
runtime's client already checked, with TLS verified against the URL's host name.
"""
