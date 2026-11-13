"""The agent adapter's pure parts: history from stored messages, card <-> frontend questions, sizes."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from ai.context import ApprovalCard, Question, QuestionCard, QuestionOption

from app.chat import agent
from app.chat.models import ChatMessage


def msg(sender: str, content: str, items: dict[str, Any] | None = None) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        sender=sender,
        content=content,
        agent_items=items,
        created_at=datetime.now(UTC),
    )


def user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def turn_items(text: str, answer: str) -> list[dict[str, Any]]:
    return [
        user(text),
        {"type": "reasoning", "id": "rs", "encrypted_content": "x"},
        {"type": "function_call", "call_id": "c", "name": "cc_get", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": "{}"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]},
    ]


def test_history_replays_agent_turns_and_skips_what_they_cover() -> None:
    legacy_q, legacy_a = msg("human", "old question"), msg("ai", "old answer")  # migrated from cableCORE
    q = msg("human", "change it")
    card = msg("ai", "Approve 1 change?")
    ans = msg("human", "Approve")
    final = msg(
        "ai", "Done.", {"covered": [str(q.id), str(card.id), str(ans.id)], "items": turn_items("change it", "Done.")}
    )
    items = agent.history_items([legacy_q, legacy_a, q, card, ans, final])
    assert items[0]["content"][0]["text"] == "old question" and items[1]["role"] == "assistant"
    assert [i["type"] for i in items[2:]] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
    ]


def test_older_turns_are_replayed_as_conversation_only() -> None:
    messages = []
    for n in range(agent.FULL_TURNS + 2):
        q = msg("human", f"q{n}")
        messages += [q, msg("ai", f"a{n}", {"covered": [str(q.id)], "items": turn_items(f"q{n}", f"a{n}")})]
    items = agent.history_items(messages)
    oldest = items[:2]
    assert [i["type"] for i in oldest] == ["message", "message"]  # tool calls and reasoning dropped
    assert sum(1 for i in items if i["type"] == "function_call") == agent.FULL_TURNS


def test_cards_become_frontend_questions_and_answers_come_back() -> None:
    approval = ApprovalCard(title="Approve 1 change in cableCORE", items=[], markdown="1. **Copy**")
    assert agent.card_questions(approval)[0].options == [agent.APPROVE, agent.DECLINE]
    assert agent.answer_for(approval, "Approve") == {"approved": True}
    assert agent.answer_for(approval, "Decline") == {"approved": False}
    assert agent.answer_for(approval, "no wait") == {"approved": False}

    card = QuestionCard(
        questions=[
            Question(
                id="plant",
                question="Which plant?",
                options=[QuestionOption(label="1300", description="a"), QuestionOption(label="2000", description="b")],
            ),
            Question(
                id="std",
                question="Which standard?",
                options=[QuestionOption(label="IEC", description="c"), QuestionOption(label="BS", description="d")],
            ),
        ]
    )
    asked = agent.card_questions(card)
    assert [q.q for q in asked] == ["Which plant?", "Which standard?"] and asked[0].descriptions == ["a", "b"]
    # several questions: the card sends "question answer" lines
    assert agent.answer_for(card, "Which plant? 1300\nWhich standard? IEC") == {
        "status": "answered",
        "answers": {"plant": "1300", "std": "IEC"},
    }
    one = QuestionCard(questions=[card.questions[0]])
    assert agent.answer_for(one, "2000") == {"status": "answered", "answers": {"plant": "2000"}}


def test_stored_turns_stay_bounded() -> None:
    huge = [user("q"), {"type": "function_call_output", "call_id": "c", "output": "x" * (agent.ITEMS_MAX_BYTES + 10)}]
    kept = agent._bounded(huge)
    assert len(kept[1]["output"]) < 5000


def test_step_labels_match_the_panel() -> None:
    assert (
        agent.step_label("cc_search", {"kind": "design", "query": "conductorCSA=95"})
        == "Searched designs: conductorCSA=95"
    )
    assert agent.step_label("cc_action", {"actions": [{"id": "design.copy"}]}) == "Ran design.copy"
    assert agent.step_label("something_new", {}) == "something new"


def test_claim_refuses_a_second_turn_in_the_same_session() -> None:
    """The guard used to read _turns, then register the turn five awaits later, so two
    requests arriving together both passed it and interleaved in the transcript."""
    session, account = uuid.uuid4(), uuid.uuid4()
    agent._turns.clear()
    try:
        first = agent.claim(session, account, True, "first")
        assert first is not None
        with pytest.raises(agent.TurnBusy):
            agent.claim(session, account, False, "second")
    finally:
        agent._turns.clear()


def test_claim_returns_the_paused_turn_so_the_answer_resumes_it() -> None:
    session, account = uuid.uuid4(), uuid.uuid4()
    agent._turns.clear()
    try:
        turn = agent.claim(session, account, True, "first")
        assert turn is not None
        turn.answer = asyncio.get_event_loop_policy().new_event_loop().create_future()
        assert agent.claim(session, account, False, "the answer") is turn
    finally:
        agent._turns.clear()


def test_claim_leaves_no_slot_for_the_stub_path() -> None:
    session, account = uuid.uuid4(), uuid.uuid4()
    agent._turns.clear()
    try:
        assert agent.claim(session, account, True, "x", stub=True) is None
        assert session not in agent._turns
    finally:
        agent._turns.clear()
