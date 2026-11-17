"""The agent loop: model -> tool calls -> model, until it answers.

run_turn(ctx, history, message) is everything the backend calls for a chat turn. The model is reached
through the OpenAI Responses API (Azure OpenAI: LUNA_BASE_URL, LUNA_API_KEY, LUNA_MODEL). Nothing is
stored at the provider (store=False): reasoning travels encrypted in the items the backend keeps.
"""

import asyncio
import json
import logging
import os
from dataclasses import replace
from typing import AsyncIterator

from ai import apps, ask_user, http, imagegen, images, memory, sandbox, skills, web
from ai.context import Event, TurnContext
from ai.settings import settings
from ai.tools import Tool, request_specs, run, tool

log = logging.getLogger(__name__)

MAX_STEPS = int(os.getenv('AGENT_MAX_STEPS', '100'))
REASONING_EFFORT = os.getenv('AGENT_REASONING_EFFORT', 'medium')
SUBAGENTS_MAX_CONCURRENT = int(os.getenv('SUBAGENTS_MAX_CONCURRENT', '6'))
# Past this many input tokens in one step, older tool results are cut to a stub (the model keeps the call and
# the result's start; the newest KEEP_OUTPUTS results stay whole). A context-length error compacts harder, once.
COMPACT_AT_TOKENS = int(os.getenv('AGENT_COMPACT_TOKENS', '150000'))
KEEP_OUTPUTS = 6
STUB = '[earlier result shortened to save context; call the tool again if you need it whole:'
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


def compact(items: list[dict], keep: int = KEEP_OUTPUTS) -> int:
    """Cuts all but the newest `keep` tool results to a stub, in place; -> how many were cut."""
    outputs = [i for i in items if i.get('type') == 'function_call_output' and isinstance(i.get('output'), str)]
    cut = 0
    for item in outputs[:len(outputs) - keep] if keep else outputs:
        if len(item['output']) > 400 and not item['output'].startswith(STUB):
            item['output'] = f"{STUB} {item['output'][:300]} …]"
            cut += 1
    return cut


def _too_long(error: Exception) -> bool:
    text = str(error).lower()
    return 'context' in text and ('length' in text or 'window' in text or 'too long' in text or 'exceed' in text)


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


def _body(input_items: list[dict], specs: list[dict]) -> dict:
    """The conversation and tool list for a model call. The OpenAI SDK walks every item of `input`/`tools` to convert
    types before sending (the second biggest CPU cost under load); `extra_body` is sent as it is, so it is used
    unless LangSmith tracing is on, which reads `input` from the arguments to record the prompt."""
    if settings().langsmith_tracing:
        return {'input': input_items, 'tools': specs}
    return {'extra_body': {'input': input_items, 'tools': specs}}


