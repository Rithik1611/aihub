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
    cc_public_url: str = 'http://localhost:5000'  # the same backend as the user's browser reaches it (pictures in the chat)
    cc_db_url: SecretStr = SecretStr('')  # read-only role ai_reader
    cc_schema_dir: Path = _APP_DIR.parents[3] / 'cableCORE_BE' / 'prisma' / 'models'
    cc_notes_file: Path = _APP_DIR / 'app_notes.md'
    cc_index_ttl_s: int = Field(default=1800, ge=60)
    # sandbox (Open Terminal)
    cc_terminal_url: str = 'http://localhost:8070'
    open_terminal_api_key: SecretStr = SecretStr('')
    cc_sandbox_tools: str = _SANDBOX_TOOLS
    # pictures for the chat (SerpAPI Google Images); empty = no search_images tool
    serpapi_key: SecretStr = SecretStr('')
    # generate_image (OpenAI image model); empty model = no tool. Base URL / key empty = the chat model's.
    image_model: str = ''
    image_base_url: str = ''
    image_api_key: SecretStr = SecretStr('')
    image_dir: Path = Path('generated_images')  # one folder per user; a shared volume when aihub runs several replicas
    image_public_url: str = 'http://localhost:8000'  # aihub as the user's browser reaches it
    image_retention_days: int = Field(default=30, ge=1)
    image_per_user_per_hour: int = Field(default=5, ge=1)
    # MES (Mendix MCP server): empty url = off. Tokens are per user: JSON {"cableCORE username": "token"}
    mes_mcp_url: str = ''
    mes_user_tokens: SecretStr = SecretStr('')
    # MES tools offered besides those the server marks readOnlyHint: reviewed read tools only (comma separated)
    mes_read_tools: str = (
        'mes_list_my_sites,mes_list_machines,mes_list_jobs,mes_job_counts,mes_get_job,mes_find_lot,mes_why_lot_unavailable,'
        'mes_list_production_orders,mes_get_production_order,mes_get_machine,mes_get_shift_window,mes_list_inventory_lots,'
        'mes_inventory_summary,mes_get_lot,mes_list_storage,mes_list_quality_orders,mes_get_quality_order,mes_quality_counts,'
        'mes_list_rework,mes_search_knowledge,mes_list_documents,mes_get_help_topic')
    # loop
    agent_max_steps: int = Field(default=100, ge=1)
    agent_reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh'] = 'medium'
    subagents_max_concurrent: int = Field(default=6, ge=1)
    tool_result_max_chars: int = Field(default=40_000, ge=1000)
    # after-turn checks and titles (LangChain chat model); empty = the chat model above
    check_model: str = ''
    # OCR for scanned PDF pages: Azure AI Document Intelligence. Empty = scanned pages have no text.
    ocr_api_url: str = ''
    ocr_api_key: SecretStr = SecretStr('')
    # Housekeeping model: DeepSeek on Azure (chat completions), for jobs that need no reasoning about the
    # user's data: chat titles, rolling chat summaries, and (when coverage_check_model says so) the
    # completeness check. Empty endpoint = everything stays on the chat model.
    deepseek_endpoint: str = ''
    deepseek_api_key: SecretStr = SecretStr('')
    deepseek_deployment_name: str = 'DeepSeek-V4-Pro'
    housekeeping_model: Literal['deepseek', 'luna'] = 'deepseek'
    # The completeness check judges the answer against the request; it moves to DeepSeek only once its
    # findings are shown to match luna's on the same answers.
    coverage_check_model: Literal['deepseek', 'luna'] = 'luna'
    # Tool search (Responses API, gpt-5.4+): tools marked `defer` (sandbox, web) are hidden until the model
    # searches for them. Off: every tool's definition is sent with every request.
    tool_search: bool = False
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
