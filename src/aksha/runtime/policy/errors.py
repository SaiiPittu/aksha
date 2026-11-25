"""Policy refusals at declaration time: ``RuleInvalid``.

The gate itself never raises (it answers ``Refuse``); these are refusals of a rule set
before any call is checked, raised when a ``Permissions`` value is built. Codes:
``rule_root_invalid`` (a path root that is not in normal form, absolute or escaping) and
``rule_role_invalid`` (a recipient rule that does not use ``id_in_source``).
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class RuleInvalid(AkshaError):
    """An argument rule that cannot be checked as written."""

    default_code = "rule_invalid"
