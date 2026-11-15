"""Shared clients: one aiohttp session per event loop, one model client (Azure OpenAI, Responses API).

With LANGSMITH_TRACING=true the model client is wrapped by LangSmith, so every loop step (prompt, tool
calls, tokens, latency) is traced without touching the loop. LANGFUSE_TRACING=true does the same with
Langfuse (self-hosted). Off by default: traces contain chat content.
"""

import asyncio
import ssl

import aiohttp
import truststore
from openai import AsyncOpenAI

from ai.settings import settings

_sessions: dict[asyncio.AbstractEventLoop, aiohttp.ClientSession] = {}
_llm: AsyncOpenAI | None = None


def session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    http = _sessions.get(loop)
    if http is None or http.closed:
        # The OS certificate store, not only certifi's: networks that inspect HTTPS sign with their own CA.
        tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        http = _sessions[loop] = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls))
    return http


def llm() -> AsyncOpenAI:
    global _llm
    if _llm is None:
        s = settings()
        client = AsyncOpenAI(base_url=s.luna_base_url, api_key=s.luna_api_key.get_secret_value())
        if s.langfuse_tracing:
            # Patches the openai SDK itself: every responses.create/parse is traced, ours and the
            # LangChain ChatOpenAI's (checks, titles), so no LangChain callback is needed.
            import langfuse.openai  # noqa: F401  # pyright: ignore[reportUnusedImport]
        if s.langsmith_tracing:
            from langsmith.wrappers import wrap_openai

            client = wrap_openai(client)
        _llm = client
    return _llm


def model() -> str:
    return settings().luna_model


async def close() -> None:
    """Call on backend shutdown."""
    http = _sessions.pop(asyncio.get_running_loop(), None)
    if http:
        await http.close()
