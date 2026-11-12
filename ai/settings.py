"""All agent configuration, typed and validated in one place (environment variables, or cableAI/.env).

The backend calls settings.require() at startup, so a missing key stops it there, not mid-chat.
loop.py reads its three AGENT_* / SUBAGENTS_* values itself; they are declared here too, so a bad value
(e.g. AGENT_MAX_STEPS=abc) is caught by require() as well.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).resolve().parent / 'apps' / 'cablecore'
_SANDBOX_TOOLS = ('run_command,get_process_status,send_process_input,kill_process,list_processes,'
                  'read_file,write_file,replace_file_content,list_files,grep_search,glob_search')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    # model (Azure OpenAI, Responses API)
    luna_base_url: str = ''
    luna_api_key: SecretStr = SecretStr('')
    luna_model: str = 'gpt-5.6-luna'
    # cableCORE
    cc_api_url: str = 'http://localhost:5000'
    cc_db_url: SecretStr = SecretStr('')  # read-only role ai_reader
    cc_schema_dir: Path = _APP_DIR.parents[3] / 'cableCORE_BE' / 'prisma' / 'models'
    cc_notes_file: Path = _APP_DIR / 'app_notes.md'
    cc_index_ttl_s: int = Field(default=1800, ge=60)
    # sandbox (Open Terminal)
    cc_terminal_url: str = 'http://localhost:8070'
    open_terminal_api_key: SecretStr = SecretStr('')
    cc_sandbox_tools: str = _SANDBOX_TOOLS
    # loop
    agent_max_steps: int = Field(default=100, ge=1)
    agent_reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh'] = 'medium'
    subagents_max_concurrent: int = Field(default=6, ge=1)
    tool_result_max_chars: int = Field(default=40_000, ge=1000)
    # after-turn checks and titles (LangChain chat model); empty = the chat model above
    check_model: str = ''
    # LangSmith tracing of every model call (loop steps, checks, titles). Off by default: it sends chat
    # content to LangSmith (LANGSMITH_API_KEY, LANGSMITH_PROJECT, LANGSMITH_ENDPOINT for self-hosted).
    langsmith_tracing: bool = False


@lru_cache
def settings() -> Settings:
    return Settings()


def require() -> Settings:
    """Settings with the keys a chat needs; raises naming what is missing."""
    s = settings()
    missing = [name for name, value in (('LUNA_BASE_URL', s.luna_base_url), ('LUNA_API_KEY', s.luna_api_key.get_secret_value()),
                                        ('CC_DB_URL', s.cc_db_url.get_secret_value())) if not value]
    if missing:
        raise RuntimeError(f'Missing settings: {", ".join(missing)} (environment or cableAI/.env).')
    return s