async def agent(ctx: TurnContext, instructions: str, items: list[dict], tools: dict[str, Tool],
                usage: dict[str, int], stream_text: bool = True, max_steps: int = MAX_STEPS) -> AsyncIterator[Event]:
    """Runs model steps until the model answers without tool calls. New output is appended to `items`."""
    specs = request_specs(tools.values())
    retried = False
    for _ in range(max_steps):
        response = None
        try:
            stream = await http.llm().responses.create(
                model=http.model(), instructions=instructions, **_body([_input_item(i) for i in items], specs),
                reasoning={'effort': REASONING_EFFORT}, store=False, include=['reasoning.encrypted_content'], stream=True,
            )
        except Exception as e:  # noqa: BLE001 - only a context overflow is handled; anything else re-raises
            if retried or not _too_long(e) or not compact(items, keep=2):
                raise
            retried = True
            continue
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
            # Per-turn cost, kept locally (the eval runner records it per case): cached input is re-sent prefix.
            usage['cached_tokens'] = usage.get('cached_tokens', 0) + (
                getattr(getattr(response.usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0)
            usage['reasoning_tokens'] = usage.get('reasoning_tokens', 0) + (
                getattr(getattr(response.usage, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0)
            if response.usage.input_tokens > COMPACT_AT_TOKENS:
                compact(items)
        output = [o.model_dump(exclude_none=True) for o in response.output]
        items += output
        calls = [o for o in output if o['type'] == 'function_call']
        usage['steps'] = usage.get('steps', 0) + 1
        usage['tool_calls'] = usage.get('tool_calls', 0) + len(calls)
        usage['skills'] = usage.get('skills', 0) + sum(1 for c in calls if c.get('name') == 'load_skill')
        usage['searches'] = usage.get('searches', 0) + sum(1 for o in output if o['type'] == 'tool_search_call')
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
            queue.put_nowait({'type': 'tool.end', 'call_id': c['call_id'], 'name': c['name'], 'status': status, 'result': result[:3000]})
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


async def turn_tools(ctx: TurnContext, instructions: str, usage: dict[str, int],
                     guests: list[apps.Guest] | None = None) -> dict[str, Tool]:
    pack = apps.get(ctx.app_id)
    tools = [*pack.tools(), ask_user.TOOL, *memory.tools(ctx), *web.TOOLS, *images.tools(), *imagegen.tools(), *await sandbox.tools(ctx)]
    by_name = {t.name: t for t in tools}
    for guest in guests or []:  # another app's tools: deferred, so they cost nothing until tool search loads them
        by_name.update({t.name: t for t in guest.tools if t.name not in by_name})
        if guest.skills and 'load_skill' in by_name:
            by_name['load_skill'] = skills.with_skills(by_name['load_skill'], guest.skills)
    if any(t.guest for t in by_name.values()):
        by_name.setdefault('tool_search', skills.tool_search_tool())
    by_name['delegate_task'] = _delegate_tool(instructions, by_name, usage)
    return by_name


REVIEW_STEPS = 8
REVIEW_PROMPT = """[Automatic review of your answer above - not written by the user]
{findings}

Check each point now: read the stored value, lookup row or formula branch it needs, or confirm it from what you
already read. Then write ONLY a short section that starts with "**Correction / added detail:**" giving what you
found, as an addition to your answer (do not repeat the answer). If on checking your answer was already complete
and every claim in it is shown, reply with exactly: NONE"""


async def _review(ctx: TurnContext, instructions: str, items: list[dict], tools: dict[str, Tool],
                  usage: dict[str, int], findings: str) -> tuple[str, list[dict]]:
    """One more pass on the gaps the completeness check found: -> (section to add or '', the pass's items).
    Not streamed token by token: a pass that ends in NONE must leave no trace on screen."""
    prompt = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': REVIEW_PROMPT.format(findings=findings.strip())}]}
    work = [*items, prompt]
    start = len(work) - 1
    async for event in agent(ctx, instructions, work, tools, usage, stream_text=False, max_steps=REVIEW_STEPS):
        ctx.emit(event)  # only cards come out of a non-streamed pass
    text = text_of(work[start:]).strip()
    return ('' if not text or text.upper().startswith('NONE') else text), work[start:]


# The review runs after the turn is done, so nothing in it may need the user or change anything: it reads
# more data only (no web research, cards, changes, sub-agents or saved notes).
REVIEW_EXCLUDED = frozenset({'search_web', 'fetch_url', 'ask_user', 'cc_action', 'delegate_task', 'remember', 'cc_note'})


async def run_turn(ctx: TurnContext, history: list[dict], message: dict) -> AsyncIterator[Event]:
    """One chat turn. history: the items earlier turns' `items` events gave; message: context.user_message(...).
    Yields the answer and `done`; after `done`, a `correction` when the completeness review found something."""
    usage: dict[str, int] = {}
    turn_items = [message]
    try:
        pack = apps.get(ctx.app_id)
        guests = await apps.guests(ctx)  # other apps this user has (MES): a brief here, their tools deferred
        instructions = await pack.instructions(ctx) + await sandbox.instructions(ctx) + apps.briefs(guests)
        tools = await turn_tools(ctx, instructions, usage, guests)
        items = [*history, message]
        start = len(items)
        async for event in agent(ctx, instructions, items, tools, usage):
            yield event
        turn_items += items[start:]
        request = ' '.join(p.get('text', '') for p in message['content'] if isinstance(p, dict))
        reply = text_of(turn_items)
        for check in await pack.after_turn(ctx, request, reply):
            yield check
            turn_items.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': check['note']}]})
    except Exception as e:
        yield {'type': 'error', 'message': f'{type(e).__name__}: {e}'}
        yield {'type': 'items', 'items': turn_items}
        yield {'type': 'done', 'usage': usage}
        return
    yield {'type': 'items', 'items': turn_items}
    yield {'type': 'done', 'usage': usage}
    # After the turn: an answer about the user's data is checked for gaps, and a review pass adds what it finds.
    try:
        check = getattr(pack, 'coverage', None)
        calls = [i['name'] for i in turn_items if i.get('type') == 'function_call']
        found = await check(ctx, request, reply, calls) if check else None
        if found:
            readers = {n: t for n, t in tools.items() if n not in REVIEW_EXCLUDED}
            added, review_items = await _review(ctx, instructions, items, readers, usage, found['note'])
            if added:
                yield {'type': 'correction', 'text': f'\n\n{added}', 'items': review_items}
    except Exception as e:  # noqa: BLE001 - the answer stands without its review
        log.warning(f'review after the turn failed: {type(e).__name__}: {e}')
