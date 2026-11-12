from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "aihub"
    debug: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]


settings = Settings()
