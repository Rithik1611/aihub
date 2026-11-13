"""Account-lifecycle checks across cableCORE and aihub.

verify_auth.py proves a request is authorised correctly *now*. This proves what
happens when the account changes underneath a live session: the token expires, the
user switches plant, an admin deletes them. Those are the cases where the two
services can disagree, because aihub authorises from the token and cableCORE
authorises from its database.

Usage:

    export VERIFY_USER=... VERIFY_PASSWORD=...                        # cableAI-licensed
    export VERIFY_UNLICENSED_USER=... VERIFY_UNLICENSED_PASSWORD=...  # no cableAI licence
    export VERIFY_ALT_PLANT_ID=...                                    # a second plant of VERIFY_USER
    export VERIFY_CC_DB_URL=postgresql://...                          # cableCORE's DB, read-only use
    uv run python backend/scripts/verify_lifecycle.py

Credentials come from the environment only. The script forges tokens with the
configured JWT secret to reach states a real login cannot produce (already expired,
licence revoked), so run it against development only. It deletes the chat sessions
it creates and restores the active plant it switches.

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

import httpx
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings

AIHUB = os.environ.get("AIHUB_URL", "http://127.0.0.1:8010")
CABLECORE = os.environ.get("CABLECORE_URL", "http://localhost:5000")
TIMEOUT = httpx.Timeout(60.0)

SESSIONS = "/api/chat/sessions"
SEND = "/api/chat/send"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    NOTE = "NOTE"


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    detail: str
    outcome: Outcome


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def record(self, group: str, name: str, expected: object, actual: object) -> None:
        ok = expected == actual
        detail = str(actual) if ok else f"expected {expected}, got {actual}"
        self.checks.append(Check(group, name, detail, Outcome.PASS if ok else Outcome.FAIL))

    def note(self, group: str, name: str, detail: str) -> None:
        """A finding that is neither pass nor fail: behaviour worth stating."""
        self.checks.append(Check(group, name, detail, Outcome.NOTE))

    def skip(self, group: str, name: str, why: str) -> None:
        self.checks.append(Check(group, name, why, Outcome.SKIP))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.outcome is Outcome.FAIL]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict[str, object]:
    decoded: dict[str, object] = jwt.decode(token, options={"verify_signature": False})
    return decoded


def reissue(base: dict[str, object], **overrides: object) -> str:
    """Re-sign a real token's claims with the configured secret, changing only what is
    under test. The signature stays valid, so the check is of aihub's rules and not of
    its signature verification."""
    settings = get_settings()
    payload = {**base, **overrides}
    for key, value in list(payload.items()):
        if value is None:
            del payload[key]
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)


async def sign_in(client: httpx.AsyncClient, user: str, password: str) -> dict[str, str]:
    response = await client.post(f"{CABLECORE}/auth/token", json={"username": user, "password": password})
    response.raise_for_status()
    data: dict[str, str] = response.json()["data"]
    return data


async def start_chat(client: httpx.AsyncClient, token: str, message: str) -> str | None:
    """A stored conversation, as a real user would leave behind. Returns its session id."""
    response = await client.post(
        f"{AIHUB}{SEND}",
        headers=bearer(token),
        json={"message": message, "mode": "DEFAULT", "sessionId": None, "newSessionId": None},
    )
    if response.status_code != 200:
        return None
    session_id: str = response.json()["data"]["sessionId"]
    return session_id


# --- scenario A: the 15-minute token ------------------------------------------


async def check_expiry_and_refresh(client: httpx.AsyncClient, report: Report, tokens: dict[str, str]) -> None:
    """After 15 minutes the token is dead. Is the user locked out, or refreshed?"""
    group = "A. expiry and refresh"
    token = tokens["accessToken"]
    base = claims_of(token)

    lifetime = (int(str(base["exp"])) - int(str(base["iat"]))) // 60
    report.record(group, "access token lives 15 minutes", 15, lifetime)

    # Exactly the state the token is in 15 minutes from now.
    expired = reissue(base, exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()))
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(expired))
    report.record(group, "expired token is refused by aihub", 401, response.status_code)

    within_skew = reissue(base, exp=int((datetime.now(UTC) - timedelta(seconds=5)).timestamp()))
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(within_skew))
    report.record(group, "seconds of clock drift do not lock out", 200, response.status_code)

    # What the frontend does on that 401: refresh against cableCORE.
    refreshed = await client.post(f"{CABLECORE}/auth/refresh", cookies={"refresh_token": tokens["refreshToken"]})
    report.record(group, "cableCORE refresh succeeds", 200, refreshed.status_code)
    new_token = refreshed.cookies.get("access_token")
    report.record(group, "refresh issues a new access token", True, new_token is not None)
    if new_token is None:
        return

    new_claims = claims_of(new_token)
    report.record(
        group,
        "refreshed token still carries the licence",
        True,
        new_claims.get("canAccessAi") is True,
    )
    report.record(
        group,
        "refreshed token lives 15 minutes",
        15,
        (int(str(new_claims["exp"])) - int(str(new_claims["iat"]))) // 60,
    )
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(new_token))
    report.record(group, "aihub accepts the refreshed token", 200, response.status_code)

    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(expired))
    report.record(group, "the expired token stays refused after refresh", 401, response.status_code)
    report.note(
        group,
        "verdict",
        "access is refreshed, not lost: the user keeps working, the dead token stays dead",
    )


# --- scenario B: switching plant ----------------------------------------------


async def plant_licence(db_url: str, account: str, plant: str) -> bool:
    """What cableCORE's own data says about this account's licence on this plant."""
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        modules = await conn.fetchval(
            'select modules from "AccountPlantModule" where "accountId"=$1 and "plantId"=$2',
            account,
            plant,
        )
        has_company = await conn.fetchval(
            'select c."hasCableAI" from "Company" c join "Account" a on a."companyId"=c.id where a.id=$1',
            account,
        )
    finally:
        await conn.close()
    mods = list(modules or [])
    return "cableAI" in mods or ("Admin" in mods and bool(has_company))


async def check_plant_switch(
    client: httpx.AsyncClient,
    report: Report,
    tokens: dict[str, str],
    alt_plant: str | None,
    db_url: str | None,
) -> None:
    """Switching plant changes which licence applies. Does aihub follow?"""
    group = "B. plant switch"
    token = tokens["accessToken"]
    base = claims_of(token)
    account = str(base["sub"])
    original = str(base.get("activePlantId"))

    # aihub's own rule, independent of cableCORE: the claim decides.
    unlicensed = reissue(base, canAccessAi=False)
    response = await client.post(
        f"{AIHUB}{SEND}",
        headers=bearer(unlicensed),
        json={"message": "x", "mode": "DEFAULT", "sessionId": None, "newSessionId": None},
    )
    report.record(group, "aihub refuses a token whose licence is false", 403, response.status_code)

    no_plant = reissue(base, activePlantId=None)
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(no_plant))
    report.record(group, "aihub refuses a token with no active plant", 403, response.status_code)

    if alt_plant is None:
        report.skip(group, "switching plant reissues the token", "VERIFY_ALT_PLANT_ID not set")
        return

    switched = await client.patch(f"{CABLECORE}/account/change-active-plant/{alt_plant}", headers=bearer(token))
    report.record(group, "cableCORE accepts the switch", 200, switched.status_code)
    new_token = switched.cookies.get("access_token")
    report.record(group, "the switch reissues the access token", True, new_token is not None)

    if new_token is not None:
        new_claims = claims_of(new_token)
        report.record(group, "the new token names the new plant", alt_plant, new_claims.get("activePlantId"))
        if db_url:
            expected = await plant_licence(db_url, account, alt_plant)
            report.record(
                group,
                "its licence matches the new plant's modules in cableCORE",
                expected,
                new_claims.get("canAccessAi"),
            )
        else:
            report.skip(group, "licence matches cableCORE's data", "VERIFY_CC_DB_URL not set")

        response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(new_token))
        expected_status = 200 if new_claims.get("canAccessAi") else 403
        report.record(group, "aihub honours the new plant's licence", expected_status, response.status_code)

        report.record(
            group,
            "the pre-switch token still names the old plant",
            original,
            str(base.get("activePlantId")),
        )
        report.note(
            group,
            "staleness window",
            "a token issued before the switch keeps the old licence until it expires"
            " (15 min); the reissue is what makes the change immediate",
        )

    restored = await client.patch(f"{CABLECORE}/account/change-active-plant/{original}", headers=bearer(token))
    report.record(group, "original plant restored", 200, restored.status_code)


async def check_unlicensed_account(
    client: httpx.AsyncClient, report: Report, unlicensed: dict[str, str] | None
) -> None:
    group = "B. plant switch"
    if unlicensed is None:
        report.skip(group, "a real unlicensed account is refused", "VERIFY_UNLICENSED_USER not set")
        return
    token = unlicensed["accessToken"]
    claims = claims_of(token)
    report.record(group, "cableCORE marks the account unlicensed", False, claims.get("canAccessAi"))
    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(token))
    report.record(group, "aihub refuses it", 403, response.status_code)
    core = await client.post(
        f"{CABLECORE}/chat/send",
        headers=bearer(token),
        json={"message": "probe", "mode": "DEFAULT"},
    )
    report.record(group, "cableCORE refuses it too, with the same verdict", 403, core.status_code)


# --- scenario C: the deleted user ---------------------------------------------


async def check_deleted_user(client: httpx.AsyncClient, report: Report, token: str, created: list[str]) -> None:
    """An admin deletes a user in cableCORE. What happens to their chat history?"""
    group = "C. deleted user"
    session_id = await start_chat(client, token, "history that should follow the account")
    if session_id is None:
        report.skip(group, "setup: store a conversation", "chat send did not return a session")
        return
    created.append(session_id)
    report.record(group, "setup: the user has chat history in aihub", True, True)

    # cableCORE has no way to tell aihub. aihub exposes no way to be told.
    schema = await client.get(f"{AIHUB}/openapi.json")
    paths = list(schema.json().get("paths", {})) if schema.status_code == 200 else []
    purge = [p for p in paths if "account" in p.lower() or "purge" in p.lower()]
    report.record(group, "aihub has no account-deletion endpoint", [], purge)

    owner_only = [p for p in paths if p.endswith("/sessions/{session_id}")]
    report.note(
        group,
        "the only deletion aihub offers",
        f"{owner_only} - one session, by its owner, with a valid token",
    )

    response = await client.get(f"{AIHUB}{SESSIONS}", headers=bearer(token))
    still_there = any(s["id"] == session_id for s in (response.json().get("data") or []))
    report.record(group, "the history is readable while the token is valid", True, still_there)
    report.note(
        group,
        "verdict",
        "NOT CONFIGURED: deleting a user in cableCORE leaves their aihub chat history"
        " in place. cableCORE soft-deletes (isDeleted=true) and never notifies aihub;"
        " aihub has no endpoint to purge an account. The history is orphaned, not removed.",
    )


# --- scenario D: one turn at a time -------------------------------------------


async def check_concurrent_turns(client: httpx.AsyncClient, report: Report, token: str, created: list[str]) -> None:
    """Two messages at once in one chat: the second must not start a second turn."""
    group = "D. concurrent turns"
    session_id = await start_chat(client, token, "first turn")
    if session_id is None:
        report.skip(group, "setup: open a session", "chat send did not return a session")
        return
    created.append(session_id)

    body = {"mode": "DEFAULT", "sessionId": session_id, "newSessionId": None}
    first, second = await asyncio.gather(
        client.post(f"{AIHUB}{SEND}", headers=bearer(token), json={**body, "message": "A"}),
        client.post(f"{AIHUB}{SEND}", headers=bearer(token), json={**body, "message": "B"}),
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in (first, second) if isinstance(r, httpx.Response))
    report.note(group, "two simultaneous sends", f"status codes {codes}")
    # Exact, not "the first one worked": the weaker form passed while both turns ran
    # and interleaved the transcript.
    report.record(group, "one turn runs, the second is refused with 409", [200, 409], codes)

    messages = await client.get(
        f"{AIHUB}/api/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    senders = [m["sender"] for m in (messages.json().get("data") or {}).get("messages", [])]
    alternating = all(a != b for a, b in pairwise(senders))
    report.record(group, "the transcript still alternates human/ai", True, alternating)


async def cleanup(client: httpx.AsyncClient, report: Report, token: str, created: list[str]) -> None:
    removed = 0
    for session_id in created:
        response = await client.delete(f"{AIHUB}/api/chat/sessions/{session_id}", headers=bearer(token))
        removed += 1 if response.status_code == 200 else 0
    report.record("cleanup", "sessions created by this run removed", len(created), removed)


def render(report: Report) -> None:
    width = max(len(c.name) for c in report.checks) + 2
    current = ""
    marks = {Outcome.PASS: "ok  ", Outcome.FAIL: "FAIL", Outcome.SKIP: "skip", Outcome.NOTE: "note"}
    for check in report.checks:
        if check.group != current:
            current = check.group
            print(f"\n{current.upper()}")
        print(f"  [{marks[check.outcome]}] {check.name:<{width}} {check.detail}")
    passed = sum(1 for c in report.checks if c.outcome is Outcome.PASS)
    skipped = sum(1 for c in report.checks if c.outcome is Outcome.SKIP)
    notes = sum(1 for c in report.checks if c.outcome is Outcome.NOTE)
    print(f"\n{passed} passed, {len(report.failed)} failed, {skipped} skipped, {notes} notes")


async def main() -> int:
    user = os.environ.get("VERIFY_USER")
    password = os.environ.get("VERIFY_PASSWORD")
    if not user or not password:
        print("VERIFY_USER and VERIFY_PASSWORD must be set", file=sys.stderr)
        return 2

    report = Report()
    created: list[str] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        tokens = await sign_in(client, user, password)
        token = tokens["accessToken"]

        un_user = os.environ.get("VERIFY_UNLICENSED_USER")
        un_password = os.environ.get("VERIFY_UNLICENSED_PASSWORD")
        unlicensed = await sign_in(client, un_user, un_password) if un_user and un_password else None

        groups = [
            ("A", check_expiry_and_refresh(client, report, tokens)),
            (
                "B",
                check_plant_switch(
                    client,
                    report,
                    tokens,
                    os.environ.get("VERIFY_ALT_PLANT_ID"),
                    os.environ.get("VERIFY_CC_DB_URL"),
                ),
            ),
            ("B2", check_unlicensed_account(client, report, unlicensed)),
            ("C", check_deleted_user(client, report, token, created)),
            ("D", check_concurrent_turns(client, report, token, created)),
        ]
        for name, coroutine in groups:
            try:
                await coroutine
            except Exception as exc:
                report.record(name, "group completed", True, f"raised {type(exc).__name__}: {exc}")

        await cleanup(client, report, token, created)

    render(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
