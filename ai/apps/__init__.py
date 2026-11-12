"""App packs: one folder per application. A pack exposes instructions(ctx), tools() and after_turn(ctx, request, reply)."""

import importlib
from types import ModuleType


def get(app_id: str) -> ModuleType:
    if not app_id.isidentifier():
        raise ValueError(f'Unknown app {app_id!r}.')
    try:
        return importlib.import_module(f'ai.apps.{app_id}')
    except ModuleNotFoundError as e:
        raise ValueError(f'Unknown app {app_id!r}.') from e
