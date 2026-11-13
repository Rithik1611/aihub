"""End-to-end verification of aihub's authentication and authorisation.

Exercises the running services the way a browser and an attacker would, rather
than the way the unit tests do: real cableCORE tokens, real cookies, real CORS
preflights. Unit tests prove the logic in isolation; this proves the deployment.

Usage:

    export VERIFY_USER=... VERIFY_PASSWORD=...              # a cableAI-licensed account
    export VERIFY_UNLICENSED_USER=... VERIFY_UNLICENSED_PASSWORD=...   # optional
    export VERIFY_ALT_PLANT_ID=...                          # optional, for the plant switch
    uv run python scripts/verify_auth.py

Credentials come from the environment only - never commit them here.

The script forges tokens with the configured JWT secret to reach cases a real
login cannot produce (expired, tampered, unsigned, claims missing). It therefore
only makes sense against a development deployment.

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import httpx
import jwt

# Run from anywhere: the app package lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings

AIHUB = os.environ.get("AIHUB_URL", "http://127.0.0.1:8010")
CABLECORE = os.environ.get("CABLECORE_URL", "http://localhost:5000")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3001")
TIMEOUT = httpx.Timeout(30.0)

SESSIONS = "/api/chat/sessions"
SEND = "/api/chat/send"

# Sessions this run created, deleted before it exits: a verification script must not
# leave rows behind in the database it is verifying.
_created: list[str] = []


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def remember(response: httpx.Response) -> httpx.Response:
    """Track a session this run created so it can be cleaned up."""
    if response.status_code == 200:
        session_id = response.json().get("data", {}).get("sessionId")
        if session_id:
            _created.append(str(session_id))
    return response


async def cleanup(client: httpx.AsyncClient, token: str, report: Report) -> None:
    removed = 0
    for session_id in _created:
        response = await client.delete(f"{AIHUB}/api/chat/sessions/{session_id}", headers=bearer(token))
        removed += 1 if response.status_code == 200 else 0
    report.record("cleanup", "sessions created by this run removed", len(_created), removed)


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    expected: str
    actual: str
    outcome: Outcome


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def record(self, group: str, name: str, expected: object, actual: object) -> None:
        outcome = Outcome.PASS if expected == actual else Outcome.FAIL
        self.checks.append(Check(group, name, str(expected), str(actual), outcome))

    def skip(self, group: str, name: str, why: str) -> None:
        self.checks.append(Check(group, name, "-", why, Outcome.SKIP))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.outcome is Outcome.FAIL]


def body(message: str = "verification probe") -> dict[str, object]:
    return {"message": message, "mode": "DEFAULT", "sessionId": None, "newSessionId": None}


async def sign_in(client: httpx.AsyncClient, user: str, password: str) -> str:
    """A real cableCORE access token, via the plain-password endpoint."""
    response = await client.post(f"{CABLECORE}/auth/token", json={"username": user, "password": password})
    response.raise_for_status()
    token: str = response.json()["data"]["accessToken"]
    return token


def claims_of(token: str) -> dict[str, object]:
    decoded: dict[str, object] = jwt.decode(token, options={"verify_signature": False})
    return decoded


def forge(**overrides: object) -> str:
    """A token signed with the real secret, so only the claims under test differ."""
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "username": "forged",
        "role": "DESIGNER",
        "type": "access",
        "activePlantId": str(uuid.uuid4()),
        "canAccessAi": True,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    payload.update(overrides)
    for key, value in list(payload.items()):
        if value is None:
            del payload[key]
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)


async def check_authentication(client: httpx.AsyncClient, report: Report, token: str) -> None:
    group = "authentication"
    cases: list[tuple[str, dict[str, str], int]] = [
        ("no credentials", {}, 401),
        ("valid bearer token", bearer(token), 200),
        ("bearer with wrong scheme", {"Authorization": f"Token {token}"}, 401),
        ("garbage bearer token", {"Authorization": "Bearer not-a-jwt"}, 401),
        ("bearer with no token", {"Authorization": "Bearer"}, 401),
    ]
    for name, headers, expected in cases:
        response = await client.get(f"{AIHUB}{SESSIONS}", headers=headers)
        report.record(group, name, expected, response.status_code)

    forged: list[tuple[str, str, int]] = [
        ("refresh token refused", forge(type="refresh"), 401),
        ("token with no type", forge(type=None), 401),
        (
            "expired beyond leeway",
            forge(exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())),
            401,
        ),
        (
            "expired within clock skew",
            forge(exp=int((datetime.now(UTC) - timedelta(seconds=5)).timestamp())),
            200,
        ),
        ("no expiry claim", forge(exp=None), 401),
        ("no subject claim", forge(sub=None), 401),
        ("subject is not a uuid", forge(sub="not-an-account"), 401),
    ]
    for name, candidate, expected in forged:
        response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(candidate))
        report.record(group, name, expected, response.status_code)

    tampered = token[:-6] + ("aaaaaa" if not token.endswith("aaaaaa") else "bbbbbb")
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(tampered))
    report.record(group, "tampered signature", 401, response.status_code)

    unsigned = jwt.encode(claims_of(token), key="", algorithm="none")
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(unsigned))
    report.record(group, "unsigned token (alg=none)", 401, response.status_code)

    wrong_secret = jwt.encode(claims_of(token), "not-the-real-secret", algorithm="HS256")
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(wrong_secret))
    report.record(group, "signed with another secret", 401, response.status_code)


async def check_licence(client: httpx.AsyncClient, report: Report, unlicensed_token: str | None) -> None:
    group = "ai licence"
    cases: list[tuple[str, str, int]] = [
        ("no activePlantId claim", forge(activePlantId=None), 403),
        ("canAccessAi false", forge(canAccessAi=False), 403),
        ("claims absent entirely", forge(activePlantId=None, canAccessAi=None), 403),
        ("licensed caller", forge(), 200),
    ]
    for name, candidate, expected in cases:
        response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(candidate))
        report.record(group, name, expected, response.status_code)

    if unlicensed_token is None:
        report.skip(group, "real unlicensed account", "VERIFY_UNLICENSED_USER not set")
        return
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(unlicensed_token))
    report.record(group, "real unlicensed account", 403, response.status_code)
    core = await client.post(
        f"{CABLECORE}/chat/send",
        headers=bearer(unlicensed_token),
        json={"message": "probe", "mode": "DEFAULT"},
    )
    report.record(group, "cableCORE agrees it is unlicensed", 403, core.status_code)


async def check_cross_site(client: httpx.AsyncClient, report: Report, token: str) -> None:
    """Cookie-authenticated writes must come from an allowed origin as JSON."""
    group = "cross-site"
    cookies = {"access_token": token}

    allowed = {"Origin": ALLOWED_ORIGIN, "Content-Type": "application/json"}
    response = remember(await client.post(f"{AIHUB}{SEND}", cookies=cookies, headers=allowed, json=body()))
    report.record(group, "cookie POST from allowed origin", 200, response.status_code)

    refused: list[tuple[str, dict[str, str]]] = [
        ("foreign origin", {"Origin": "https://evil.example", "Content-Type": "application/json"}),
        ("no origin header", {"Content-Type": "application/json"}),
        ("null origin", {"Origin": "null", "Content-Type": "application/json"}),
        (
            "allowed host, wrong port",
            {"Origin": "http://localhost:9999", "Content-Type": "application/json"},
        ),
        (
            "allowed host, wrong scheme",
            {"Origin": "https://localhost:3001", "Content-Type": "application/json"},
        ),
        (
            "origin with trailing slash",
            {"Origin": f"{ALLOWED_ORIGIN}/", "Content-Type": "application/json"},
        ),
        ("text/plain body", {"Origin": ALLOWED_ORIGIN, "Content-Type": "text/plain"}),
        (
            "form-encoded body",
            {"Origin": ALLOWED_ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        ),
    ]
    for name, headers in refused:
        response = await client.post(f"{AIHUB}{SEND}", cookies=cookies, headers=headers, content=b'{"message":"x"}')
        report.record(group, name, 403, response.status_code)

    response = remember(
        await client.post(
            f"{AIHUB}/api/chat/send-with-file",
            cookies=cookies,
            headers={"Origin": ALLOWED_ORIGIN},
            files={"message": (None, "multipart probe")},
        )
    )
    report.record(group, "multipart upload from allowed origin", 200, response.status_code)

    response = await client.post(
        f"{AIHUB}/api/chat/send-with-file",
        cookies=cookies,
        headers={"Origin": "https://evil.example"},
        files={"message": (None, "multipart probe")},
    )
    report.record(group, "multipart upload from foreign origin", 403, response.status_code)

    response = await client.get(f"{AIHUB}{SESSIONS}", cookies=cookies)
    report.record(group, "cookie GET needs no origin", 200, response.status_code)

    response = remember(
        await client.post(
            f"{AIHUB}{SEND}",
            headers={**bearer(token), "Origin": "https://evil.example"},
            json=body(),
        )
    )
    report.record(group, "bearer POST is exempt", 200, response.status_code)

    response = await client.patch(
        f"{AIHUB}/api/chat/sessions/{uuid.uuid4()}/pin",
        cookies=cookies,
        headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        json={"isPinned": True},
    )
    report.record(group, "foreign origin on PATCH", 403, response.status_code)

    response = await client.delete(
        f"{AIHUB}/api/chat/sessions/{uuid.uuid4()}",
        cookies=cookies,
        headers={"Origin": "https://evil.example"},
    )
    report.record(group, "foreign origin on DELETE", 403, response.status_code)


async def check_cors(client: httpx.AsyncClient, report: Report) -> None:
    group = "cors"
    cases: list[tuple[str, dict[str, str], int]] = [
        (
            "preflight from allowed origin",
            {
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            200,
        ),
        (
            "preflight from foreign origin",
            {
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            400,
        ),
        (
            "preflight for an unlisted header",
            {
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-evil",
            },
            400,
        ),
    ]
    for name, headers, expected in cases:
        response = await client.options(f"{AIHUB}{SEND}", headers=headers)
        report.record(group, name, expected, response.status_code)


async def check_security_headers(client: httpx.AsyncClient, report: Report) -> None:
    group = "headers"
    response = await client.get(f"{AIHUB}/api/health")
    expected = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }
    for header, value in expected.items():
        report.record(group, header, value, response.headers.get(header))


async def check_tenant_isolation(client: httpx.AsyncClient, report: Report, token: str) -> None:
    """One account must never reach another's conversation."""
    group = "isolation"
    mine = remember(await client.post(f"{AIHUB}{SEND}", headers=bearer(token), json=body("isolation probe")))
    if mine.status_code != 200:
        report.skip(group, "setup: create a session", f"send returned {mine.status_code}")
        return
    session_id = mine.json()["data"]["sessionId"]
    report.record(group, "setup: session created", 200, mine.status_code)

    intruder = bearer(forge())
    probes: list[tuple[str, str, str]] = [
        (
            "another account reads messages",
            "GET",
            f"{AIHUB}/api/chat/sessions/{session_id}/messages",
        ),
        ("another account deletes it", "DELETE", f"{AIHUB}/api/chat/sessions/{session_id}"),
    ]
    for name, method, url in probes:
        response = await client.request(method, url, headers=intruder)
        report.record(group, name, 404, response.status_code)

    response = await client.patch(
        f"{AIHUB}/api/chat/sessions/{session_id}/name",
        headers=intruder,
        json={"name": "hijacked"},
    )
    report.record(group, "another account renames it", 404, response.status_code)

    listing = await client.get(f"{AIHUB}{SESSIONS}", headers=intruder)
    owns = [s["id"] for s in listing.json().get("data", [])] if listing.status_code == 200 else []
    report.record(group, "session absent from another account's list", False, session_id in owns)


