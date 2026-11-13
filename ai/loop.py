"""The agent loop: model -> tool calls -> model, until it answers.

run_turn(ctx, history, message) is everything the backend calls for a chat turn. The model is reached
through the OpenAI Responses API (Azure OpenAI: LUNA_BASE_URL, LUNA_API_KEY, LUNA_MODEL). Nothing is
stored at the provider (store=False): reasoning travels encrypted in the items the backend keeps.
"""

import asyncio
import json
import os
from dataclasses import replace
from typing import AsyncIterator

from ai import apps, ask_user, http, sandbox, web
from ai.context import Event, TurnContext
from ai.tools import Tool, run, tool

MAX_STEPS = int(os.getenv('AGENT_MAX_STEPS', '100'))
REASONING_EFFORT = os.getenv('AGENT_REASONING_EFFORT', 'medium')
SUBAGENTS_MAX_CONCURRENT = int(os.getenv('SUBAGENTS_MAX_CONCURRENT', '6'))
SUBAGENT_PROMPT = """You are a sub-agent working on a specific task assigned by the lead agent.

You have full access to the workspace — you can read, write, edit files, and run commands.
Focus exclusively on your assigned task. Do NOT work on anything outside your scope.

When done, end with a clear summary:
- What you did
- What files you changed (if any)
- Any issues or open questions
"""

# Fields the Responses API accepts back as input, per item type (Azure rejects e.g. `status` on reasoning).
_INPUT_FIELDS = {
    'message': {'type', 'role', 'content'},
    'function_call': {'type', 'call_id', 'name', 'arguments', 'id', 'namespace'},  # namespace: tools loaded by tool search
    'function_call_output': {'type', 'call_id', 'output'},
    'reasoning': {'type', 'id', 'summary', 'encrypted_content'},
}
_subagents = asyncio.Semaphore(SUBAGENTS_MAX_CONCURRENT)


def _input_item(item: dict) -> dict:
    allowed = _INPUT_FIELDS.get(item.get('type', ''))
    return {k: v for k, v in item.items() if k in allowed} if allowed else item


def text_of(items: list[dict]) -> str:
    """The assistant's visible text in these items."""
    return '\n'.join(
        part.get('text', '') for item in items if item.get('type') == 'message' and item.get('role') == 'assistant'
        for part in item.get('content') or [] if part.get('type') == 'output_text'
    )


async def _drain(task: asyncio.Task, queue: asyncio.Queue) -> AsyncIterator[Event]:
    """Events the task puts on the queue, as they come, until the task is done."""
    while True:
        get = asyncio.ensure_future(queue.get())
        await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
        if get.done():
            yield get.result()
            continue
        get.cancel()
        while not queue.empty():
            yield queue.get_nowait()
        return


