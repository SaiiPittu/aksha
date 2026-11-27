"""Accounts of the HTTP server: orgs, users, roles, sessions, invites, API tokens, the audit log.

``store.py`` is the SQLite database (``<home>/accounts.sqlite``, ``AKSHA_ACCOUNTS_PATH``),
``credentials.py`` the argon2id password hashing and the random secrets kept as digests,
``service.py`` the rules (who may sign up, sign in, invite, change a role), and
``errors.py`` the one refusal they raise. The runtime never imports any of it: the server
maps each signed-in request to a tenant string, the runtime owner, and passes that on.
"""