async def check_input_validation(client: httpx.AsyncClient, report: Report, token: str) -> None:
    group = "validation"
    auth = bearer(token)
    cases: list[tuple[str, dict[str, object], int]] = [
        ("unknown field refused", {**body(), "evil": "x"}, 422),
        ("empty message refused", {**body(), "message": ""}, 422),
        ("over-long message refused", {**body(), "message": "x" * 4001}, 422),
        ("non-DEFAULT mode refused", {**body(), "mode": "FORMULA"}, 400),
        ("bad session id refused", {**body(), "sessionId": "not-a-uuid"}, 422),
    ]
    for name, payload, expected in cases:
        response = await client.post(f"{AIHUB}{SEND}", headers=auth, json=payload)
        report.record(group, name, expected, response.status_code)

    response = await client.get(f"{AIHUB}/api/chat/sessions/not-a-uuid/messages", headers=auth)
    report.record(group, "bad session id in path refused", 422, response.status_code)

    response = await client.patch(
        f"{AIHUB}/api/chat/sessions/{uuid.uuid4()}/name",
        headers=auth,
        json={"name": "x" * 101},
    )
    report.record(group, "over-long session name refused", 422, response.status_code)


async def check_token_freshness(
    client: httpx.AsyncClient,
    report: Report,
    token: str,
    alt_plant: str | None,
    live_cablecore: bool = True,
) -> None:
    """The two gap fixes: a short lifetime, and claims reissued on a plant switch."""
    group = "claim freshness"
    if not live_cablecore:
        report.skip(group, "access token lifetime is 15 minutes", "needs cableCORE")
        report.skip(group, "plant switch reissues the token", "needs cableCORE")
        return
    claims = claims_of(token)
    lifetime = (int(str(claims["exp"])) - int(str(claims["iat"]))) // 60
    report.record(group, "access token lifetime is 15 minutes", 15, lifetime)

    if alt_plant is None:
        report.skip(group, "plant switch reissues the token", "VERIFY_ALT_PLANT_ID not set")
        return

    original = str(claims.get("activePlantId"))
    auth = bearer(token)
    switched = await client.patch(f"{CABLECORE}/account/change-active-plant/{alt_plant}", headers=auth)
    report.record(group, "plant switch accepted", 200, switched.status_code)

    reissued = switched.cookies.get("access_token")
    report.record(group, "plant switch reissues the access token", True, reissued is not None)
    if reissued is not None:
        new_claims = claims_of(reissued)
        report.record(group, "reissued token names the new plant", alt_plant, new_claims.get("activePlantId"))
        response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(reissued))
        report.record(group, "aihub accepts the reissued token", 200, response.status_code)

    restored = await client.patch(f"{CABLECORE}/account/change-active-plant/{original}", headers=auth)
    report.record(group, "original plant restored", 200, restored.status_code)


