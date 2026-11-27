"""The rules of accounts, over :class:`AccountStore`.

Sign-up and claim, sign-in with throttling, sessions, invites, roles, API tokens, guests.

Roles, from most to least: ``owner``, ``admin``, ``member``, ``viewer``. An Admin can never
grant or change the Owner role; only the Owner hands it on (and becomes an Admin). Each
user's ``tenant`` is the runtime owner their work is kept under: their user ID, or
``local`` for the person who claims a server they used in local mode.

A guest is the Owner of a personal org of their own, alone in it, and stays so when
saved as an account; only an invite brings someone into the server's main org. Guest
creation is limited per client address (five an hour), counted from the audit rows.

Throttling counts the ``sign_in_failed`` audit rows of an (email, address) pair: five within
fifteen minutes refuse that pair with ``throttled`` for fifteen minutes after the fifth.
No counter is stored; the audit rows are the facts it is derived from.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from aksha.interfaces.http.accounts.credentials import (
    TOKEN_PREFIX,
    Passwords,
    check_email,
    check_password,
    digest,
    new_id,
    new_secret,
)
from aksha.interfaces.http.accounts.errors import AccountError
from aksha.interfaces.http.accounts.store import (
    AccountStore,
    ApiToken,
    AuditRecord,
    AuthSession,
    Invite,
    Member,
    Org,
    StoreRefused,
    User,
)

__all__ = ["AccountError", "Accounts", "Resolved", "SignedIn"]

ROLES = ("owner", "admin", "member", "viewer")
RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}
INVITABLE = ("admin", "member", "viewer")
LOCAL_TENANT = "local"
"""The tenant of local mode, the CLI's default owner; a claim keeps it."""

HOUR = 3600.0
DAY = 24 * HOUR
SESSION_TTL_S = 14 * DAY
"""A remembered session ends 14 days after it was last used."""
SHORT_SESSION_TTL_S = 12 * HOUR
"""A session without ``remember`` is a browser-session cookie that ends 12 hours on."""
INVITE_TTL_S = 7 * DAY
GUEST_TTL_S = DAY
GUESTS_PER_HOUR = 5
"""Guests one client address may start in an hour."""
THROTTLE_LIMIT = 5
THROTTLE_WINDOW_S = 15 * 60.0
TOUCH_EVERY_S = 60.0
"""A session's sliding expiry and a token's last use are written at most once a minute."""

MAX_NAME = 200


def _invalid() -> AccountError:
    return AccountError(401, "invalid_credentials", "the email or the password is wrong")


