"""The account routes: ``/auth``, ``/org`` and ``/tokens``.

``/auth`` holds sign-up, claim, sign-in, invites and guests. The rules live in
``accounts/service.py``; these routes parse, check the role and set or clear the cookie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

from aksha.interfaces.http.accounts.errors import AccountError
from aksha.interfaces.http.accounts.service import SignedIn as NewSession
from aksha.interfaces.http.accounts.store import ApiToken, AuditRecord, Invite, Member
from aksha.interfaces.http.auth import (
    COOKIE,
    AccountHolder,
    AdminPrincipal,
    Principal,
    SignedIn,
    accounts_of,
    auth_state,
    client_ip,
    is_local,
)
from aksha.interfaces.http.common import API, json_body

router = APIRouter(prefix=API)
MAX_AUDIT = 500


def iso(t: float | None) -> str | None:
    """Epoch seconds as an ISO 8601 UTC time, or None."""
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def member_json(m: Member) -> dict[str, Any]:
    """A member as the API shows them."""
    return {"id": m.user.id, "email": m.user.email, "name": m.user.name, "role": m.role,
            "active": m.active, "is_guest": m.user.is_guest,
            "created_at": iso(m.user.created_at)}  # fmt: skip


def invite_json(request: Request, i: Invite, token: str | None = None) -> dict[str, Any]:
    """An invite; with ``token`` (only when just issued), its token and link too."""
    out: dict[str, Any] = {
        "id": i.id, "email": i.email, "role": i.role, "state": i.state(accounts_of(request).now()),
        "created_at": iso(i.created_at), "expires_at": iso(i.expires_at),
    }  # fmt: skip
    inviter = accounts_of(request).store.user(i.inviter_id)
    out["invited_by"] = None if inviter is None else {"name": inviter.name, "email": inviter.email}
    if token is not None:
        out["token"] = token
        out["link"] = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return out


def token_json(t: ApiToken) -> dict[str, Any]:
    """An API token's record (never its secret)."""
    return {"id": t.id, "name": t.name, "created_at": iso(t.created_at),
            "last_used_at": iso(t.last_used_at), "revoked": t.revoked_at is not None}  # fmt: skip


def audit_json(r: AuditRecord) -> dict[str, Any]:
    """One audit record."""
    return {
        "id": r.id,
        "at": iso(r.at),
        "org": r.org_id,
        "actor": r.actor_id,
        "action": r.action,
        "target": r.target,
        "detail": dict(r.detail),
        "ip": r.ip,
    }


def mode(request: Request) -> str:
    """``trusted``, ``local`` or ``accounts``."""
    config = auth_state(request).config
    if config.trust_user_header or config.accounts is None:
        return "trusted"
    return "accounts" if config.accounts.has_accounts() else "local"


def me_json(request: Request, who: Principal) -> dict[str, Any]:
    """Who the request acts for: what ``/auth/me`` and every sign-in answer."""
    org = None
    if who.member is not None:
        found = accounts_of(request).store.org(who.member.org_id)
        org = None if found is None else {"id": found.id, "name": found.name, "kind": found.kind}
    user = who.member.user if who.member is not None else None
    return {
        "mode": mode(request), "via": who.via, "tenant": who.tenant, "role": who.role,
        "csrf": who.csrf, "org": org,
        "user": None if who.member is None else member_json(who.member),
        "is_guest": who.is_guest,
        "guest_expires_at": iso(None if user is None else user.guest_expires_at),
    }  # fmt: skip


def _secure(request: Request) -> bool:
    return auth_state(request).config.cookie_secure or request.url.scheme == "https"


def set_cookie(request: Request, response: Response, token: str, *, remember: bool,
               expires_at: float) -> None:  # fmt: skip
    """The session cookie: HttpOnly, SameSite=Lax, Secure over HTTPS (or when forced)."""
    max_age = round(expires_at - accounts_of(request).now()) if remember else None
    response.set_cookie(COOKIE, token, max_age=max_age, path="/", httponly=True,
                        samesite="lax", secure=_secure(request))  # fmt: skip