async def check_rate_limit(client: httpx.AsyncClient, report: Report, token: str) -> None:
    """Runs last: it spends the account's budget for the rest of the window."""
    group = "rate limit"
    settings = get_settings()
    auth = bearer(token)
    codes: list[int] = []
    for _ in range(settings.chat_rate_limit + 5):
        response = await client.get(f"{AIHUB}{SESSIONS}", headers=auth)
        codes.append(response.status_code)
        if response.status_code == 429:
            report.record(group, "429 carries Retry-After", True, "retry-after" in response.headers)
            break
    report.record(group, "burst is throttled", True, 429 in codes)
    report.record(group, "requests under the limit succeed", True, codes[0] == 200)

    other = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(forge()))
    report.record(group, "another account is unaffected", 200, other.status_code)


def render(report: Report) -> None:
    width = max(len(c.name) for c in report.checks) + 2
    current = ""
    for check in report.checks:
        if check.group != current:
            current = check.group
            print(f"\n{current.upper()}")
        mark = {Outcome.PASS: "ok  ", Outcome.FAIL: "FAIL", Outcome.SKIP: "skip"}[check.outcome]
        detail = f"expected {check.expected}, got {check.actual}" if check.outcome is Outcome.FAIL else check.actual
        print(f"  [{mark}] {check.name:<{width}} {detail}")

    passed = sum(1 for c in report.checks if c.outcome is Outcome.PASS)
    skipped = sum(1 for c in report.checks if c.outcome is Outcome.SKIP)
    print(f"\n{passed} passed, {len(report.failed)} failed, {skipped} skipped")


