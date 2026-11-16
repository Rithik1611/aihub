"""LangChain adapters. The agent loop does not use LangChain; this is for everything around it.

A. langchain_tools(ctx): the agent's tools as LangChain StructuredTools, with the same Pydantic argument
   models. Calls go through ai.tools.run, so validation, the write harness (one approval card per
   cc_action batch), and the result cap all still apply. Cards are put on ctx.emit; set it to receive them.
C. structured(Model, job): a LangChain chat model that answers as a Pydantic model. job='check' (the
   writes check) runs on the chat model (or CHECK_MODEL); job='housekeeping' (titles, summaries) on DeepSeek
   when DEEPSEEK_ENDPOINT is set, falling back to the chat model if DeepSeek fails.
(B, tracing, is in http.py: LangSmith wraps the model client when LANGSMITH_TRACING=true.)
"""

import json
from functools import lru_cache
from typing import Any, Literal, TypeVar

from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from ai import apps, ask_user, sandbox, web
from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool, run

M = TypeVar('M', bound=BaseModel)


# ---- C: chat model with Pydantic output


@lru_cache
def chat_model() -> ChatOpenAI:
    s = settings()
    return ChatOpenAI(
        model=s.check_model or s.luna_model,
        base_url=s.luna_base_url,
        api_key=s.luna_api_key,
        use_responses_api=True,
        reasoning={'effort': 'low'},
        store=False,
    )


@lru_cache
def housekeeping_model() -> ChatOpenAI | None:
    """DeepSeek for housekeeping jobs; None when it is not configured or switched off."""
    s = settings()
    if s.housekeeping_model != 'deepseek' or not s.deepseek_endpoint:
        return None
    return ChatOpenAI(
        model=s.deepseek_deployment_name,
        base_url=s.deepseek_endpoint.rstrip('/'),
        api_key=s.deepseek_api_key,
        max_completion_tokens=4000,
        timeout=120,
        max_retries=1,
    )


def structured(model: type[M], job: Literal['check', 'housekeeping'] = 'check') -> Runnable[Any, M]:
    """`await structured(Verdict).ainvoke(prompt)` -> a validated Verdict. Housekeeping jobs run on DeepSeek
    when it is configured, and fall back to the chat model when DeepSeek fails or answers off-schema."""
    luna: Runnable[Any, Any] = chat_model().with_structured_output(model, method='json_schema', strict=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    cheap = housekeeping_model() if job == 'housekeeping' else None
    if cheap is None:
        return luna
    return cheap.with_structured_output(model, method='json_schema', strict=True).with_fallbacks([luna])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportReturnType]


# ---- A: the agent's tools for LangChain / LangGraph


def as_langchain(t: Tool, ctx: TurnContext) -> StructuredTool:
    async def call(**kwargs: Any) -> str:
        arguments = t.args_model.model_validate(kwargs).model_dump_json() if t.args_model else json.dumps(kwargs, default=str)
        _status, text = await run({t.name: t}, ctx, t.name, arguments)
        return text

    return StructuredTool.from_function(coroutine=call, name=t.name, description=t.description,
                                        args_schema=t.args_model or t.parameters)


async def langchain_tools(ctx: TurnContext) -> list[StructuredTool]:
    """The app's tools plus ask_user, web and sandbox tools (no delegate_task: that is the loop's sub-agent)."""
    tools = [*apps.get(ctx.app_id).tools(), ask_user.TOOL, *web.TOOLS, *await sandbox.tools(ctx)]
    return [as_langchain(t, ctx) for t in tools]
