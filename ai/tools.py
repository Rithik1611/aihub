"""Tools: a Python function becomes a model tool with a Pydantic argument model.

The function's docstring is the description and `:param x:` lines describe the arguments; its signature
(types, Literal choices, Annotated[..., Field(...)] limits, defaults) becomes a Pydantic model. The model's
JSON schema is what the LLM sees, and every call's arguments are validated against it before the function
runs. The first argument is the turn's TurnContext; the LLM never sees it.
"""

import inspect
import json
import re
import types
import typing
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ai.context import TurnContext
from ai.settings import settings

RESULT_MAX_CHARS = settings().tool_result_max_chars

ToolFn = Callable[..., Awaitable[Any]]  # fn(ctx, **args)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema the LLM sees
    fn: ToolFn
    # Waits on the user (an approval card, a question): runs alone, in order; other calls of a step run concurrently.
    sequential: bool = False
    args_model: type[BaseModel] | None = None  # None: the callee validates (sandbox tools, from its own OpenAPI)

    def spec(self) -> dict[str, Any]:
        return {'type': 'function', 'name': self.name, 'description': self.description, 'parameters': self.parameters}


def _allows_none(annotation: Any) -> bool:
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _allows_none(typing.get_args(annotation)[0])
    return annotation is Any or (origin in (typing.Union, types.UnionType) and type(None) in typing.get_args(annotation))


def _clean(node: Any, defs: dict[str, Any]) -> Any:
    """Pydantic's schema, flattened for the LLM: $refs inlined, no titles/defaults, `X | None` as X."""
    if isinstance(node, list):
        return [_clean(n, defs) for n in typing.cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    schema = typing.cast(dict[str, Any], node)
    if '$ref' in schema:
        return _clean(defs[schema['$ref'].split('/')[-1]], defs)
    options = schema.get('anyOf')
    if isinstance(options, list):
        kept = [o for o in typing.cast(list[Any], options) if o != {'type': 'null'}]
        if len(kept) == 1:
            extra = {k: v for k, v in schema.items() if k not in ('anyOf', 'default', 'title')}
            return {**_clean(kept[0], defs), **extra}
    return {k: _clean(v, defs) for k, v in schema.items() if k not in ('title', 'default', '$defs')}


def tool(fn: ToolFn, sequential: bool = False, name: str = '') -> Tool:
    name = name or fn.__name__
    doc = inspect.getdoc(fn) or ''
    description = doc.split('\n:param')[0].split('\n:return')[0].strip()
    param_docs = {m.group(1): ' '.join(m.group(2).split()) for m in re.finditer(r':param (\w+):(.*?)(?=\n:param|\n:return|\Z)', doc, re.S)}
    hints = typing.get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for pname, param in list(inspect.signature(fn).parameters.items())[1:]:  # [0] is ctx
        annotation = hints.get(pname, str)
        required = param.default is inspect.Parameter.empty
        if not required and param.default is None and not _allows_none(annotation):
            annotation = annotation | None  # `x: list[str] = None`: an explicit null from the LLM is fine too
        fields[pname] = (annotation, Field(... if required else param.default, description=param_docs.get(pname)))
    model = create_model(f'{name}_args', __config__=ConfigDict(extra='ignore'), **fields)
    raw = model.model_json_schema()
    parameters = _clean(raw, raw.get('$defs', {}))
    parameters.setdefault('required', [])
    return Tool(name, description, parameters, fn, sequential, model)


def cap(text: str) -> str:
    """One huge tool output (a full table, a long log) must not overflow the model's context."""
    if len(text) <= RESULT_MAX_CHARS:
        return text
    half = RESULT_MAX_CHARS // 2
    return (text[:half] + f'\n\n[... {len(text) - RESULT_MAX_CHARS} characters cut: the output was too long. '
            'Write large output to a file and print only a summary or the rows you need ...]\n\n' + text[-half:])


def _problems(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}" for err in e.errors()][:20]


async def run(tools: dict[str, Tool], ctx: TurnContext, name: str, arguments: str) -> tuple[str, str]:
    """-> (status completed|failed, result text for the model). Never raises."""
    t = tools.get(name)
    if not t:
        return 'failed', f'Unknown tool {name!r}.'
    try:
        raw = json.loads(arguments or '{}')
        if not isinstance(raw, dict):
            raise ValueError('arguments must be a JSON object')
        if t.args_model:
            args = t.args_model.model_validate(raw)
            kwargs = {k: getattr(args, k) for k in type(args).model_fields}
        else:
            kwargs = {k: v for k, v in typing.cast(dict[str, Any], raw).items() if k in t.parameters.get('properties', {})}
    except ValidationError as e:
        return 'failed', json.dumps({'error': 'Invalid arguments; nothing ran.', 'problems': _problems(e)}, ensure_ascii=False)
    except ValueError as e:
        return 'failed', json.dumps({'error': f'Invalid arguments: {e}'}, ensure_ascii=False)
    try:
        result = await t.fn(ctx, **kwargs)
    except Exception as e:
        return 'failed', json.dumps({'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False)
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    failed = text.startswith('{"error"') or text.startswith('Error:')
    return ('failed' if failed else 'completed'), cap(text)
