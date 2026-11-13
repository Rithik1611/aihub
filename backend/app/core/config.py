from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value comes from the environment or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "aihub"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]

    # aihub's own Postgres - deliberately not cableCORE's database (see docs/adr/0004).
    database_url: PostgresDsn
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_echo: bool = False
    # Wait for a free pooled connection; without it a saturated pool blocks forever.
    db_pool_timeout_seconds: int = Field(default=10, ge=1)
    # Recycle before a proxy or Postgres drops an idle connection underneath us.
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    # Server-side cap on a single statement. A query that hangs otherwise holds its
    # connection until the process dies; cableCORE sets the same guard in its DSN.
    db_statement_timeout_seconds: int = Field(default=30, ge=1)

    # The aihub AI service. Still under development; when it is unreachable a turn
    # fails but the user's stored message is kept.
    ai_base_url: str = "http://localhost:8888/api/chatbot"
    # Dev only: answer with a canned reply instead of calling the AI service, so the
    # chat flow can be exercised before the service exists. Replies are prefixed so
    # stored stub content is never mistaken for a real answer. Never enable in
    # production - guarded in app.main.
    ai_stub: bool = False

    # Shared with cableCORE_BE, which signs the tokens aihub verifies.
    jwt_secret: SecretStr
    # Literal, not str: a free-form algorithm is how "none" and HMAC/RSA confusion
    # get in. Only cableCORE's HMAC family is accepted.
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    # cableCORE reads the token from this cookie when no Authorization header is sent.
    jwt_cookie_name: str = "access_token"
    # Tolerance for clock drift between cableCORE and aihub.
    jwt_leeway_seconds: int = Field(default=30, ge=0, le=300)

    # Per-account limit on chat requests. Each one can call an LLM, so this is cost
    # control as much as abuse control. Mirrors cableCORE's 60/minute.
    chat_rate_limit: int = Field(default=60, ge=1)
    chat_rate_window_seconds: int = Field(default=60, ge=1)

    @field_validator("cors_origins")
    @classmethod
    def _origins_are_exact(cls, value: list[str]) -> list[str]:
        """These double as the CSRF allowlist (app.auth), so each must be one exact
        origin. A wildcard or a stray path would quietly widen who may write."""
        normalised: list[str] = []
        for origin in value:
            # Browsers send the scheme and host lowercased; compare like for like.
            candidate = origin.strip().lower()
            scheme, sep, host = candidate.partition("://")
            if (
                candidate == "*"
                or scheme not in ("http", "https")
                or not sep
                or not host
                or "/" in host
            ):
                raise ValueError(f"cors_origins entry {origin!r} must be scheme://host[:port]")
            normalised.append(candidate)
        return normalised

    @field_validator("ai_base_url")
    @classmethod
    def _ai_url_is_http(cls, value: str) -> str:
        """The caller's cableCORE token is forwarded here, so a stray value would
        hand live credentials to whatever host it names."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("ai_base_url must start with http:// or https://")
        return value


@lru_cache
def get_settings() -> Settings:
    # Values come from the environment, so none are passed here.
    return Settings()
