"""The clamp is what stops a malformed AI response bloating the JSONB column."""

from app.chat.metadata import (
    MAX_OPTIONS,
    MAX_QUESTION_LEAD,
    MAX_QUESTIONS,
    MAX_STATE_BYTES,
    MAX_SUGGESTIONS,
    MAX_TASK_DETAILS,
    MAX_TASKS,
    MAX_TEXT,
    clamp_metadata,
)
from app.chat.schemas import ChatMessageMetadata, ChatQuestion, ChatTask, TaskDetail


def test_clamps_task_count_and_text() -> None:
    metadata = ChatMessageMetadata(
        tasks=[
            ChatTask(
                key="k" * (MAX_TEXT + 50),
                label="l",
                amount="a",
                details=[TaskDetail(label="d", meta="m")] * (MAX_TASK_DETAILS + 5),
            )
        ]
        * (MAX_TASKS + 10)
    )
    clamped = clamp_metadata(metadata)
    assert clamped.tasks is not None
    assert len(clamped.tasks) == MAX_TASKS
    assert len(clamped.tasks[0].key) == MAX_TEXT
    assert len(clamped.tasks[0].details) == MAX_TASK_DETAILS


def test_clamps_questions_and_options() -> None:
    metadata = ChatMessageMetadata(
        questions=[ChatQuestion(q="?", options=["o"] * (MAX_OPTIONS + 5))] * (MAX_QUESTIONS + 5)
    )
    clamped = clamp_metadata(metadata)
    assert clamped.questions is not None
    assert len(clamped.questions) == MAX_QUESTIONS
    assert len(clamped.questions[0].options) == MAX_OPTIONS


def test_clamps_suggestions_and_question_lead() -> None:
    metadata = ChatMessageMetadata(
        suggestions=["s" * (MAX_TEXT + 10)] * (MAX_SUGGESTIONS + 5),
        questionLead="q" * (MAX_QUESTION_LEAD + 100),
    )
    clamped = clamp_metadata(metadata)
    assert clamped.suggestions is not None
    assert len(clamped.suggestions) == MAX_SUGGESTIONS
    assert len(clamped.suggestions[0]) == MAX_TEXT
    assert clamped.question_lead is not None
    assert len(clamped.question_lead) == MAX_QUESTION_LEAD


def test_drops_oversized_state() -> None:
    """Oversized state is dropped whole - truncating JSON would store invalid data."""
    metadata = ChatMessageMetadata(state={"blob": "x" * (MAX_STATE_BYTES + 1000)})
    assert clamp_metadata(metadata).state is None


def test_keeps_state_that_fits() -> None:
    metadata = ChatMessageMetadata(state={"active_enquiry": {"id": "e1"}})
    assert clamp_metadata(metadata).state == {"active_enquiry": {"id": "e1"}}


def test_false_flags_are_dropped_not_stored() -> None:
    clamped = clamp_metadata(ChatMessageMetadata(stopped=False, complete=False))
    assert clamped.stopped is None
    assert clamped.complete is None


def test_true_flags_survive() -> None:
    clamped = clamp_metadata(ChatMessageMetadata(stopped=True, complete=True))
    assert clamped.stopped is True
    assert clamped.complete is True


def test_empty_metadata_stays_empty() -> None:
    clamped = clamp_metadata(ChatMessageMetadata())
    assert clamped.model_dump(by_alias=True, exclude_none=True) == {}


def test_question_lead_accepts_both_spellings_and_stores_camel_case() -> None:
    """Input takes either spelling; storage matches cableCORE so history can migrate verbatim."""
    from_ai = ChatMessageMetadata.model_validate({"questionLead": "Please confirm."})
    from_api = ChatMessageMetadata.model_validate({"question_lead": "Please confirm."})
    assert from_ai.question_lead == "Please confirm."
    assert from_ai == from_api
    assert clamp_metadata(from_ai).model_dump(by_alias=True, exclude_none=True) == {"questionLead": "Please confirm."}


def test_metadata_column_writes_sql_null_not_json_null() -> None:
    """A message with no metadata must store SQL NULL, as cableCORE does.

    Without none_as_null SQLAlchemy writes JSON `null` into the JSONB column, and
    `where metadata is null` then silently matches nothing.
    """
    from app.chat.models import ChatMessage

    column_type = ChatMessage.__table__.c.metadata.type
    assert getattr(column_type, "none_as_null", False) is True
