"""Who a request acts for: the principal dependency and the role checks every route uses.

A request resolves to one :class:`Principal`: the runtime ``tenant`` it reads and writes,
its ``role`` and how it was authenticated (``via``):

- ``header``: the app trusts ``X-Aksha-User`` (``AuthConfig(trust_user_header=True)``,
  ``aksha serve --trust-user-header`` and the session API tests); the role is Owner.
- ``bearer``: ``Authorization: Bearer aksha_pat_...``, an API token of an active member.
- ``cookie``: the ``aksha_session`` cookie of a signed-in browser. An unsafe request
  (POST, PUT, PATCH, DELETE) must also send ``X-CSRF-Token`` equal to the session's token.
- ``local``: while the server has no account, a loopback client on a loopback host name
  acts as the Owner of tenant ``local`` (or of the CLI's ``X-Aksha-User``, which it has
  always sent). An unsafe request must send ``X-CSRF-Token`` (from ``/auth/me``) or the
  CLI's header; a request from elsewhere is refused ``setup_required``.

Routes take one of the annotated dependencies below. ``ReadTenant`` and ``StopTenant``
take ``?as_user=<user id>``: an Admin or the Owner may read (or stop the run of) a member
of their org, and an audit record is written before the read or the stop. Every other
command refuses ``as_user`` (``as_user_refused``): nobody answers, approves, steers or
sends on another member's behalf. A Viewer's command is refused ``forbidden_role``.

A command's ``by`` is set from the principal (:class:`ByRule`, ADR-P19 B19): ``user:<id>``
or ``local``, so the event store records who acted; with the trusted header it is kept.
"""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass, field, replace
from typing import Annotated

from fastapi import Depends, Request

from aksha.interfaces.http.accounts.errors import AccountError
from aksha.interfaces.http.accounts.service import LOCAL_TENANT, RANK, Accounts
from aksha.interfaces.http.accounts.store import AuthSession, Member
from aksha.interfaces.http.common import UNSAFE
from aksha.interfaces.wire import WireInvalid
from aksha.runtime.session.named import DEFAULT_BY, DecideCorrection, NamedCommand

__all__ = [
    "COOKIE",
    "AccountError",
    "AccountHolder",
    "AdminPrincipal",
    "AuthConfig",
    "ByRule",
    "Commander",
    "Principal",
    "ReadTenant",
    "SignedIn",
    "StopTenant",
    "accounts_of",
    "client_ip",
    "command_principal",
    "current_principal",
    "is_local",
    "principal_by",
    "read_tenant",
    "signed_in",
]

COOKIE = "aksha_session"


@dataclass(frozen=True)
class AuthConfig:
    """How the app authenticates: a trusted header, or accounts (with local mode).

    ``accounts`` is required unless ``trust_user_header``; ``guests`` turns on the guest
    sandbox (``AKSHA_GUESTS=1``); ``cookie_secure`` marks the cookie Secure even over
    plain HTTP behind a TLS proxy (``AKSHA_COOKIE_SECURE=1``).
    """

    accounts: Accounts | None = None
    trust_user_header: bool = False
    guests: bool = False
    cookie_secure: bool = False

    def check(self) -> None:
        """Refuse a configuration with neither a trusted header nor an accounts store."""
        if not self.trust_user_header and self.accounts is None:
            raise ValueError("real authentication needs an accounts store (AuthConfig.accounts)")

    def close(self) -> None:
        """Close the accounts store, if any."""
        if self.accounts is not None:
            self.accounts.close()


@dataclass(frozen=True)
class AuthState:
    """The app's auth configuration and its local-mode CSRF token (``app.state.auth``)."""

    config: AuthConfig
    local_csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)


@dataclass(frozen=True)
class Principal:
    """Who a request acts for: the runtime tenant, the role and how they signed in.

    ``member`` is the account (None for ``header`` and ``local``); ``session`` the cookie
    session (``cookie`` only); ``csrf`` the token unsafe cookie or local requests send.
    """

    tenant: str
    role: str
    via: str
    member: Member | None = None
    session: AuthSession | None = None
    csrf: str | None = field(default=None, repr=False)

    @property
    def user_id(self) -> str | None:
        """The account's user ID, or None without an account."""
        return None if self.member is None else self.member.user.id

    @property
    def org_id(self) -> str | None:
        """The account's org, or None without an account."""
        return None if self.member is None else self.member.org_id

    @property
    def is_guest(self) -> bool:
        """Whether the account is a guest's."""
        return self.member is not None and self.member.user.is_guest

    def at_least(self, role: str) -> bool:
        """Whether this principal's role is ``role`` or above it."""
        return RANK[self.role] >= RANK[role]


def auth_state(request: Request) -> AuthState:
    """The app's :class:`AuthState`."""
    state: AuthState = request.app.state.auth
    return state


