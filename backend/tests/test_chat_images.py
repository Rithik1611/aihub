import uuid

from app.chat import files
from app.chat.models import ChatFile


def _row(name: str, size: int) -> ChatFile:
    return ChatFile(id=uuid.uuid4(), account_id=uuid.uuid4(), session_id=uuid.uuid4(), name=name, mime="x", size=size)


def test_image_part_only_for_model_image_types() -> None:
    assert files.image_part(_row("shot.png", 3), b"abc") == {
        "type": "input_image",
        "image_url": "data:image/png;base64,YWJj",
    }
    assert files.image_part(_row("scan.tiff", 3), b"abc") is None
    assert files.image_part(_row("big.jpg", 3), b"x" * (files.IMAGE_MAX_BYTES + 1)) is None


def test_stored_history_keeps_a_reference_not_the_image_data() -> None:
    file_id = uuid.uuid4()
    url = "data:image/png;base64,YWJj"
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "what is this"}, {"type": "input_image", "image_url": url}],
        },
        {"type": "function_call_output", "call_id": "1", "output": "x"},
    ]
    stored = files.without_image_data(items, {url: file_id})
    assert stored[0]["content"][1]["image_url"] == f"{files.FILE_REF}{file_id}"
    assert items[0]["content"][1]["image_url"] == url  # the live turn is untouched
    assert stored[1] == items[1]
