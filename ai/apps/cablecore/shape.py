"""Trim a large response to the fields its capability's `returns` names.

`returns` is the contract each described route already declares, e.g.
    data.{designName, isLocked, tabs[].subTabs[].attributeValues[].{id (value row id), plantAttribute.name, valueDropdown}}
Syntax: dotted paths; `[]` maps over a list; `{a, b}` groups; `a|b` alternatives; `(notes)` are ignored;
`X or Y` keeps what either names. A leading `data` matches an unwrapped response too. Missing fields are skipped
and nulls dropped, so the result is the data the model needs, in its original shape.
"""

import re
from typing import Any

_MISSING = object()


def _strip_notes(text: str) -> str:
    while True:
        stripped = re.sub(r'\s*\([^()]*\)', '', text)
        if stripped == text:
            return text
        text = stripped


def _split(text: str, sep: str) -> list[str]:
    """Split on `sep` outside {...}."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += {'{': 1, '}': -1}.get(ch, 0)
        if depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            start = i + len(sep)
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _parse(spec: str) -> list[tuple[list[tuple[list[str], bool]], Any]]:
    """`a.b[].{x, y}` -> [([(['a'], False), (['b'], True)], <sub-spec>)]"""
    items = []
    for item in _split(spec, ','):
        sub = None
        brace = item.find('{')
        if brace >= 0 and item.endswith('}'):
            sub = _parse(item[brace + 1:-1])
            item = item[:brace].rstrip('.')
        segments = []
        for seg in (s for s in item.split('.') if s):
            is_list = seg.endswith('[]')
            names = [n.split(':')[0].strip() for n in seg.removesuffix('[]').split('|')]  # `status: queued` names status
            names = [n for n in names if re.fullmatch(r'\w+', n)]
            if names:
                segments.append((names, is_list))
        if segments or sub is not None:
            items.append((segments, sub))
    return items


def _project(value: Any, items: list) -> Any:
    out: dict[str, Any] = {}
    for segments, sub in items:
        got = _walk(value, segments, sub)
        if isinstance(got, dict):
            _merge(out, got)
    return out or _MISSING


def _walk(value: Any, segments: list, sub: Any) -> Any:
    if not segments:
        if sub is None:
            return value
        if isinstance(value, list):
            rows = [r for r in (_project(v, sub) for v in value) if r is not _MISSING]
            return rows or _MISSING
        return _project(value, sub)
    if not isinstance(value, dict):
        return _MISSING
    names, is_list = segments[0]
    out: dict[str, Any] = {}
    for name in names:
        if value.get(name) is None:
            continue
        inner = value[name]
        if is_list and isinstance(inner, list):
            rows = [r for r in (_walk(v, segments[1:], sub) for v in inner) if r is not _MISSING]
            if rows:
                out[name] = rows
        else:
            got = _walk(inner, segments[1:], sub)
            if got is not _MISSING:
                out[name] = got
    return out or _MISSING


def _merge(into: dict, other: dict) -> None:
    for key, value in other.items():
        if isinstance(into.get(key), dict) and isinstance(value, dict):
            _merge(into[key], value)
        elif isinstance(into.get(key), list) and isinstance(value, list) and len(into[key]) == len(value):
            for a, b in zip(into[key], value):
                if isinstance(a, dict) and isinstance(b, dict):
                    _merge(a, b)
        else:
            into.setdefault(key, value)


def trim(value: Any, returns: str) -> Any | None:
    """The parts of `value` that `returns` names, or None when it names nothing that is there."""
    best = None
    for alternative in _split(_strip_notes(returns), ' or '):
        items = _parse(alternative)
        got = _project(value, items)
        if got is _MISSING and items and items[0][0] and items[0][0][0][0] == ['data']:
            # the contract names the API envelope (data.…); the response may already be unwrapped
            got = _project(value, [(items[0][0][1:], items[0][1]), *items[1:]])
        if got is not _MISSING and (best is None or len(str(got)) > len(str(best))):
            best = got
    return best