def signed_in_response(request: Request, response: Response, new: NewSession) -> dict[str, Any]:
    """Set the cookie of a new session and answer as ``/auth/me`` would."""
    set_cookie(request, response, new.token, remember=new.session.remember,
               expires_at=new.session.expires_at)  # fmt: skip
    who = Principal(new.member.user.tenant, new.member.role, "cookie", new.member, new.session,
                    new.session.csrf)  # fmt: skip
    return me_json(request, who)


# --- /auth ----------------------------------------------------------------------------------


@router.get("/auth/status")
def status(request: Request) -> dict[str, Any]:
    """``mode`` (``local``, ``accounts``, or ``trusted`` for the header switch), guests, sign-up."""
    current = mode(request)
    return {"mode": current, "guests": current != "trusted" and auth_state(request).config.guests,
            "signup_open": current == "local"}  # fmt: skip


@router.post("/auth/signup")
async def signup(request: Request, response: Response) -> dict[str, Any]:
    """Create the server's org and its Owner (only while the server has no account)."""
    body = await json_body(request)
    new = accounts_of(request).signup(body.get("email"), body.get("password"), body.get("name"),
                                      body.get("org_name"), client_ip(request))  # fmt: skip
    return signed_in_response(request, response, new)


@router.post("/auth/claim")
async def claim(request: Request, response: Response) -> dict[str, Any]:
    """Sign-up from this machine that keeps local mode's work: the Owner's tenant is ``local``."""
    accounts = accounts_of(request)
    if not is_local(request):
        raise AccountError(403, "loopback_only", "claim the server from the machine it runs on")
    body = await json_body(request)
    new = accounts.signup(body.get("email"), body.get("password"), body.get("name"),
                          body.get("org_name"), client_ip(request), local=True)  # fmt: skip
    return signed_in_response(request, response, new)


@router.post("/auth/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    """Sign in with email and password; ``remember`` (default true) keeps it for 14 days."""
    body = await json_body(request)
    remember = body.get("remember", True)
    if not isinstance(remember, bool):
        raise AccountError(422, "validation", "remember is true or false")
    new = accounts_of(request).login(body.get("email"), body.get("password"), remember=remember,
                                     ip=client_ip(request))  # fmt: skip
    return signed_in_response(request, response, new)


@router.post("/auth/logout")
def logout(request: Request, response: Response, who: SignedIn) -> dict[str, Any]:
    """End this browser's session."""
    if who.session is not None:
        accounts_of(request).logout(who.session)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax",
                           secure=_secure(request))  # fmt: skip
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, response: Response, who: SignedIn) -> dict[str, Any]:
    """Who this request acts for; renews a remembered cookie's lifetime."""
    session, token = who.session, request.cookies.get(COOKIE)
    if session is not None and session.remember and token:
        set_cookie(request, response, token, remember=True, expires_at=session.expires_at)
    return me_json(request, who)


@router.post("/auth/password")
async def password(request: Request, response: Response, who: AccountHolder) -> dict[str, Any]:
    """Change the password (``current``, ``new``); every session of the user ends."""
    assert who.member is not None
    body = await json_body(request)
    new = accounts_of(request).change_password(who.member, body.get("current"), body.get("new"))
    return signed_in_response(request, response, new)


@router.get("/auth/invites/{token}")
def invite_preview(token: str, request: Request) -> dict[str, Any]:
    """What an invite link offers: the org, the inviter, the role, the email, its state."""
    accounts = accounts_of(request)
    i = accounts.invite_by_token(token)
    org = accounts.store.org(i.org_id)
    inviter = accounts.store.user(i.inviter_id)
    return {
        "org": None if org is None else {"id": org.id, "name": org.name},
        "inviter": None if inviter is None else {"name": inviter.name, "email": inviter.email},
        "role": i.role, "email": i.email, "state": i.state(accounts.now()),
        "expires_at": iso(i.expires_at),
    }  # fmt: skip


@router.post("/auth/invites/{token}/accept")
async def invite_accept(token: str, request: Request, response: Response) -> dict[str, Any]:
    """Accept an invite with a name and a password; signs the new account in."""
    body = await json_body(request)
    new = accounts_of(request).accept(token, body.get("name"), body.get("password"),
                                      client_ip(request))  # fmt: skip
    return signed_in_response(request, response, new)


