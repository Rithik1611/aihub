from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr
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

    # The aihub AI service. Still under development; when it is unreachable a turn
    # fails but the user's stored message is kept.
    ai_base_url: str = "http://localhost:8888/api/chatbot"
    # Dev only: answer with a canned reply instead of calling the AI service, so the
    # chat flow can be exercised before the service exists. Replies are prefixed so
    # stored stub content is never mistaken for a real answer. Never enable in
    # production - guarded in app.main.
    ai_stub: bool = False

    # Shared with cableCORE_BE, which signs the tokens aihub verifies (HS256).
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    # cableCORE reads the token from this cookie when no Authorization header is sent.
    jwt_cookie_name: str = "access_token"


@lru_cache
def get_settings() -> Settings:
    # Values come from the environment, so none are passed here.
    return Settings()
