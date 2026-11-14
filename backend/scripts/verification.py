"""Shared harness for the verification scripts.

`verify_auth.py` checks that a request is authorised correctly now;
`verify_lifecycle.py` checks what happens when the account changes underneath a
live session. They ask different questions of the same two services, so the way
they sign in, forge tokens, record results and clean up belongs here rather than
in each of them.

Nothing in here is application code: it exists to drive the running services the
way a browser and an attacker would.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field

# The app package lives one level up; importing it is what gives the scripts the
# same JWT secret and algorithm the server is running with.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings

AIHUB: Final = os.environ.get("AIHUB_URL", "http://127.0.0.1:8010")
CABLECORE: Final = os.environ.get("CABLECORE_URL", "http://localhost:5000")
ALLOWED_ORIGIN: Final = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3001")
TIMEOUT: Final = httpx.Timeout(60.0)

SESSIONS: Final = "/api/chat/sessions"
SEND: Final = "/api/chat/send"


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
    """What a run found, in the order it found it."""

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

    def render(self) -> None:
        width = max((len(c.name) for c in self.checks), default=0) + 2
        marks = {
            Outcome.PASS: "ok  ",
            Outcome.FAIL: "FAIL",
            Outcome.SKIP: "skip",
            Outcome.NOTE: "note",
        }
        current = ""
        for check in self.checks:
            if check.group != current:
                current = check.group
                print(f"\n{current.upper()}")
            print(f"  [{marks[check.outcome]}] {check.name:<{width}} {check.detail}")
        counts = {outcome: 0 for outcome in Outcome}
        for check in self.checks:
            counts[check.outcome] += 1
        print(
            f"\n{counts[Outcome.PASS]} passed, {counts[Outcome.FAIL]} failed, "
            f"{counts[Outcome.SKIP]} skipped, {counts[Outcome.NOTE]} notes"
        )

    def exit_code(self) -> int:
        return 1 if self.failed else 0


class Tokens(BaseModel):
    """cableCORE's sign-in response. Parsed, not indexed into."""

    model_config = ConfigDict(extra="ignore")

    access: str = Field(alias="accessToken")
    refresh: str = Field(alias="refreshToken")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def claims_of(token: str) -> dict[str, object]:
    """The claims without verifying the signature: for reading what a token says."""
    decoded: dict[str, object] = jwt.decode(token, options={"verify_signature": False})
    return decoded


def sign(claims: Mapping[str, object]) -> str:
    """Sign claims with the secret the server is configured with.

    This is how the scripts reach states a real login cannot produce - already
    expired, licence revoked, claims missing. The signature is genuine, so what is
    under test is the server's rules rather than its signature checking. It only
    makes sense against a development deployment.
    """
    settings = get_settings()
    payload = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)


def forge(**overrides: object) -> str:
    """A valid, licensed token for an account that need not exist in cableCORE."""
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "username": "forged",
        "role": "DESIGNER",
        "type": "access",
        "activePlantId": str(uuid.uuid4()),
        "canAccessAi": True,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return sign({**base, **overrides})


def reissue(base: Mapping[str, object], **overrides: object) -> str:
    """Re-sign a real token's claims, changing only what is under test."""
    return sign({**base, **overrides})


async def sign_in(client: httpx.AsyncClient, user: str, password: str) -> Tokens:
    """A real cableCORE token pair, via the plain-password endpoint."""
    response = await client.post(f"{CABLECORE}/auth/token", json={"username": user, "password": password})
    response.raise_for_status()
    return Tokens.model_validate(response.json()["data"])


@dataclass
class Sessions:
    """Chat sessions a run created, so it can delete them again.

    A verification script that leaves rows behind skews the next run's session
    list, so creation and teardown are tracked in one place.
    """

    created: list[str] = field(default_factory=list)

    def remember(self, response: httpx.Response) -> httpx.Response:
        if response.status_code == 200:
            session_id = response.json().get("data", {}).get("sessionId")
            if session_id:
                self.created.append(str(session_id))
        return response

    async def cleanup(self, client: httpx.AsyncClient, token: str, report: Report) -> None:
        removed = 0
        for session_id in self.created:
            response = await client.delete(f"{AIHUB}{SESSIONS}/{session_id}", headers=bearer(token))
            removed += 1 if response.status_code == 200 else 0
        report.record("cleanup", "sessions created by this run removed", len(self.created), removed)


def chat_body(message: str = "verification probe", **overrides: object) -> dict[str, object]:
    return {
        "message": message,
        "mode": "DEFAULT",
        "sessionId": None,
        "newSessionId": None,
        **overrides,
    }