@router.post("/auth/guest")
def guest(request: Request, response: Response) -> dict[str, Any]:
    """Start a 24-hour guest account in its own tenant (only with ``AKSHA_GUESTS=1``)."""
    accounts = accounts_of(request)
    if not auth_state(request).config.guests:
        raise AccountError(403, "guests_disabled", "this server has no guest sandbox")
    return signed_in_response(request, response, accounts.create_guest(client_ip(request)))


@router.post("/auth/guest/save")
async def guest_save(request: Request, response: Response, who: AccountHolder) -> dict[str, Any]:
    """Keep a guest's work as a normal account (email, password, name) in the server's org."""
    assert who.member is not None
    body = await json_body(request)
    new = accounts_of(request).save_guest(who.member, body.get("email"), body.get("password"),
                                          body.get("name"), client_ip(request))  # fmt: skip
    return signed_in_response(request, response, new)


# --- /org -----------------------------------------------------------------------------------


@router.get("/org/members")
def members(request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """Everyone in the org."""
    assert who.member is not None
    found = accounts_of(request).store.members(who.member.org_id)
    return {"members": [member_json(m) for m in found]}


@router.patch("/org/members/{user}")
async def member_update(user: str, request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """Change a member's ``role`` or ``active``."""
    assert who.member is not None
    body = await json_body(request)
    changed = accounts_of(request).update_member(who.member, user, role=body.get("role"),
                                                 active=body.get("active"),
                                                 ip=client_ip(request))  # fmt: skip
    return {"member": member_json(changed)}


@router.get("/org/invites")
def invites(request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """The org's invites, newest first (no links: a resend issues a new one)."""
    assert who.member is not None
    found = accounts_of(request).store.invites(who.member.org_id)
    return {"invites": [invite_json(request, i) for i in found]}


@router.post("/org/invites")
async def invite_create(request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """Invite ``emails`` with ``role``; answers each invite's link to copy (no email is sent)."""
    assert who.member is not None
    body = await json_body(request)
    made = accounts_of(request).invite(who.member, body.get("emails"), body.get("role"),
                                       client_ip(request))  # fmt: skip
    return {"invites": [invite_json(request, i, token) for i, token in made]}


@router.post("/org/invites/{invite_id}/resend")
def invite_resend(invite_id: str, request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """A new link and expiry for an open invite."""
    assert who.member is not None
    i, token = accounts_of(request).resend(who.member, invite_id, client_ip(request))
    return {"invite": invite_json(request, i, token)}


@router.delete("/org/invites/{invite_id}")
def invite_revoke(invite_id: str, request: Request, who: AdminPrincipal) -> dict[str, Any]:
    """Revoke an invite."""
    assert who.member is not None
    i = accounts_of(request).revoke(who.member, invite_id, client_ip(request))
    return {"invite": invite_json(request, i)}


@router.get("/org/audit")
def audit(request: Request, who: AdminPrincipal, limit: int = 50, before: int | None = None,
          action: str | None = None, actor: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The org's audit log, newest first; ``before`` is the last ID of the previous page."""
    assert who.member is not None
    found = accounts_of(request).audit(who.member, limit=max(1, min(limit, MAX_AUDIT)),
                                       before=before, action=action, actor_id=actor)  # fmt: skip
    return {"records": [audit_json(r) for r in found]}


# --- /tokens --------------------------------------------------------------------------------


@router.get("/tokens")
def tokens(request: Request, who: AccountHolder) -> dict[str, Any]:
    """The user's API tokens."""
    assert who.member is not None
    return {"tokens": [token_json(t) for t in accounts_of(request).store.tokens(who.user_id or "")]}


@router.post("/tokens")
async def token_create(request: Request, who: AccountHolder) -> dict[str, Any]:
    """A new API token (``name``); its ``secret`` is shown this once."""
    assert who.member is not None
    body = await json_body(request)
    made, secret = accounts_of(request).create_token(who.member, body.get("name"),
                                                     client_ip(request))  # fmt: skip
    return {"token": token_json(made), "secret": secret}


@router.delete("/tokens/{token_id}")
def token_revoke(token_id: str, request: Request, who: AccountHolder) -> dict[str, Any]:
    """Revoke one of the user's tokens."""
    assert who.member is not None
    accounts_of(request).revoke_token(who.member, token_id, client_ip(request))
    return {"ok": True}
