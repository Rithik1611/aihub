"""generate_image against a fake image client: file saved under the user, owner-only lookup, limits, errors never echo."""

import base64
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import SecretStr

from ai import imagegen
from ai.context import TurnContext

USER = '3f1c2a4e-9b7d-4c1a-8e2f-0a1b2c3d4e5f'
PNG_B64 = base64.b64encode(imagegen.PNG + b'data').decode()


def ctx(user=USER):
    return TurnContext(user_id=user, user_email='u', cc_token='', chat_id='c', wait_answer=None)  # type: ignore[arg-type]


def fake(monkeypatch, tmp_path, result=None, error=None, limit=5):
    calls = []

    async def generate(**kw):
        calls.append(kw)
        if error:
            raise error
        return result or SimpleNamespace(data=[SimpleNamespace(b64_json=PNG_B64)])

    monkeypatch.setattr(imagegen, '_client', SimpleNamespace(images=SimpleNamespace(generate=generate)))
    monkeypatch.setattr(imagegen, 'settings', lambda: SimpleNamespace(
        image_model='gpt-image-1', image_dir=tmp_path, image_public_url='http://aihub:8000/', image_retention_days=30,
        image_per_user_per_hour=limit, image_api_key=SecretStr('sk-secret')))
    imagegen._usage.clear()
    return calls


async def test_saves_the_picture_for_its_owner_only(monkeypatch, tmp_path):
    calls = fake(monkeypatch, tmp_path)
    md = await imagegen.generate_image(ctx(), '3-core XLPE cable cross-section', 'wide')
    url = md.split('(')[1].rstrip(')')
    prefix = f'http://aihub:8000/api/chat/images/{USER}/'
    assert md.startswith('![AI-generated illustration](') and url.startswith(prefix)
    name = url[len(prefix):]
    assert imagegen.image_path(USER, name).read_bytes().startswith(imagegen.PNG)
    assert imagegen.image_path('11111111-1111-1111-1111-111111111111', name) is None  # someone else's id: not found
    assert imagegen.image_path(USER, '../../etc/passwd') is None and imagegen.image_path('..', name) is None
    assert calls[0]['size'] == '1536x1024' and 'XLPE' in calls[0]['prompt']


async def test_limit_empty_prompt_and_bad_file(monkeypatch, tmp_path):
    calls = fake(monkeypatch, tmp_path, limit=1)
    assert 'error' in json.loads(await imagegen.generate_image(ctx(), '  '))
    assert '![' in await imagegen.generate_image(ctx(), 'a cable')
    assert 'limit reached' in await imagegen.generate_image(ctx(), 'another')
    assert len(calls) == 1  # the second never reached the paid API
    fake(monkeypatch, tmp_path, result=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'<svg/>').decode())]))
    assert 'unusable' in await imagegen.generate_image(ctx('u2'), 'a cable')


async def test_errors_never_echo_the_message(monkeypatch, tmp_path):
    req = httpx.Request('POST', 'https://secret-host.example/v1/images')
    refused = openai.BadRequestError('policy sk-secret', response=httpx.Response(400, request=req), body=None)
    fake(monkeypatch, tmp_path, error=refused)
    out = await imagegen.generate_image(ctx(), 'x')
    assert 'refused' in out and 'sk-secret' not in out
    fake(monkeypatch, tmp_path, error=RuntimeError('https://secret-host.example key=sk-secret'))
    out = await imagegen.generate_image(ctx(), 'x')
    assert 'RuntimeError' in out and 'secret' not in out


def test_no_model_no_tool(monkeypatch):
    monkeypatch.setattr(imagegen, 'settings', lambda: SimpleNamespace(image_model=''))
    assert imagegen.tools() == []