async def main() -> int:
    user = os.environ.get("VERIFY_USER")
    password = os.environ.get("VERIFY_PASSWORD")

    report = Report()
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        # Without cableCORE credentials the script still verifies aihub on its own,
        # with a locally signed token; the checks that need cableCORE are skipped.
        live_cablecore = bool(user and password)
        token = await sign_in(client, user, password) if live_cablecore else forge()
        if not live_cablecore:
            print("VERIFY_USER/VERIFY_PASSWORD not set - running aihub-only checks")

        unlicensed_user = os.environ.get("VERIFY_UNLICENSED_USER")
        unlicensed_password = os.environ.get("VERIFY_UNLICENSED_PASSWORD")
        unlicensed = (
            await sign_in(client, unlicensed_user, unlicensed_password)
            if live_cablecore and unlicensed_user and unlicensed_password
            else None
        )

        groups: list[tuple[str, Coroutine[None, None, None]]] = [
            ("authentication", check_authentication(client, report, token)),
            ("ai licence", check_licence(client, report, unlicensed)),
            ("cross-site", check_cross_site(client, report, token)),
            ("cors", check_cors(client, report)),
            ("headers", check_security_headers(client, report)),
            ("isolation", check_tenant_isolation(client, report, token)),
            ("validation", check_input_validation(client, report, token)),
            (
                "claim freshness",
                check_token_freshness(
                    client,
                    report,
                    token,
                    os.environ.get("VERIFY_ALT_PLANT_ID") if live_cablecore else None,
                    live_cablecore,
                ),
            ),
        ]
        for name, coroutine in groups:
            try:
                await coroutine
            except Exception as exc:
                report.record(name, "group completed", True, f"raised {type(exc).__name__}: {exc}")

        # Before the rate-limit burst: DELETE is throttled like any other chat call,
        # so cleanup after it would be refused. Always runs, even if a group raised.
        await cleanup(client, token, report)

        # Last: it spends the account's budget for the rest of the window.
        try:
            await check_rate_limit(client, report, token)
        except Exception as exc:
            report.record("rate limit", "group completed", True, f"raised {type(exc).__name__}")

    render(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