async def agent(ctx: TurnContext, instructions: str, items: list[dict], tools: dict[str, Tool],
                usage: dict[str, int], stream_text: bool = True) -> AsyncIterator[Event]:
    """Runs model steps until the model answers without tool calls. New output is appended to `items`."""
    specs = [t.spec() for t in tools.values()]
    for _ in range(MAX_STEPS):
        response = None
        stream = await http.llm().responses.create(
            model=http.model(), instructions=instructions, input=[_input_item(i) for i in items], tools=specs,
            reasoning={'effort': REASONING_EFFORT}, store=False, include=['reasoning.encrypted_content'], stream=True,
        )
        async for event in stream:
            if event.type == 'response.output_text.delta' and stream_text:
                yield {'type': 'text.delta', 'text': event.delta}
            elif event.type in ('response.completed', 'response.incomplete'):
                response = event.response
            elif event.type in ('response.failed', 'error'):
                raise RuntimeError(getattr(getattr(event, 'response', None), 'error', None) or getattr(event, 'message', 'model error'))
        if response is None:
            raise RuntimeError('The model stream ended without a response.')
        if response.usage:
            usage['input_tokens'] = usage.get('input_tokens', 0) + response.usage.input_tokens
            usage['output_tokens'] = usage.get('output_tokens', 0) + response.usage.output_tokens
        output = [o.model_dump(exclude_none=True) for o in response.output]
        items += output
        calls = [o for o in output if o['type'] == 'function_call']
        if not calls:
            return

        queue: asyncio.Queue = asyncio.Queue()
        step_ctx = replace(ctx, emit=queue.put_nowait)  # cards from this step's tools come out of this stream

        async def call(c: dict) -> dict:
            try:
                args = json.loads(c.get('arguments') or '{}')
            except ValueError:
                args = {}
            queue.put_nowait({'type': 'tool.start', 'call_id': c['call_id'], 'name': c['name'], 'args': args})
            status, result = await run(tools, step_ctx, c['name'], c.get('arguments') or '{}')
            queue.put_nowait({'type': 'tool.end', 'call_id': c['call_id'], 'name': c['name'], 'status': status, 'result': result[:500]})
            return {'type': 'function_call_output', 'call_id': c['call_id'], 'output': result}

        async def run_step() -> list[dict]:
            # Calls that may wait on the user run one at a time, in order; the rest of the step concurrently.
            first = [await call(c) for c in calls if tools.get(c['name']) and tools[c['name']].sequential]
            rest = await asyncio.gather(*(call(c) for c in calls if not (tools.get(c['name']) and tools[c['name']].sequential)))
            done = {o['call_id']: o for o in [*first, *rest]}
            return [done[c['call_id']] for c in calls]

        task = asyncio.ensure_future(run_step())
        async for event in _drain(task, queue):
            if event['type'] == 'card' or stream_text:
                yield event
        items += task.result()
    items.append({'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': f'(Stopped after {MAX_STEPS} steps without a final answer.)'}]})


def _delegate_tool(instructions: str, tools: dict[str, Tool], usage: dict[str, int]) -> Tool:
    async def delegate_task(ctx: TurnContext, task: str, context: str = '') -> str:
        """
        Delegate focused work to a parallel sub-agent using the current tools.

        :param task: The specific task for the sub-agent to complete
        :param context: Relevant context, decisions, or file paths for the task
        :return: Foreground result text
        """
        prompt = f'{task}\n\n## Context\n{context}' if context else task
        items = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}]
        sub_tools = {n: t for n, t in tools.items() if n != 'delegate_task'}  # no recursion
        async with _subagents:
            try:
                async for event in agent(ctx, f'{instructions}\n\n{SUBAGENT_PROMPT}', items, sub_tools, usage, stream_text=False):
                    ctx.emit(event)  # only cards come out of a sub-agent: onto the lead's stream
            except Exception as e:
                return f'Error: {e}'
        return text_of(items) or 'Sub-agent produced no output.'

    return tool(delegate_task)


async def turn_tools(ctx: TurnContext, instructions: str, usage: dict[str, int]) -> dict[str, Tool]:
    pack = apps.get(ctx.app_id)
    tools = [*pack.tools(), ask_user.TOOL, *web.TOOLS, *await sandbox.tools(ctx)]
    by_name = {t.name: t for t in tools}
    by_name['delegate_task'] = _delegate_tool(instructions, by_name, usage)
    return by_name


async def run_turn(ctx: TurnContext, history: list[dict], message: dict) -> AsyncIterator[Event]:
    """One chat turn. history: the items earlier turns' `items` events gave; message: context.user_message(...)."""
    usage: dict[str, int] = {}
    turn_items = [message]
    try:
        pack = apps.get(ctx.app_id)
        instructions = await pack.instructions(ctx) + await sandbox.instructions(ctx)
        tools = await turn_tools(ctx, instructions, usage)
        items = [*history, message]
        start = len(items)
        async for event in agent(ctx, instructions, items, tools, usage):
            yield event
        turn_items += items[start:]
        request = ' '.join(p.get('text', '') for p in message['content'] if isinstance(p, dict))
        for check in await pack.after_turn(ctx, request, text_of(turn_items)):
            yield check
            turn_items.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': check['note']}]})
    except Exception as e:
        yield {'type': 'error', 'message': f'{type(e).__name__}: {e}'}
    yield {'type': 'items', 'items': turn_items}
    yield {'type': 'done', 'usage': usage}
