"""Chat uploads: the bytes in MinIO (the same MinIO cableCORE_BE uses, bucket `ai-chat-files`), the extracted text
in chat_file, so the whole document goes into the conversation once and stays in its history.

Object key: {account}/{session}/{file id}/{name}. Nothing here is served to the browser; the sandbox still gets
its own copy for analysis (xlsx calculations, making files).
"""

import asyncio
import io
import logging
import uuid
from functools import lru_cache
from typing import Final

import ai.documents
from minio import Minio
from sqlalchemy import select

from app.chat.models import ChatFile
from app.core.config import get_settings
from app.core.db import SessionFactory

logger = logging.getLogger(__name__)

# About 30k tokens: a document up to this size goes into the conversation whole.
INLINE_CHARS: Final = 120_000


@lru_cache
def _client() -> Minio | None:
    s = get_settings()
    if not s.minio_endpoint:
        return None
    return Minio(
        f"{s.minio_endpoint}:{s.minio_port}",
        access_key=s.minio_user_name,
        secret_key=s.minio_password.get_secret_value(),
        secure=s.minio_use_ssl,
    )


def _put(key: str, content: bytes, mime: str) -> None:
    client = _client()
    if client is None:
        return
    bucket = get_settings().minio_bucket
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(bucket, key, io.BytesIO(content), len(content), content_type=mime)


def _remove(keys: list[str]) -> None:
    client = _client()
    if client is None:
        return
    for key in keys:
        client.remove_object(get_settings().minio_bucket, key)


async def store(
    account_id: uuid.UUID, session_id: uuid.UUID, name: str, content: bytes, mime: str
) -> tuple[ChatFile, str]:
    """Keeps the upload and its text; -> (row, problem). A storage or extraction failure is reported, not raised:
    the turn goes on with the sandbox copy."""
    file_id = uuid.uuid4()
    key = f"{account_id}/{session_id}/{file_id}/{name}"
    problem = ""
    try:
        await asyncio.to_thread(_put, key, content, mime)
    except Exception as exc:
        logger.warning("chat upload %s not stored in MinIO: %s", name, exc)
        key, problem = "", "The file could not be stored for later chats."
    try:
        text, pages = await asyncio.to_thread(ai.documents.extract, name, content)
    except ValueError as exc:
        text, pages, problem = "", 0, str(exc)
    row = ChatFile(
        id=file_id,
        account_id=account_id,
        session_id=session_id,
        name=name[:255],
        mime=mime[:127],
        size=len(content),
        object_key=key or None,
        pages=pages,
        text=text,
    )
    async with SessionFactory() as db:
        db.add(row)
        await db.commit()
    return row, problem


def document_block(row: ChatFile, sandbox_path: str | None, problem: str) -> str:
    """The document as the model reads it in the user's message: whole when it fits, else its start and where
    the rest is."""
    where = f" The file is also at {sandbox_path} in the sandbox." if sandbox_path else ""
    if not row.text:
        reason = problem or "it has no text to read (e.g. an image or a scan)"
        return f'[Document "{row.name}": its text could not be taken: {reason}.{where}]'
    unit = "sheets" if row.name.lower().endswith((".xlsx", ".xlsm")) else "pages"
    head = f'[Document "{row.name}", {row.pages} {unit}, {len(row.text)} characters'
    if len(row.text) <= INLINE_CHARS:
        return f"{head}, full text:]\n{row.text}\n[end of {row.name}]"
    return (
        f"{head}; the first {INLINE_CHARS} characters:]\n{row.text[:INLINE_CHARS]}\n"
        f"[{row.name} continues; read the rest from the sandbox copy.{where}]"
    )


async def remove_session_files(session_id: uuid.UUID) -> None:
    """Deletes a session's uploads from MinIO (the rows go with the session)."""
    async with SessionFactory() as db:
        keys = [
            k for k in (await db.scalars(select(ChatFile.object_key).where(ChatFile.session_id == session_id))) if k
        ]
    try:
        await asyncio.to_thread(_remove, keys)
    except Exception as exc:
        logger.warning("chat files of session %s left in MinIO: %s", session_id, exc)