def accounts_of(request: Request) -> Accounts:
    """The app's accounts; ``accounts_off`` when it trusts the header instead."""
    accounts = auth_state(request).config.accounts
    if accounts is None or auth_state(request).config.trust_user_header:
        raise AccountError(404, "accounts_off", "this server trusts X-Aksha-User; no accounts")
    return accounts


def client_ip(request: Request) -> str | None:
    """The client's address, as the audit log records it."""
    return None if request.client is None else request.client.host


def _loopback_address(text: str | None) -> bool:
    if not text:
        return False
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def is_local(request: Request) -> bool:
    """A loopback client that asked for a loopback host name (no rebound DNS name)."""
    if not _loopback_address(client_ip(request)):
        return False
    host = (request.url.hostname or "").lower()
    return host == "localhost" or host.endswith(".localhost") or _loopback_address(host)


def _same(given: str | None, expected: str | None) -> bool:
    return bool(given) and bool(expected) and secrets.compare_digest(str(given), str(expected))


def _csrf_refused() -> AccountError:
    return AccountError(403, "csrf_failed", "send the X-CSRF-Token header of this session")


def current_principal(request: Request) -> Principal:
    """The principal of ``request``, CSRF checked; refuses ``unauthenticated`` otherwise."""
    state = auth_state(request)
    header = request.headers.get("x-aksha-user")
    if state.config.trust_user_header:
        if not header:
            raise AccountError(422, "validation", "the X-Aksha-User header names the owner")
        return Principal(header, "owner", "header")
    accounts = state.config.accounts
    assert accounts is not None
    unsafe = request.method in UNSAFE
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, secret = authorization.partition(" ")
        resolved = accounts.resolve_token(secret.strip()) if scheme.lower() == "bearer" else None
        if resolved is None:
            raise AccountError(401, "unauthenticated", "the API token is not valid")
        m = resolved.member
        return Principal(m.user.tenant, m.role, "bearer", m)
    cookie = request.cookies.get(COOKIE)
    if cookie:
        found = accounts.resolve_session(cookie)
        if found is not None and found.session is not None:
            if unsafe and not _same(request.headers.get("x-csrf-token"), found.session.csrf):
                raise _csrf_refused()
            m = found.member
            return Principal(m.user.tenant, m.role, "cookie", m, found.session,
                             found.session.csrf)  # fmt: skip
    if not accounts.has_accounts():
        if not is_local(request):
            raise AccountError(
                401, "setup_required", "the server has no account yet: claim it from this machine"
            )
        if unsafe and not header and not _same(request.headers.get("x-csrf-token"),
                                               state.local_csrf):  # fmt: skip
            raise _csrf_refused()
        return Principal(header or LOCAL_TENANT, "owner", "local", csrf=state.local_csrf)
    raise AccountError(401, "unauthenticated", "sign in first")


def signed_in(request: Request) -> None:
    """Any principal (an open route when the app trusts the header and none is sent)."""
    if not auth_state(request).config.trust_user_header:
        current_principal(request)


