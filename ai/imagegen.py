"""generate_image: the model draws a new illustration (OpenAI image model) and shows it in the chat.

Unlike search_images these are NOT real photos: the reply must say so. The picture is saved under
image_dir/<user id>/<random>.png and served by the backend (GET /api/chat/images/...) to its owner only, so the
chat shows a URL on our own origin (the FE sanitizer allows exactly that shape) and no base64 sits in the history.
The key stays here; none without IMAGE_MODEL. Per-user hourly limit, bounded prompt, old files swept on write.
"""

import base64
import json
import re
import secrets
import time
from collections import deque
from pathlib import Path

import openai
from openai import AsyncOpenAI

from ai.context import TurnContext
from ai.settings import settings
from ai.tools import Tool, tool

MAX_PROMPT_CHARS = 1500
MAX_BYTES = 8_000_000
TIMEOUT_S = 120
PNG = b'\x89PNG\r\n\x1a\n'
NAME = re.compile(r'^[0-9a-f]{32}\.png$')
STYLE = ('Clean technical illustration on a plain white background, clear flat shading, no watermark. '
         'Keep any text labels few, short and correctly spelled.')
_usage: dict[str, deque[float]] = {}  # ponytail: per process; a shared store (Valkey) when aihub runs several replicas
_client: AsyncOpenAI | None = None


def _may_generate(user_id: str) -> bool:
    now, limit = time.time(), settings().image_per_user_per_hour
    if user_id not in _usage and len(_usage) >= 1000:
        _usage.pop(next(iter(_usage)))
    times = _usage.setdefault(user_id, deque())
    while times and now - times[0] > 3600:
        times.popleft()
    if len(times) >= limit:
        return False
    times.append(now)
    return True


def _sweep(root: Path) -> None:
    cutoff = time.time() - settings().image_retention_days * 86400
    for f in root.glob('*/*.png'):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass  # gone already, or in use: the next sweep gets it


def image_path(user_id: str, name: str) -> Path | None:
    """The file for (user, name), or None for anything that is not exactly one of our files (the backend route uses this)."""
    if not NAME.match(name) or not re.fullmatch(r'[0-9a-f-]{36}', user_id):
        return None
    path = settings().image_dir / user_id / name
    return path if path.is_file() else None


def _images() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = settings()
        _client = AsyncOpenAI(base_url=s.image_base_url or s.luna_base_url,
                              api_key=(s.image_api_key or s.luna_api_key).get_secret_value(), timeout=TIMEOUT_S, max_retries=1)
    return _client


async def generate_image(ctx: TurnContext, prompt: str, size: str = 'square') -> str:
    """
    Draw a NEW illustration for the user. Use it only when the user asks you to draw, generate, create or
    illustrate something, or asks for a concept picture that no photo shows. For what an existing cable or part
    looks like, use search_images instead. It is slow and costs money: call it at most once per reply.
    Write the prompt yourself, in English, specific: the subject, the layers or parts in order (materials, colours),
    the view (cross-section, side view) and what to label. Do not put in anything you do not know.
    Show the result in your reply with the markdown returned, and say it is an AI-generated illustration:
    its construction details may be wrong and it is not a drawing of a real design.

    :param prompt: what to draw, in a few precise sentences
    :param size: square, wide or tall
    :return: markdown image, or an error to explain to the user
    """
    prompt = (prompt or '').strip()
    if not prompt:
        return json.dumps({'error': 'Give a description of what to draw.'})
    if not _may_generate(ctx.user_id):
        return json.dumps({'error': f'Image limit reached ({settings().image_per_user_per_hour} per hour). Answer in words, or try again later.'})
    dims = {'square': '1024x1024', 'wide': '1536x1024', 'tall': '1024x1536'}.get(size, '1024x1024')
    try:
        r = await _images().images.generate(model=settings().image_model, prompt=f'{prompt[:MAX_PROMPT_CHARS]}\n\n{STYLE}',
                                            size=dims, n=1, quality='medium')  # type: ignore[arg-type]
        raw = base64.b64decode((r.data or [None])[0].b64_json or '', validate=True)  # type: ignore[union-attr]
    except openai.BadRequestError:
        return json.dumps({'error': 'The image service refused this request (content policy or invalid prompt). Say so; do not retry with a disguised prompt.'})
    except Exception as e:
        return json.dumps({'error': f'Image generation failed ({type(e).__name__}).'})  # never echo the message: it can carry the endpoint
    if not raw.startswith(PNG) or len(raw) > MAX_BYTES:
        return json.dumps({'error': 'Image generation returned an unusable file.'})
    folder = settings().image_dir / ctx.user_id
    folder.mkdir(parents=True, exist_ok=True)
    name = f'{secrets.token_hex(16)}.png'
    (folder / name).write_bytes(raw)
    _sweep(settings().image_dir)
    return f'![AI-generated illustration]({settings().image_public_url.rstrip("/")}/api/chat/images/{ctx.user_id}/{name})'


def tools() -> list[Tool]:
    return [tool(generate_image)] if settings().image_model else []
