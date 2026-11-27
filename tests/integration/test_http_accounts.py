"""Accounts over HTTP: local mode and its claim, sign-in, CSRF, invites, roles, tokens, guests.

Every request is made through ``TestClient`` against the real app over the scribe fixture
agent, with the accounts database in the test's folder and a clock the test moves.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aksha.app import open_blob_store, open_reader, open_session, session_capabilities
from aksha.interfaces.http.accounts.credentials import Passwords, digest
from aksha.interfaces.http.accounts.service import Accounts
from aksha.interfaces.http.accounts.store import AccountStore
from aksha.interfaces.http.auth import COOKIE, AuthConfig, command_tenant, read_tenant
from aksha.interfaces.http.reads import reads_router
from aksha.interfaces.http.server import create_app
from aksha.runtime.projections.view import load_view
from aksha.runtime.session.worker import RuntimeThread
from tests.support.session import SESSION, config

FAST = Passwords(time_cost=1, memory_cost=8, parallelism=1)
PW = "a long password 1"
REMOTE = ("203.0.113.7", 40000)
LOOPBACK = ("127.0.0.1", 40000)
DAY = 86_400.0


class Tick:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Bench:
    """The app over a runtime thread and an accounts database, and the clients made for it."""

    def __init__(self, tmp: Path, *, upload_max_bytes: int = 50 * 1024 * 1024,
                 **auth: Any) -> None:  # fmt: skip
        self.clock = Tick()
        cfg = self.cfg = config(tmp)
        self.runtime = RuntimeThread(lambda: open_session(cfg))
        self.runtime.start()
        self.accounts = Accounts(AccountStore(tmp / "accounts.sqlite", clock=self.clock), FAST)

        def record() -> dict[str, Any]:
            return self.runtime.call(lambda s: session_capabilities(s, cfg).as_json()).result(10)

        reads = reads_router(lambda: open_reader(cfg), lambda: open_blob_store(cfg.storage),
                             read_tenant, submit=self.runtime.submit,
                             session_meta=self.accounts.store,
                             command_tenant_dep=command_tenant)  # fmt: skip
        self.app = create_app(self.runtime, lambda: open_reader(cfg), record, heartbeat_s=0.05,
                              auth=AuthConfig(accounts=self.accounts, **auth),
                              extra_routers=(reads,),
                              upload_max_bytes=upload_max_bytes)  # fmt: skip
        self.stack = ExitStack()

    def client(
        self, *, local: bool = False, ip: str | None = None, base_url: str | None = None
    ) -> TestClient:
        default = "http://127.0.0.1:8765" if local else "http://aksha.example"
        peer = LOOPBACK if local else ((ip, 40000) if ip else REMOTE)
        c = TestClient(self.app, base_url=base_url or default, client=peer)
        return self.stack.enter_context(c)

    def close(self) -> None:
        self.stack.close()
        self.app.state.closing.set()
        self.runtime.stop()
        self.accounts.close()

    def payloads(self, tenant: str, event_type: str) -> list[dict[str, Any]]:
        """Every ``event_type`` payload of ``tenant`` in the event store, oldest first."""
        store = open_reader(self.cfg)
        try:
            events = load_view(store, tenant).events
        finally:
            store.close()
        return [dict(e.payload) for e in events if e.type == event_type]

    def audit(self, action: str) -> list[Any]:
        return [r for r in self.accounts.store.audit_records(org_id=None) if r.action == action]


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    yield b
    b.close()


@pytest.fixture
def guest_bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path, guests=True)
    yield b
    b.close()


def signed(c: TestClient, r: Any) -> TestClient:
    """Keep the CSRF token a sign-in returned on the client, as the web app does."""
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


def signup(bench: Bench, email: str = "owner@example.org") -> TestClient:
    c = bench.client()
    body = {"email": email, "password": PW, "name": "Owner", "org_name": "Acme"}
    return signed(c, c.post("/api/v1/auth/signup", json=body))


def invite(admin: TestClient, email: str, role: str) -> str:
    r = admin.post("/api/v1/org/invites", json={"emails": [email], "role": role})
    assert r.status_code == 200, r.text
    (sent,) = r.json()["invites"]
    assert sent["link"].endswith("/invite/" + sent["token"])
    return str(sent["token"])


def join(bench: Bench, admin: TestClient, email: str, role: str) -> tuple[TestClient, str]:
    token = invite(admin, email, role)
    c = bench.client()
    r = c.post(f"/api/v1/auth/invites/{token}/accept", json={"name": email, "password": PW})
    signed(c, r)
    return c, str(r.json()["user"]["id"])


def login(c: TestClient, email: str, password: str = PW, **extra: Any) -> Any:
    return c.post("/api/v1/auth/login", json={"email": email, "password": password, **extra})


def wait_run(c: TestClient, predicate: Any, params: dict[str, str] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        data = c.get(f"/api/v1/sessions/{SESSION}", params=params).json()
        if data.get("run") is not None and predicate(data["run"]):
            return dict(data["run"])
        time.sleep(0.05)
    raise AssertionError("the run never reached the expected state")


def message(c: TestClient, text: str, **query: str) -> Any:
    return c.post(f"/api/v1/sessions/{SESSION}/messages", json={"text": text}, params=query)


# --- local mode and its claim ---------------------------------------------------------------


def test_local_mode_acts_as_the_local_owner_on_loopback_only(bench: Bench) -> None:
    local = bench.client(local=True)
    status = local.get("/api/v1/auth/status").json()
    assert status == {"mode": "local", "guests": False, "signup_open": True}
    me = local.get("/api/v1/auth/me").json()
    assert me["tenant"] == "local" and me["role"] == "owner" and me["user"] is None
    assert local.get("/api/v1/runs").status_code == 200
    remote = bench.client()
    assert remote.get("/api/v1/auth/status").json()["mode"] == "local"
    r = remote.get("/api/v1/runs")
    assert r.status_code == 401 and r.json()["error"]["code"] == "setup_required"


def test_local_mode_needs_a_loopback_host_name_too(bench: Bench) -> None:
    """A page on another name that resolves to 127.0.0.1 is not the local person."""
    rebound = bench.client(local=True, base_url="http://attacker.example:8765")
    r = rebound.get("/api/v1/runs")
    assert r.status_code == 401 and r.json()["error"]["code"] == "setup_required"


def test_a_local_mode_write_needs_the_csrf_token_or_the_cli_header(bench: Bench) -> None:
    local = bench.client(local=True)
    r = message(local, "Write a note")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    csrf = local.get("/api/v1/auth/me").json()["csrf"]
    r = local.post(f"/api/v1/sessions/{SESSION}/messages", json={"text": "Write a note"},
                   headers={"X-CSRF-Token": csrf})  # fmt: skip
    assert r.status_code == 200, r.text
    cli = local.get("/api/v1/runs", headers={"X-Aksha-User": "o1"})
    assert cli.json() == {"runs": []}  # the CLI's own --user, as before accounts


def test_the_claim_keeps_what_local_mode_did(bench: Bench) -> None:
    local = bench.client(local=True)
    local.headers["X-CSRF-Token"] = local.get("/api/v1/auth/me").json()["csrf"]
    assert message(local, "Write a note").status_code == 200
    wait_run(local, lambda r: r["state"] == "ended")
    remote = bench.client()
    body = {"email": "me@example.org", "password": PW, "name": "Me", "org_name": "Home"}
    r = remote.post("/api/v1/auth/claim", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "loopback_only"
    claimed = bench.client(local=True)
    signed(claimed, claimed.post("/api/v1/auth/claim", json=body))
    me = claimed.get("/api/v1/auth/me").json()
    assert me["tenant"] == "local" and me["role"] == "owner" and me["org"]["name"] == "Home"
    assert [r["run"] for r in claimed.get("/api/v1/runs").json()["runs"]] == ["R-0001"]
    assert claimed.post("/api/v1/auth/claim", json=body).json()["error"]["code"] == (
        "signup_closed"
    )
    anonymous = bench.client(local=True)
    r = anonymous.get("/api/v1/runs")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    assert anonymous.get("/api/v1/auth/status").json()["mode"] == "accounts"
    assert [a.action for a in bench.audit("claim")] == ["claim"]


# --- sign-up, sign-in and the cookie --------------------------------------------------------


def test_signup_works_once_then_closes(bench: Bench) -> None:
    owner = signup(bench)
    me = owner.get("/api/v1/auth/me").json()
    assert me["role"] == "owner" and me["tenant"] == me["user"]["id"]
    assert me["tenant"].startswith("u-") and me["is_guest"] is False
    again = bench.client().post(
        "/api/v1/auth/signup", json={"email": "b@example.org", "password": PW, "name": "B"}
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "signup_closed"
    assert bench.client().get("/api/v1/auth/status").json() == {
        "mode": "accounts", "guests": False, "signup_open": False,
    }  # fmt: skip
    assert len(bench.audit("signup")) == 1


def test_login_sets_the_cookie_flags_and_logout_ends_the_session(bench: Bench) -> None:
    signup(bench)
    c = bench.client()
    r = login(c, "OWNER@example.org", remember=True)
    cookie = r.headers["set-cookie"]
    assert r.status_code == 200 and cookie.startswith("aksha_session=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=1209600" in cookie and "Secure" not in cookie
    token = c.cookies["aksha_session"]
    signed(c, r)
    assert c.get("/api/v1/auth/me").status_code == 200
    assert c.post("/api/v1/auth/logout").status_code == 200
    replay = bench.client()
    replay.cookies.set("aksha_session", token)
    assert replay.get("/api/v1/auth/me").status_code == 401  # deleted server-side
    short = login(bench.client(), "owner@example.org", remember=False)
    assert "Max-Age" not in short.headers["set-cookie"]
    https = login(bench.client(base_url="https://aksha.example"), "owner@example.org")
    assert "Secure" in https.headers["set-cookie"]


def test_a_browser_session_lasts_twelve_hours_and_a_remembered_one_slides(bench: Bench) -> None:
    signup(bench)
    short, long = bench.client(), bench.client()
    signed(short, login(short, "owner@example.org", remember=False))
    signed(long, login(long, "owner@example.org", remember=True))
    bench.clock.t += 11 * 3600
    assert short.get("/api/v1/auth/me").status_code == 200
    assert long.get("/api/v1/auth/me").status_code == 200
    bench.clock.t += 2 * 3600
    assert short.get("/api/v1/auth/me").status_code == 401
    bench.clock.t += 13 * DAY  # 14 days after sign-in, but 13 after it was last used
    assert long.get("/api/v1/auth/me").status_code == 200
    bench.clock.t += 15 * DAY
    assert long.get("/api/v1/auth/me").status_code == 401


def test_the_secure_flag_can_be_forced(tmp_path: Path) -> None:
    b = Bench(tmp_path, cookie_secure=True)
    try:
        c = b.client()
        body = {"email": "o@example.org", "password": PW, "name": "O"}
        assert "Secure" in c.post("/api/v1/auth/signup", json=body).headers["set-cookie"]
    finally:
        b.close()


def test_an_unknown_email_and_a_wrong_password_read_the_same(bench: Bench) -> None:
    signup(bench)
    unknown = login(bench.client(), "nobody@example.org")
    wrong = login(bench.client(), "owner@example.org", "the wrong password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    assert len(bench.audit("sign_in_failed")) == 2


def test_five_failures_lock_the_email_from_that_address_for_fifteen_minutes(bench: Bench) -> None:
    signup(bench)
    c = bench.client()
    for _ in range(5):
        assert login(c, "owner@example.org", "the wrong password").status_code == 401
    r = login(c, "owner@example.org")
    assert r.status_code == 429 and r.json()["error"]["code"] == "throttled"
    elsewhere = bench.client(ip="198.51.100.9")
    assert login(elsewhere, "owner@example.org").status_code == 200
    bench.clock.t += 14 * 60
    assert login(c, "owner@example.org").status_code == 429
    bench.clock.t += 2 * 60
    assert login(c, "owner@example.org").status_code == 200


def test_four_failures_do_not_lock(bench: Bench) -> None:
    signup(bench)
    c = bench.client()
    for _ in range(4):
        login(c, "owner@example.org", "the wrong password")
    assert login(c, "owner@example.org").status_code == 200


# --- CSRF -----------------------------------------------------------------------------------


def test_a_cookie_write_needs_the_csrf_header_and_a_bearer_write_does_not(bench: Bench) -> None:
    owner = signup(bench)
    csrf = owner.headers.pop("X-CSRF-Token")
    r = message(owner, "Write a note")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = owner.post(f"/api/v1/sessions/{SESSION}/messages", json={"text": "Write a note"},
                   headers={"X-CSRF-Token": "not-the-token"})  # fmt: skip
    assert r.status_code == 403
    assert owner.get("/api/v1/runs").status_code == 200  # a read needs no token
    owner.headers["X-CSRF-Token"] = csrf
    secret = owner.post("/api/v1/tokens", json={"name": "cli"}).json()["secret"]
    bearer = bench.client()
    bearer.headers["Authorization"] = f"Bearer {secret}"
    assert message(bearer, "Write a note").status_code == 200


@pytest.mark.parametrize("route", ["/api/v1/auth/claim", "/api/v1/auth/signup"])
def test_a_text_plain_post_to_an_unauthenticated_route_is_refused(bench: Bench, route: str) -> None:
    """A cross-site ``no-cors`` post sends ``text/plain``: it never reaches the route."""
    local = bench.client(local=True)
    body = json.dumps({"email": "x@example.org", "password": PW, "name": "X", "org_name": "X"})
    for media in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = local.post(route, content=body, headers={"Content-Type": media})
        assert r.status_code == 415, (media, r.text)
        assert r.json()["error"]["code"] == "unsupported_media_type"
    assert local.post(route, content=body).status_code == 415  # no type at all
    assert not bench.accounts.has_accounts()
    assert local.get("/api/v1/auth/status").json()["mode"] == "local"
    r = local.post(route, content=body, headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200, r.text


def test_an_unsafe_request_from_another_origin_is_refused(bench: Bench) -> None:
    local = bench.client(local=True)
    body = {"email": "x@example.org", "password": PW, "name": "X", "org_name": "X"}
    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999", "http://localhost:8765"):
        r = local.post("/api/v1/auth/claim", json=body, headers={"Origin": origin})
        assert r.status_code == 403, (origin, r.text)
        assert r.json()["error"]["code"] == "cross_origin"
    assert not bench.accounts.has_accounts()
    read = local.get("/api/v1/auth/status", headers={"Origin": "http://evil.example"})
    assert read.status_code == 200  # a read is not refused
    same = local.post("/api/v1/auth/claim", json=body, headers={"Origin": "http://127.0.0.1:8765"})
    owner = signed(local, same)
    r = message(owner, "Write a note")
    assert r.status_code == 200
    r = owner.post(f"/api/v1/sessions/{SESSION}/messages", json={"text": "Again"},
                   headers={"Origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403 and r.json()["error"]["code"] == "cross_origin"


def test_a_json_upload_over_the_cap_is_refused_too_large(tmp_path: Path) -> None:
    bench = Bench(tmp_path, upload_max_bytes=256)
    try:
        owner = signup(bench)
        r = owner.post("/api/v1/inputs", json={"files": {"notes/big.md": "x" * 512}})
        assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    finally:
        bench.close()


def test_a_multipart_body_is_taken_only_by_the_upload_route(bench: Bench) -> None:
    owner = signup(bench)
    r = owner.post(f"/api/v1/sessions/{SESSION}/messages", data={"text": "Write a note"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"
    r = owner.post("/api/v1/inputs/upload", files=[("file", ("a.md", b"# A", "text/markdown"))])
    assert r.status_code != 415


# --- invites --------------------------------------------------------------------------------


def test_an_invite_previews_and_its_acceptance_signs_in(bench: Bench) -> None:
    owner = signup(bench)
    token = invite(owner, "m@example.org", "member")
    preview = bench.client().get(f"/api/v1/auth/invites/{token}").json()
    assert preview["state"] == "valid" and preview["role"] == "member"
    assert preview["email"] == "m@example.org" and preview["org"]["name"] == "Acme"
    assert preview["inviter"]["name"] == "Owner"
    listed = owner.get("/api/v1/org/invites").json()["invites"]
    assert listed[0]["invited_by"] == {"name": "Owner", "email": preview["inviter"]["email"]}
    member = bench.client()
    r = member.post(f"/api/v1/auth/invites/{token}/accept", json={"name": "M", "password": PW})
    signed(member, r)
    me = member.get("/api/v1/auth/me").json()
    assert me["role"] == "member" and me["user"]["email"] == "m@example.org"
    assert me["tenant"] == me["user"]["id"] != owner.get("/api/v1/auth/me").json()["tenant"]
    assert bench.client().get(f"/api/v1/auth/invites/{token}").json()["state"] == "used"
    again = bench.client().post(f"/api/v1/auth/invites/{token}/accept",
                                json={"name": "X", "password": PW})  # fmt: skip
    assert again.status_code == 409 and again.json()["error"]["code"] == "invite_used"
    members = owner.get("/api/v1/org/members").json()["members"]
    assert sorted(m["role"] for m in members) == ["member", "owner"]
    assert [a.action for a in bench.audit("invite_accepted")] == ["invite_accepted"]


def test_an_expired_or_revoked_invite_cannot_be_accepted(bench: Bench) -> None:
    owner = signup(bench)
    old = invite(owner, "late@example.org", "viewer")
    bench.clock.t += 7 * DAY + 1
    assert bench.client().get(f"/api/v1/auth/invites/{old}").json()["state"] == "expired"
    r = bench.client().post(f"/api/v1/auth/invites/{old}/accept",
                            json={"name": "L", "password": PW})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "invite_expired"
    revoked = invite(owner, "gone@example.org", "member")
    (row,) = [i for i in owner.get("/api/v1/org/invites").json()["invites"]
              if i["email"] == "gone@example.org"]  # fmt: skip
    assert "token" not in row and row["state"] == "valid"
    assert owner.delete(f"/api/v1/org/invites/{row['id']}").status_code == 200
    assert bench.client().get(f"/api/v1/auth/invites/{revoked}").json()["state"] == "revoked"
    r = bench.client().post(f"/api/v1/auth/invites/{revoked}/accept",
                            json={"name": "G", "password": PW})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "invite_revoked"
    assert len(bench.audit("invite_revoked")) == 1


def test_a_resend_issues_a_new_link_and_retires_the_old_one(bench: Bench) -> None:
    owner = signup(bench)
    old = invite(owner, "m@example.org", "member")
    (row,) = owner.get("/api/v1/org/invites").json()["invites"]
    bench.clock.t += 6 * DAY
    r = owner.post(f"/api/v1/org/invites/{row['id']}/resend")
    assert r.status_code == 200, r.text
    new = r.json()["invite"]["token"]
    assert new != old
    assert bench.client().get(f"/api/v1/auth/invites/{old}").status_code == 404
    bench.clock.t += 2 * DAY  # past the first expiry, inside the second
    assert bench.client().get(f"/api/v1/auth/invites/{new}").json()["state"] == "valid"


def test_only_an_admin_invites_and_nobody_invites_an_owner(bench: Bench) -> None:
    owner = signup(bench)
    member, _ = join(bench, owner, "m@example.org", "member")
    r = member.post("/api/v1/org/invites", json={"emails": ["x@example.org"], "role": "viewer"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role"
    r = owner.post("/api/v1/org/invites", json={"emails": ["x@example.org"], "role": "owner"})
    assert r.status_code == 422
    r = owner.post("/api/v1/org/invites", json={"emails": ["M@example.org"], "role": "viewer"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


# --- the role matrix ------------------------------------------------------------------------


@pytest.fixture
def org(bench: Bench) -> dict[str, Any]:
    owner = signup(bench)
    admin, admin_id = join(bench, owner, "admin@example.org", "admin")
    m1, m1_id = join(bench, owner, "m1@example.org", "member")
    m2, m2_id = join(bench, owner, "m2@example.org", "member")
    viewer, viewer_id = join(bench, owner, "viewer@example.org", "viewer")
    return {
        "owner": owner,
        "admin": admin,
        "m1": m1,
        "m2": m2,
        "viewer": viewer,
        "admin_id": admin_id,
        "m1_id": m1_id,
        "m2_id": m2_id,
        "viewer_id": viewer_id,
    }


def test_a_viewer_reads_but_never_commands(bench: Bench, org: dict[str, Any]) -> None:
    viewer = org["viewer"]
    assert viewer.get("/api/v1/runs").json() == {"runs": []}
    for path, body in [
        (f"/api/v1/sessions/{SESSION}/messages", {"text": "Write a note"}),
        ("/api/v1/commands", {"command": "Stop", "args": {"run_id": "R-0001"}}),
        ("/api/v1/runs/R-0001/stop", {}),
        ("/api/v1/inputs", {"files": {"a.md": "# A\n"}}),
        ("/api/v1/approvals/K-0001/approve", {"shown_signature": "x"}),
        ("/api/v1/interrupts/I-0001/answer", {"text": "blue"}),
        ("/api/v1/runs/R-0001/steer", {"text": "x"}),
    ]:
        r = viewer.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role", path


def test_a_member_reads_only_their_own_work(bench: Bench, org: dict[str, Any]) -> None:
    m1, m2 = org["m1"], org["m2"]
    assert message(m1, "Ask first please").status_code == 200
    wait_run(m1, lambda r: r["state"] == "waiting")
    assert m2.get("/api/v1/runs").json() == {"runs": []}
    assert m2.get("/api/v1/runs/R-0001").status_code == 404
    r = m2.get("/api/v1/runs", params={"as_user": org["m1_id"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role"
    own = m1.get("/api/v1/runs", params={"as_user": org["m1_id"]})
    assert [x["run"] for x in own.json()["runs"]] == ["R-0001"]
    assert bench.audit("read_session_as_admin") == []


def test_an_admin_reads_a_members_work_and_each_read_is_audited(
    bench: Bench, org: dict[str, Any]
) -> None:
    admin, m1 = org["admin"], org["m1"]
    as_m1 = {"as_user": org["m1_id"]}
    assert message(m1, "Ask first please").status_code == 200
    wait_run(m1, lambda r: r["state"] == "waiting")
    before = len(bench.audit("read_session_as_admin"))
    runs = admin.get("/api/v1/runs", params=as_m1).json()["runs"]
    assert [r["run"] for r in runs] == ["R-0001"]
    assert admin.get("/api/v1/runs/R-0001", params=as_m1).status_code == 200
    assert admin.get(f"/api/v1/sessions/{SESSION}", params=as_m1).json()["run"] is not None
    records = bench.audit("read_session_as_admin")
    assert len(records) == before + 3
    assert {(r.actor_id, r.target) for r in records} == {(org["admin_id"], org["m1_id"])}
    assert records[0].detail["path"] == f"/api/v1/sessions/{SESSION}"  # newest first
    r = admin.get("/api/v1/runs", params={"as_user": "u-not-a-member"})
    assert r.status_code == 404
    assert org["owner"].get("/api/v1/runs", params=as_m1).status_code == 200


def test_an_admin_stops_a_members_run_but_never_answers_or_approves(
    bench: Bench, org: dict[str, Any]
) -> None:
    admin, m1 = org["admin"], org["m1"]
    as_m1 = {"as_user": org["m1_id"]}
    assert message(m1, "Ask first please").status_code == 200
    run = wait_run(m1, lambda r: r["state"] == "waiting")
    (waiting,) = run["waiting_on"]
    for path, body in [
        (f"/api/v1/interrupts/{waiting['interrupt_id']}/answer", {"answers": {"q1": "x"}}),
        ("/api/v1/approvals/K-0001/approve", {"shown_signature": "x"}),
        ("/api/v1/approvals/K-0001/reject", {}),
        (f"/api/v1/runs/{run['run']}/steer", {"text": "x"}),
        (f"/api/v1/sessions/{SESSION}/messages", {"text": "x"}),
        ("/api/v1/commands", {"command": "Stop", "args": {"run_id": run["run"]}}),
        ("/api/v1/inputs", {"files": {"a.md": "# A\n"}}),
    ]:
        r = admin.post(path, json=body, params=as_m1)
        assert r.status_code == 403 and r.json()["error"]["code"] == "as_user_refused", path
    mine = admin.post(f"/api/v1/interrupts/{waiting['interrupt_id']}/answer",
                      json={"answers": {"q1": "x"}})  # fmt: skip
    # In the admin's own tenant there is no such question: refused, never answered.
    assert mine.status_code in (404, 409), mine.text
    assert mine.json()["error"]["details"]["status"] == "refused", mine.text
    r = m1.post(f"/api/v1/runs/{run['run']}/stop", params={"as_user": org["m2_id"]})
    assert r.status_code == 403
    assert bench.audit("stop_run_as_admin") == []
    r = admin.post(f"/api/v1/runs/{run['run']}/stop", params=as_m1, json={"reason": "cost"})
    assert r.status_code == 200, r.text
    (record,) = bench.audit("stop_run_as_admin")
    assert record.actor_id == org["admin_id"] and record.target == org["m1_id"]
    assert record.detail["run"] == run["run"]
    ended = wait_run(m1, lambda r: r["state"] == "ended")
    assert ended["outcome"] == "stopped"


def test_the_audit_log_is_for_admins(bench: Bench, org: dict[str, Any]) -> None:
    assert org["m1"].get("/api/v1/org/audit").status_code == 403
    records = org["admin"].get("/api/v1/org/audit").json()["records"]
    actions = [r["action"] for r in records]
    assert actions[-1] == "signup" and "invite_accepted" in actions  # newest first
    page = org["admin"].get("/api/v1/org/audit", params={"limit": 2}).json()["records"]
    rest = org["admin"].get("/api/v1/org/audit",
                            params={"before": page[-1]["id"], "action": "invite_sent"})  # fmt: skip
    assert len(page) == 2 and all(r["action"] == "invite_sent" for r in rest.json()["records"])
    assert all(r["id"] < page[-1]["id"] for r in rest.json()["records"])
    mine = org["admin"].get("/api/v1/org/audit", params={"actor": org["m1_id"]}).json()
    assert {r["actor"] for r in mine["records"]} == {org["m1_id"]}


def test_role_changes_respect_the_owner(bench: Bench, org: dict[str, Any]) -> None:
    owner, admin = org["owner"], org["admin"]
    owner_id = owner.get("/api/v1/auth/me").json()["user"]["id"]
    r = admin.patch(f"/api/v1/org/members/{org['m1_id']}", json={"role": "owner"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "owner_only"
    r = admin.patch(f"/api/v1/org/members/{owner_id}", json={"role": "member"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "owner_only"
    r = admin.patch(f"/api/v1/org/members/{owner_id}", json={"active": False})
    assert r.status_code == 403
    r = org["m2"].patch(f"/api/v1/org/members/{org['m1_id']}", json={"role": "viewer"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role"
    r = admin.patch(f"/api/v1/org/members/{org['m1_id']}", json={"role": "viewer"})
    assert r.status_code == 200 and r.json()["member"]["role"] == "viewer"
    assert message(org["m1"], "Write a note").status_code == 403
    r = owner.patch(f"/api/v1/org/members/{org['admin_id']}", json={"role": "owner"})
    assert r.status_code == 200
    roles = {m["id"]: m["role"] for m in owner.get("/api/v1/org/members").json()["members"]}
    assert roles[org["admin_id"]] == "owner" and roles[owner_id] == "admin"
    changed = bench.audit("role_changed")[::-1]  # the store answers newest first
    assert [c.detail["role"] for c in changed] == ["viewer", "owner", "admin"]


def test_deactivation_revokes_sessions_and_tokens(bench: Bench, org: dict[str, Any]) -> None:
    m1 = org["m1"]
    secret = m1.post("/api/v1/tokens", json={"name": "laptop"}).json()["secret"]
    bearer = bench.client()
    bearer.headers["Authorization"] = f"Bearer {secret}"
    assert bearer.get("/api/v1/auth/me").status_code == 200
    r = org["admin"].patch(f"/api/v1/org/members/{org['m1_id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["member"]["active"] is False
    assert m1.get("/api/v1/auth/me").status_code == 401
    assert bearer.get("/api/v1/auth/me").status_code == 401
    r = login(bench.client(), "m1@example.org")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    assert len(bench.audit("user_deactivated")) == 1
    org["admin"].patch(f"/api/v1/org/members/{org['m1_id']}", json={"active": True})
    assert login(bench.client(), "m1@example.org").status_code == 200


def test_a_password_change_signs_out_every_other_session(bench: Bench) -> None:
    owner = signup(bench)
    other = bench.client()
    signed(other, login(other, "owner@example.org"))
    r = owner.post("/api/v1/auth/password", json={"current": "wrong one!", "new": "new pass 22"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = owner.post("/api/v1/auth/password", json={"current": PW, "new": "new pass 22"})
    signed(owner, r)
    assert other.get("/api/v1/auth/me").status_code == 401
    assert owner.get("/api/v1/auth/me").status_code == 200
    assert login(bench.client(), "owner@example.org").status_code == 401
    assert login(bench.client(), "owner@example.org", "new pass 22").status_code == 200


# --- API tokens -----------------------------------------------------------------------------


def test_an_api_token_is_shown_once_tracked_and_revocable(bench: Bench) -> None:
    owner = signup(bench)
    r = owner.post("/api/v1/tokens", json={"name": "cli"})
    assert r.status_code == 200
    secret, token_id = r.json()["secret"], r.json()["token"]["id"]
    assert secret.startswith("aksha_pat_")
    (listed,) = owner.get("/api/v1/tokens").json()["tokens"]
    assert listed["name"] == "cli" and listed["last_used_at"] is None and "secret" not in listed
    bearer = bench.client()
    bearer.headers["Authorization"] = f"Bearer {secret}"
    assert bearer.get("/api/v1/auth/me").json()["role"] == "owner"
    (listed,) = owner.get("/api/v1/tokens").json()["tokens"]
    assert listed["last_used_at"] is not None
    other, _ = join(bench, owner, "m@example.org", "member")
    assert other.delete(f"/api/v1/tokens/{token_id}").status_code == 404
    assert owner.delete(f"/api/v1/tokens/{token_id}").status_code == 200
    r = bearer.get("/api/v1/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    assert [a.action for a in bench.audit("token_created")] == ["token_created"]
    assert [a.action for a in bench.audit("token_revoked")] == ["token_revoked"]
    wrong = bench.client()
    wrong.headers["Authorization"] = "Bearer aksha_pat_made-up"
    assert wrong.get("/api/v1/runs").status_code == 401


# --- guests ---------------------------------------------------------------------------------


def test_guests_are_off_by_default(bench: Bench) -> None:
    signup(bench)
    r = bench.client().post("/api/v1/auth/guest")
    assert r.status_code == 403 and r.json()["error"]["code"] == "guests_disabled"


def test_a_guest_works_in_their_own_tenant_then_saves_the_account(guest_bench: Bench) -> None:
    bench = guest_bench
    owner = signup(bench)
    assert bench.client().get("/api/v1/auth/status").json()["guests"] is True
    guest = bench.client()
    signed(guest, guest.post("/api/v1/auth/guest"))
    me = guest.get("/api/v1/auth/me").json()
    assert me["is_guest"] is True and me["role"] == "owner" and me["tenant"].startswith("g-")
    assert me["guest_expires_at"] is not None and me["org"]["name"] != "Acme"
    assert message(guest, "Write a note").status_code == 200
    wait_run(guest, lambda r: r["state"] == "ended")
    r = guest.post("/api/v1/auth/guest/save",
                   json={"email": "g@example.org", "password": PW, "name": "G"})  # fmt: skip
    signed(guest, r)
    saved = guest.get("/api/v1/auth/me").json()
    assert saved["is_guest"] is False and saved["tenant"] == me["tenant"]
    assert saved["guest_expires_at"] is None and saved["role"] == "owner"
    assert saved["org"] == me["org"]  # the personal org is kept; the main org is not joined
    assert [x["run"] for x in guest.get("/api/v1/runs").json()["runs"]] == ["R-0001"]
    assert len(bench.audit("guest_created")) == len(bench.audit("guest_saved")) == 1
    assert owner.post("/api/v1/auth/guest/save",
                      json={"email": "z@example.org", "password": PW, "name": "Z"}
                      ).json()["error"]["code"] == "not_a_guest"  # fmt: skip


def test_a_saved_guest_never_joins_the_main_org_or_sees_its_members(guest_bench: Bench) -> None:
    bench = guest_bench
    owner = signup(bench)
    _, member_id = join(bench, owner, "m@example.org", "member")
    guest = bench.client()
    signed(guest, guest.post("/api/v1/auth/guest"))
    body = {"email": "g@example.org", "password": PW, "name": "G"}
    signed(guest, guest.post("/api/v1/auth/guest/save", json=body))
    gid = guest.get("/api/v1/auth/me").json()["user"]["id"]
    main = owner.get("/api/v1/org/members").json()["members"]
    assert {m["email"] for m in main} == {"owner@example.org", "m@example.org"}
    mine = guest.get("/api/v1/org/members").json()["members"]
    assert [m["id"] for m in mine] == [gid]
    assert guest.get("/api/v1/runs", params={"as_user": member_id}).status_code == 404
    again = bench.client()
    assert login(again, "g@example.org").status_code == 200
    assert {m["email"] for m in signed(again, login(again, "g@example.org"))
            .get("/api/v1/org/members").json()["members"]} == {"g@example.org"}  # fmt: skip


def test_guests_never_see_each_other(guest_bench: Bench) -> None:
    signup(guest_bench)
    a, b = guest_bench.client(ip="198.51.100.1"), guest_bench.client(ip="198.51.100.2")
    signed(a, a.post("/api/v1/auth/guest"))
    signed(b, b.post("/api/v1/auth/guest"))
    a_me, b_me = a.get("/api/v1/auth/me").json(), b.get("/api/v1/auth/me").json()
    assert a_me["org"]["id"] != b_me["org"]["id"]
    assert [m["id"] for m in a.get("/api/v1/org/members").json()["members"]] == [a_me["user"]["id"]]
    assert a.get("/api/v1/runs", params={"as_user": b_me["user"]["id"]}).status_code == 404
    r = a.post("/api/v1/org/invites", json={"emails": ["x@example.org"], "role": "member"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "guest_forbidden"


def test_guest_creation_is_limited_per_address(guest_bench: Bench) -> None:
    signup(guest_bench)
    one = guest_bench.client(ip="198.51.100.9")
    for _ in range(5):
        assert one.post("/api/v1/auth/guest").status_code == 200
    r = one.post("/api/v1/auth/guest")
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_guests"
    assert guest_bench.client(ip="198.51.100.10").post("/api/v1/auth/guest").status_code == 200
    guest_bench.clock.t += 3601
    assert one.post("/api/v1/auth/guest").status_code == 200


def test_an_expired_guest_is_signed_out(guest_bench: Bench) -> None:
    signup(guest_bench)
    guest = guest_bench.client()
    signed(guest, guest.post("/api/v1/auth/guest"))
    guest_bench.clock.t += DAY + 1
    assert guest.get("/api/v1/auth/me").status_code == 401


def test_a_guest_cannot_be_saved_before_the_server_has_an_owner(guest_bench: Bench) -> None:
    guest = guest_bench.client()
    signed(guest, guest.post("/api/v1/auth/guest"))
    r = guest.post("/api/v1/auth/guest/save",
                   json={"email": "g@example.org", "password": PW, "name": "G"})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "setup_required"


# --- secrets at rest ------------------------------------------------------------------------


def _every_value(path: Path) -> list[str]:
    """Every cell of every table of the accounts database, as text, read past the service."""
    db = sqlite3.connect(path)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return [str(cell) for t in tables for row in db.execute(f'SELECT * FROM "{t}"')
                for cell in row if cell is not None]  # fmt: skip
    finally:
        db.close()


def test_only_digests_of_the_cookie_invite_and_api_token_are_stored(bench: Bench) -> None:
    owner = signup(bench)
    again = bench.client()
    assert login(again, "owner@example.org").status_code == 200
    cookies = [c.cookies[COOKIE] for c in (owner, again)]
    link = owner.post("/api/v1/org/invites", json={"emails": ["m@example.org"], "role": "member"})
    (sent,) = link.json()["invites"]
    first = sent["link"].rsplit("/", 1)[-1]
    resent = owner.post(f"/api/v1/org/invites/{sent['id']}/resend").json()["invite"]["token"]
    other = owner.post("/api/v1/org/invites", json={"emails": ["v@example.org"], "role": "viewer"})
    (kept,) = other.json()["invites"]
    api = owner.post("/api/v1/tokens", json={"name": "cli"}).json()["secret"]
    secrets = [*cookies, first, resent, kept["token"], api]
    assert all(len(x) >= 32 for x in secrets) and len(set(secrets)) == len(secrets)
    values = _every_value(bench.accounts.store.path)
    for secret in secrets:
        assert not [v for v in values if secret in v], "a raw secret is stored"
    for secret in (*cookies, resent, kept["token"], api):  # the live ones, by their digest
        assert digest(secret) in values
    bearer = bench.client()
    bearer.headers["Authorization"] = f"Bearer {api}"
    assert bearer.get("/api/v1/auth/me").status_code == 200  # and they still resolve


# --- who a command is by --------------------------------------------------------------------


def test_an_accounts_command_is_recorded_as_its_user_and_another_by_is_refused(
    bench: Bench,
) -> None:
    owner = signup(bench)
    me = owner.get("/api/v1/auth/me").json()
    user, tenant = f"user:{me['user']['id']}", me["tenant"]
    assert message(owner, "Ask first please").status_code == 200
    run = wait_run(owner, lambda r: r["state"] == "waiting")
    (waiting,) = run["waiting_on"]
    answer = f"/api/v1/interrupts/{waiting['interrupt_id']}/answer"
    for body in ({"answers": {"q1": "x"}, "by": "agent:scribe"},
                 {"answers": {"q1": "x"}, "by": "user:u-someone-else"}):  # fmt: skip
        r = owner.post(answer, json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "by_not_allowed", r.text
    r = owner.post("/api/v1/commands", json={"command": "Stop", "args": {"run_id": run["run"]},
                                             "by": "agent:scribe"})  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "by_not_allowed"
    assert bench.payloads(tenant, "interrupt.resolved") == []
    r = owner.post(answer, json={"answers": {"q1": "x"}, "by": "person"})  # the CLI's default
    assert r.status_code == 200, r.text
    assert [p["by"] for p in bench.payloads(tenant, "interrupt.resolved")] == [user]
    wait_run(owner, lambda r: r["state"] == "ended")
    assert message(owner, "Ask first again").status_code == 200
    second = wait_run(owner, lambda r: r["state"] == "waiting" and r["run"] != run["run"])
    r = owner.post("/api/v1/commands", json={"command": "Stop", "args": {"run_id": second["run"]}})
    assert r.status_code == 200, r.text
    assert [p["by"] for p in bench.payloads(tenant, "run.stop_requested")] == [user]


def test_a_local_mode_command_is_recorded_as_local(bench: Bench) -> None:
    cli = bench.client(local=True)
    cli.headers["X-Aksha-User"] = "local"
    assert message(cli, "Ask first please").status_code == 200
    run = wait_run(cli, lambda r: r["state"] == "waiting")
    r = cli.post(f"/api/v1/runs/{run['run']}/stop", json={"by": "ops"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "by_not_allowed"
    assert cli.post(f"/api/v1/runs/{run['run']}/stop", json={}).status_code == 200
    assert [p["by"] for p in bench.payloads("local", "run.stop_requested")] == ["local"]


def test_the_trusted_header_mode_keeps_the_bodys_by(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()
    app = create_app(runtime, lambda: open_reader(cfg), dict,
                     auth=AuthConfig(trust_user_header=True))  # fmt: skip
    h = {"X-Aksha-User": "o7"}
    try:
        with TestClient(app) as c:
            r = c.post(f"/api/v1/sessions/{SESSION}/messages", headers=h,
                       json={"text": "Ask first please"})  # fmt: skip
            assert r.status_code == 200
            run = wait_run(TestClient(app, headers=h), lambda r: r["state"] == "waiting")
            r = c.post(f"/api/v1/runs/{run['run']}/stop", headers=h, json={"by": "ops"})
            assert r.status_code == 200, r.text
        store = open_reader(cfg)
        try:
            events = load_view(store, "o7").events
        finally:
            store.close()
        assert [e.payload["by"] for e in events if e.type == "run.stop_requested"] == ["ops"]
    finally:
        app.state.closing.set()
        runtime.stop()


# --- the trusted header switch --------------------------------------------------------------


def test_the_trusted_header_mode_reports_itself(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()
    app = create_app(runtime, lambda: open_reader(cfg), dict,
                     auth=AuthConfig(trust_user_header=True))  # fmt: skip
    try:
        with TestClient(app) as c:
            assert c.get("/api/v1/auth/status").json()["mode"] == "trusted"
            me = c.get("/api/v1/auth/me", headers={"X-Aksha-User": "o7"}).json()
            assert me["tenant"] == "o7" and me["role"] == "owner"
            r = c.get("/api/v1/runs", params={"as_user": "u-1"}, headers={"X-Aksha-User": "o7"})
            assert r.status_code == 403
    finally:
        app.state.closing.set()
        runtime.stop()


def test_real_auth_needs_an_accounts_store() -> None:
    with pytest.raises(ValueError, match="accounts"):
        AuthConfig(trust_user_header=False).check()


def test_the_reads_router_refuses_writes_from_a_viewer_and_an_admin_acting_as_someone(
    bench: Bench, org: dict[str, Any]
) -> None:
    """Reads take ``as_user`` for an Admin; the reads router's writes never do."""
    viewer, admin, m1_id = org["viewer"], org["admin"], org["m1_id"]
    assert viewer.get("/api/v1/sessions").status_code == 200
    assert admin.get("/api/v1/sessions", params={"as_user": m1_id}).status_code == 200
    writes = [
        (f"/api/v1/sessions/{SESSION}/archive", None),
        (f"/api/v1/sessions/{SESSION}/restore", None),
        ("/api/v1/reviews/I-0001/accept", {"card_hash": "x"}),
        ("/api/v1/reviews/I-0001/reject", {"card_hash": "x"}),
    ]
    for path, body in writes:
        r = viewer.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role", path
        r = admin.post(path, json=body, params={"as_user": m1_id})
        assert r.status_code == 403 and r.json()["error"]["code"] == "as_user_refused", path
    r = viewer.patch(f"/api/v1/sessions/{SESSION}", json={"title": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role"
    r = viewer.post("/api/v1/inputs/upload", files={"file": ("a.md", b"# A\n", "text/markdown")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_role"