def account_principal(who: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """A principal with an account; ``no_account`` in local and header modes."""
    if who.member is None:
        raise AccountError(409, "no_account", "this needs an account: sign up or claim first")
    return who


def admin_principal(who: Annotated[Principal, Depends(account_principal)]) -> Principal:
    """An Admin or the Owner, with an account."""
    if not who.at_least("admin"):
        raise AccountError(403, "forbidden_role", "only an admin or the owner may do this")
    return who


def command_principal(
    who: Annotated[Principal, Depends(current_principal)], as_user: str | None = None
) -> Principal:
    """A principal that may command: Member or above, acting only for itself."""
    if not who.at_least("member"):
        raise AccountError(403, "forbidden_role", "a viewer reads only")
    if as_user is not None:
        raise AccountError(403, "as_user_refused", "a command acts only for its sender")
    return who


def command_tenant(who: Annotated[Principal, Depends(command_principal)]) -> str:
    """The tenant a write acts in: always the sender's own (Viewers and ``as_user`` refused)."""
    return who.tenant


def _tenant_as(request: Request, who: Principal, as_user: str | None, action: str,
               detail: dict[str, str]) -> str:  # fmt: skip
    """``who``'s tenant, or (audited first) the tenant of the member ``as_user`` names."""
    if as_user is None or as_user == who.user_id:
        return who.tenant
    if who.member is None:
        raise AccountError(403, "as_user_refused", "as_user needs an account")
    if not who.at_least("admin"):
        raise AccountError(403, "forbidden_role", "only an admin or the owner acts as_user")
    accounts = accounts_of(request)
    target = accounts.store.member(as_user)
    if target is None or target.org_id != who.member.org_id:
        raise AccountError(404, "not_found", f"no member {as_user} in this org")
    accounts.record(action, org_id=who.member.org_id, actor_id=who.member.user.id,
                    target=as_user, ip=client_ip(request), detail=detail)  # fmt: skip
    return target.user.tenant


def read_tenant(
    request: Request,
    who: Annotated[Principal, Depends(current_principal)],
    as_user: str | None = None,
) -> str:
    """The tenant a read reads: the principal's, or a member's for an Admin (audited)."""
    detail = {"method": request.method, "path": request.url.path}
    return _tenant_as(request, who, as_user, "read_session_as_admin", detail)


def stop_tenant(
    request: Request,
    run: str,
    who: Annotated[Principal, Depends(current_principal)],
    as_user: str | None = None,
) -> str:
    """The tenant a Stop of ``run`` acts in: the sender's, or a member's for an Admin."""
    if not who.at_least("member"):
        raise AccountError(403, "forbidden_role", "a viewer reads only")
    return _tenant_as(request, who, as_user, "stop_run_as_admin", {"run": run})


# --- who a command is recorded as (``by``) ------------------------------------------------


IDENTITY_PREFIXES = ("user:", "agent:", "g-", "u-")
"""A ``by`` that starts with one of these names someone, never a role."""


@dataclass(frozen=True)
class ByRule:
    """How the server sets a command's ``by`` from the principal that sent it.

    ``identity`` is what the event store records: ``user:<id>`` for an account (cookie or
    API token), ``local`` in local mode, None when the app trusts ``X-Aksha-User`` (the
    body's ``by`` is kept, as before accounts). With an identity:

    - A command whose body names no ``by``, the default ``person`` (the role of anyone who
      sends one, and what the CLI sends when given no ``--by``) or the identity itself is
      recorded under the identity. Any other ``by`` (``agent:<name>``, another user, a
      role) is refused 422 ``by_not_allowed``: nobody sends as someone else.
    - ``DecideCorrection`` is the exception: the runtime decides it only for the role the
      correction names as its domain owner (ADR-P05 C1, ``not_domain_owner``), so its
      ``by`` stays a role name. ``person`` maps to every principal that may command; any
      other role name (a domain owner set by ``AKSHA_CORRECTION_APPROVER``) maps only to
      an Admin or the Owner, since the org has no per-domain roles yet. An identity-shaped
      ``by`` is refused. Who decided is written to the audit log beside it.
    """

    identity: str | None
    role: str = "owner"

    def may_act_as(self, role_name: str) -> bool:
        """Whether this principal may decide a correction as ``role_name`` (class doc)."""
        if not role_name or role_name == LOCAL_TENANT or role_name.startswith(IDENTITY_PREFIXES):
            return False
        if role_name == DEFAULT_BY:
            return RANK[self.role] >= RANK["member"]
        return RANK[self.role] >= RANK["admin"]

    def bind(self, command: NamedCommand) -> NamedCommand:
        """``command`` with ``by`` set from the principal; ``by_not_allowed`` otherwise."""
        if self.identity is None:
            return command
        if isinstance(command, DecideCorrection):
            if not self.may_act_as(command.by):
                raise WireInvalid("by_not_allowed", f"you may not decide as {command.by!r}")
            return command
        if command.by not in (DEFAULT_BY, self.identity):
            raise WireInvalid("by_not_allowed", f"by is set from who you are ({self.identity}), "
                              f"not {command.by!r}")  # fmt: skip
        return replace(command, by=self.identity)


def by_rule(who: Principal, request: Request) -> ByRule:
    """The :class:`ByRule` of ``who`` (``via`` header trusts the body's ``by``)."""
    if who.via == "header" or auth_state(request).config.trust_user_header:
        return ByRule(None, who.role)
    return ByRule("local" if who.member is None else f"user:{who.member.user.id}", who.role)


def principal_by(request: Request, who: Annotated[Principal, Depends(current_principal)]) -> ByRule:
    """The dependency the reads router takes to bind ``by`` on its writes."""
    return by_rule(who, request)


SignedIn = Annotated[Principal, Depends(current_principal)]
"""Any principal."""
AccountHolder = Annotated[Principal, Depends(account_principal)]
"""A principal with an account."""
AdminPrincipal = Annotated[Principal, Depends(admin_principal)]
"""An Admin or the Owner."""
Commander = Annotated[Principal, Depends(command_principal)]
"""A Member or above, acting for itself (no ``as_user``)."""
ReadTenant = Annotated[str, Depends(read_tenant)]
"""The tenant a read reads (``?as_user=`` for an Admin, audited)."""
StopTenant = Annotated[str, Depends(stop_tenant)]
"""The tenant a Stop acts in (``?as_user=`` for an Admin, audited)."""