def _name(value: object, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME:
        raise AccountError(422, "validation", f"a name is 1 to {MAX_NAME} characters")
    return value.strip()


@dataclass(frozen=True)
class SignedIn:
    """A new session: the member, the stored session and the cookie's secret (shown once)."""

    member: Member
    session: AuthSession
    token: str = field(repr=False)


@dataclass(frozen=True)
class Resolved:
    """Who a cookie or an API token belongs to; ``session`` is None for a token."""

    member: Member
    session: AuthSession | None


class Accounts:
    """Account rules over one store; every check that refuses raises :class:`AccountError`."""

    def __init__(self, store: AccountStore, passwords: Passwords | None = None) -> None:
        """Hold the store and the password hasher (library defaults unless given)."""
        self.store = store
        self.passwords = passwords or Passwords()

    def now(self) -> float:
        """The store's clock."""
        return self.store.clock()

    def close(self) -> None:
        """Close the store."""
        self.store.close()

    def has_accounts(self) -> bool:
        """Whether the server has its org (and so its Owner): local mode ends then."""
        return self.store.org_of_kind("main") is not None

    def main_org(self) -> Org | None:
        """The server's org, or None in local mode."""
        return self.store.org_of_kind("main")

    def record(self, action: str, *, org_id: str | None, actor_id: str | None,
               target: str | None, ip: str | None,
               detail: dict[str, Any] | None = None) -> None:  # fmt: skip
        """Append one audit record."""
        self.store.audit(action, org_id=org_id, actor_id=actor_id, target=target,
                         detail=detail or {}, ip=ip)  # fmt: skip

    # --- sign-up and claim ------------------------------------------------------------------

    def signup(self, email: object, password: object, name: object, org_name: object,
               ip: str | None, *, local: bool = False) -> SignedIn:  # fmt: skip
        """Create the server's org and its Owner, and sign them in; only once per server.

        With ``local`` (the claim), the Owner's tenant is ``local``, so what was done in
        local mode stays theirs; otherwise it is their new user ID.
        """
        if self.has_accounts():
            raise AccountError(409, "signup_closed", "the server has its owner; ask for an invite")
        address = check_email(email)
        user_id = new_id("u")
        user = User(user_id, address, _name(name), LOCAL_TENANT if local else user_id, False,
                    None, self.now(), self.passwords.hash(check_password(password)))  # fmt: skip
        try:
            org = self.store.found_org(_name(org_name, "My organisation"), user)
        except StoreRefused as e:
            if e.code == "org_exists":
                raise AccountError(409, "signup_closed", "the server has its owner") from None
            raise AccountError(409, e.code, e.message) from None
        self.record("claim" if local else "signup", org_id=org.id, actor_id=user_id,
                    target=user_id, ip=ip)  # fmt: skip
        return self.start(self._member(user_id), remember=True)

    # --- sign-in ----------------------------------------------------------------------------

    def throttled(self, email: str, ip: str | None) -> bool:
        """Whether five failures within 15 minutes, the last under 15 minutes ago, lock it."""
        now = self.now()
        times = self.store.failed_sign_ins(email, ip, now - 2 * THROTTLE_WINDOW_S)
        for i in range(len(times) - THROTTLE_LIMIT + 1):
            newest, oldest = times[i], times[i + THROTTLE_LIMIT - 1]
            if now - newest < THROTTLE_WINDOW_S and newest - oldest <= THROTTLE_WINDOW_S:
                return True
        return False

    def login(self, email: object, password: object, *, remember: bool,
              ip: str | None) -> SignedIn:  # fmt: skip
        """Sign in; an unknown email, a wrong password and a deactivated account read the same."""
        if not isinstance(email, str) or not isinstance(password, str):
            raise AccountError(422, "validation", "email and password are strings")
        key = email.strip().lower()
        if self.throttled(key, ip):
            raise AccountError(429, "throttled", "too many failed sign-ins; try again later")
        user = self.store.user_by_email(key) if key else None
        member = None if user is None else self.store.member(user.id)
        stored = None if user is None else user.password_hash
        ok = self.passwords.verify(stored, password[:2048])
        if not ok or user is None or member is None or not member.active:
            self.record("sign_in_failed", org_id=None if member is None else member.org_id,
                        actor_id=None, target=key, ip=ip)  # fmt: skip
            raise _invalid()
        if stored is not None and self.passwords.needs_rehash(stored):
            self.store.set_password(user.id, self.passwords.hash(password))
        self.record("sign_in", org_id=member.org_id, actor_id=user.id, target=user.id, ip=ip)
        return self.start(member, remember=remember)

    def start(self, member: Member, *, remember: bool) -> SignedIn:
        """A new session for ``member``; a guest's ends with the guest at the latest."""
        now = self.now()
        expires = now + (SESSION_TTL_S if remember else SHORT_SESSION_TTL_S)
        if member.user.guest_expires_at is not None:
            expires = min(expires, member.user.guest_expires_at)
        token = new_secret()
        session = self.store.create_session(digest(token), member.user.id, csrf=new_secret(),
                                            remember=remember, expires_at=expires)  # fmt: skip
        return SignedIn(member, session, token)

    def _usable(self, member: Member | None) -> bool:
        if member is None or not member.active:
            return False
        expires = member.user.guest_expires_at
        return expires is None or expires > self.now()

    def resolve_session(self, token: str) -> Resolved | None:
        """The member a session cookie signs in, sliding a remembered session; else None."""
        session = self.store.session(digest(token))
        if session is None:
            return None
        now = self.now()
        if session.expires_at <= now:
            self.store.delete_session(session.token_hash)
            return None
        member = self.store.member(session.user_id)
        if member is None or not self._usable(member):
            return None
        if session.remember and now - session.last_seen_at >= TOUCH_EVERY_S:
            expires = now + SESSION_TTL_S
            if member.user.guest_expires_at is not None:
                expires = min(expires, member.user.guest_expires_at)
            self.store.touch_session(session.token_hash, expires)
            session = replace(session, expires_at=expires, last_seen_at=now)
        return Resolved(member, session)

    def resolve_token(self, secret: str) -> Resolved | None:
        """The member an API token acts for, noting its use; else None."""
        if not secret.startswith(TOKEN_PREFIX):
            return None
        token = self.store.token_by_secret(digest(secret))
        if token is None or token.revoked_at is not None:
            return None
        member = self.store.member(token.user_id)
        if member is None or not self._usable(member):
            return None
        if token.last_used_at is None or self.now() - token.last_used_at >= TOUCH_EVERY_S:
            self.store.token_used(token.id)
        return Resolved(member, None)

    def logout(self, session: AuthSession) -> None:
        """End one session."""
        self.store.delete_session(session.token_hash)

    def change_password(self, member: Member, current: object, new: object) -> SignedIn:
        """Change the password after checking the current one; every session ends.

        Returns a new session, for the browser that made the change.
        """
        if not isinstance(current, str) or not self.passwords.verify(
            member.user.password_hash, current
        ):
            raise _invalid()
        self.store.set_password(member.user.id, self.passwords.hash(check_password(new)))
        self.store.delete_sessions_of(member.user.id)
        return self.start(self._member(member.user.id), remember=True)

    def _member(self, user_id: str) -> Member:
        found = self.store.member(user_id)
        if found is None:
            raise AccountError(404, "not_found", f"no user {user_id}")
        return found

    # --- invites ----------------------------------------------------------------------------

    def invite(self, actor: Member, emails: object, role: object,
               ip: str | None) -> list[tuple[Invite, str]]:  # fmt: skip
        """Invite each email with ``role``; returns each invite with its link token (once)."""
        if actor.user.is_guest:
            raise AccountError(403, "guest_forbidden", "a guest invites nobody: save it first")
        if role not in INVITABLE:
            raise AccountError(422, "validation", "role is admin, member or viewer")
        if not isinstance(emails, list) or not emails:
            raise AccountError(422, "validation", "emails is a non-empty list")
        addresses = [check_email(e) for e in emails]
        for address in addresses:
            if self.store.user_by_email(address) is not None:
                raise AccountError(409, "email_taken", f"{address} already has an account")
        out = []
        for address in addresses:
            token = new_secret()
            invite = self.store.create_invite(new_id("inv"), actor.org_id, address, str(role),
                                              digest(token), actor.user.id,
                                              self.now() + INVITE_TTL_S)  # fmt: skip
            self.record(
                "invite_sent",
                org_id=actor.org_id,
                actor_id=actor.user.id,
                target=invite.id,
                ip=ip,
                detail={"email": address, "role": role},
            )
            out.append((invite, token))
        return out

    def invite_by_token(self, token: str) -> Invite:
        """The invite a link token names; ``not_found`` for an unknown or replaced link."""
        invite = self.store.invite_by_token(digest(token))
        if invite is None:
            raise AccountError(404, "not_found", "no such invite")
        return invite

    def accept(self, token: str, name: object, password: object, ip: str | None) -> SignedIn:
        """Create the invited account and sign it in; the invite must be valid."""
        invite = self.invite_by_token(token)
        state = invite.state(self.now())
        if state != "valid":
            raise AccountError(409, f"invite_{state}", f"the invite is {state}")
        user_id = new_id("u")
        user = User(user_id, invite.email, _name(name), user_id, False, None, self.now(),
                    self.passwords.hash(check_password(password)))  # fmt: skip
        try:
            self.store.accept_invite(invite, user)
        except StoreRefused as e:
            raise AccountError(409, e.code, e.message) from None
        self.record("invite_accepted", org_id=invite.org_id, actor_id=user_id, target=invite.id,
                    ip=ip, detail={"role": invite.role})  # fmt: skip
        return self.start(self._member(user_id), remember=True)

    def _own_invite(self, actor: Member, invite_id: str) -> Invite:
        invite = self.store.invite(invite_id)
        if invite is None or invite.org_id != actor.org_id:
            raise AccountError(404, "not_found", f"no invite {invite_id}")
        return invite

    def resend(self, actor: Member, invite_id: str, ip: str | None) -> tuple[Invite, str]:
        """A new link and a new 7-day expiry for an open invite; the old link stops working."""
        invite = self._own_invite(actor, invite_id)
        state = invite.state(self.now())
        if state in ("used", "revoked"):
            raise AccountError(409, f"invite_{state}", f"the invite is {state}")
        token = new_secret()
        self.store.reissue_invite(invite.id, digest(token), self.now() + INVITE_TTL_S)
        self.record("invite_sent", org_id=actor.org_id, actor_id=actor.user.id, target=invite.id,
                    ip=ip, detail={"email": invite.email, "role": invite.role,
                                   "resend": True})  # fmt: skip
        found = self.store.invite(invite.id)
        assert found is not None
        return found, token

    def revoke(self, actor: Member, invite_id: str, ip: str | None) -> Invite:
        """Revoke an invite that was not used."""
        invite = self._own_invite(actor, invite_id)
        if invite.accepted_at is not None:
            raise AccountError(409, "invite_used", "the invite was used")
        if invite.revoked_at is None:
            self.store.revoke_invite(invite.id)
            self.record("invite_revoked", org_id=actor.org_id, actor_id=actor.user.id,
                        target=invite.id, ip=ip, detail={"email": invite.email})  # fmt: skip
        found = self.store.invite(invite.id)
        assert found is not None
        return found

    # --- members ----------------------------------------------------------------------------

    def update_member(self, actor: Member, user_id: str, *, role: object = None,
                      active: object = None, ip: str | None) -> Member:  # fmt: skip
        """Change a member's role or deactivate them, by the rules in the module docstring.

        Granting ``owner`` is the Owner handing it on: the Owner becomes an Admin.
        Deactivating ends every session of the member.
        """
        target = self.store.member(user_id)
        if target is None or target.org_id != actor.org_id:
            raise AccountError(404, "not_found", f"no member {user_id} in this org")
        if role is not None and role not in ROLES:
            raise AccountError(422, "validation", "role is owner, admin, member or viewer")
        if active is not None and not isinstance(active, bool):
            raise AccountError(422, "validation", "active is true or false")
        if target.user.id == actor.user.id:
            raise AccountError(403, "own_account", "nobody changes their own role or access")
        if actor.role != "owner" and (target.role == "owner" or role == "owner"):
            raise AccountError(403, "owner_only", "only the owner hands on or changes the owner")
        if role == "owner" and active is False:
            raise AccountError(422, "validation", "hand on ownership and deactivate separately")
        org = actor.org_id
        if role is not None and role != target.role:
            self.store.set_role(target.user.id, str(role))
            self.record("role_changed", org_id=org, actor_id=actor.user.id, target=target.user.id,
                        ip=ip, detail={"role": role, "was": target.role})  # fmt: skip
            if role == "owner":
                self.store.set_role(actor.user.id, "admin")
                self.record("role_changed", org_id=org, actor_id=actor.user.id,
                            target=actor.user.id, ip=ip,
                            detail={"role": "admin", "was": "owner"})  # fmt: skip
        if active is not None and active != target.active:
            self.store.set_active(target.user.id, active)
            action = "user_reactivated" if active else "user_deactivated"
            self.record(action, org_id=org, actor_id=actor.user.id, target=target.user.id, ip=ip)
        return self._member(target.user.id)

    def audit(self, actor: Member, *, limit: int, before: int | None, action: str | None,
              actor_id: str | None) -> list[AuditRecord]:  # fmt: skip
        """The org's audit records, newest first."""
        return self.store.audit_records(org_id=actor.org_id, limit=limit, before=before,
                                        action=action, actor_id=actor_id)  # fmt: skip

    # --- API tokens -------------------------------------------------------------------------

    def create_token(self, member: Member, name: object, ip: str | None) -> tuple[ApiToken, str]:
        """A new API token; its secret is returned this once and never again."""
        secret = new_secret(TOKEN_PREFIX)
        token = self.store.create_token(new_id("tok"), member.user.id, _name(name),
                                        digest(secret))  # fmt: skip
        self.record("token_created", org_id=member.org_id, actor_id=member.user.id,
                    target=token.id, ip=ip, detail={"name": token.name})  # fmt: skip
        return token, secret

    def revoke_token(self, member: Member, token_id: str, ip: str | None) -> None:
        """Revoke one of the member's own tokens."""
        mine = {t.id: t for t in self.store.tokens(member.user.id)}
        if token_id not in mine:
            raise AccountError(404, "not_found", f"no token {token_id}")
        if mine[token_id].revoked_at is None:
            self.store.revoke_token(token_id)
            self.record("token_revoked", org_id=member.org_id, actor_id=member.user.id,
                        target=token_id, ip=ip)  # fmt: skip

    # --- guests -----------------------------------------------------------------------------

    def create_guest(self, ip: str | None) -> SignedIn:
        """A guest for 24 hours, alone in a personal org of their own (its Owner), signed in.

        Each guest has their own org and tenant, so no guest sees another. An address may
        start five guests an hour (``too_many_guests``), counted from the audit rows.
        """
        if self.store.actions_from("guest_created", ip, self.now() - HOUR) >= GUESTS_PER_HOUR:
            message = f"at most {GUESTS_PER_HOUR} guests an hour from one address"
            raise AccountError(429, "too_many_guests", message)
        guest_id = new_id("g")
        org = self.store.create_personal(guest_id, "Guest", tenant=guest_id,
                                         guest_expires_at=self.now() + GUEST_TTL_S)  # fmt: skip
        self.record("guest_created", org_id=org.id, actor_id=guest_id, target=guest_id, ip=ip)
        return self.start(self._member(guest_id), remember=True)

    def save_guest(self, member: Member, email: object, password: object, name: object,
                   ip: str | None) -> SignedIn:  # fmt: skip
        """Turn a guest into a normal account that keeps its personal org, tenant and work.

        The account never joins the server's main org: that takes an invite.
        """
        if not member.user.is_guest:
            raise AccountError(409, "not_a_guest", "this account is not a guest")
        if self.main_org() is None:
            raise AccountError(409, "setup_required", "the server has no owner yet")
        address = check_email(email)
        hashed = self.passwords.hash(check_password(password))
        try:
            self.store.save_guest(member.user.id, address, _name(name), hashed)
        except StoreRefused as e:
            raise AccountError(409, e.code, e.message) from None
        self.store.delete_sessions_of(member.user.id)
        self.record("guest_saved", org_id=member.org_id, actor_id=member.user.id,
                    target=member.user.id, ip=ip)  # fmt: skip
        return self.start(self._member(member.user.id), remember=True)
