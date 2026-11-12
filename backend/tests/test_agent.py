"""The agent adapter's pure parts: history from stored messages, card <-> frontend questions, sizes."""

import uuid
from datetime import datetime, timezone
from typing import Any

from ai.context import ApprovalCard, Question, QuestionCard, QuestionOption
from app.chat import agent
from app.chat.models import ChatMessage


def msg(sender: str, content: str, items: dict[str, Any] | None = None) -> ChatMessage:
    return ChatMessage(id=uuid.uuid4(), session_id=uuid.uuid4(), sender=sender, content=content,
                       agent_items=items, created_at=datetime.now(timezone.utc))


def user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def turn_items(text: str, answer: str) -> list[dict[str, Any]]:
    return [user(text), {"type": "reasoning", "id": "rs", "encrypted_content": "x"},
            {"type": "function_call", "call_id": "c", "name": "cc_get", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c", "output": "{}"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}]


def test_history_replays_agent_turns_and_skips_what_they_cover() -> None:
    legacy_q, legacy_a = msg("human", "old question"), msg("ai", "old answer")  # migrated from cableCORE
    q = msg("human", "change it")
    card = msg("ai", "Approve 1 change?")
    ans = msg("human", "Approve")
    final = msg("ai", "Done.", {"covered": [str(q.id), str(card.id), str(ans.id)], "items": turn_items("change it", "Done.")})
    items = agent.history_items([legacy_q, legacy_a, q, card, ans, final])
    assert items[0]["content"][0]["text"] == "old question" and items[1]["role"] == "assistant"
    assert [i["type"] for i in items[2:]] == ["message", "reasoning", "function_call", "function_call_output", "message"]


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

    card = QuestionCard(questions=[
        Question(id="plant", question="Which plant?", options=[QuestionOption(label="1300", description="a"), QuestionOption(label="2000", description="b")]),
        Question(id="std", question="Which standard?", options=[QuestionOption(label="IEC", description="c"), QuestionOption(label="BS", description="d")]),
    ])
    asked = agent.card_questions(card)
    assert [q.q for q in asked] == ["Which plant?", "Which standard?"] and asked[0].descriptions == ["a", "b"]
    # several questions: the card sends "question answer" lines
    assert agent.answer_for(card, "Which plant? 1300\nWhich standard? IEC") == {"status": "answered", "answers": {"plant": "1300", "std": "IEC"}}
    one = QuestionCard(questions=[card.questions[0]])
    assert agent.answer_for(one, "2000") == {"status": "answered", "answers": {"plant": "2000"}}


def test_stored_turns_stay_bounded() -> None:
    huge = [user("q"), {"type": "function_call_output", "call_id": "c", "output": "x" * (agent.ITEMS_MAX_BYTES + 10)}]
    kept = agent._bounded(huge)
    assert len(kept[1]["output"]) < 5000


def test_step_labels_match_the_panel() -> None:
    assert agent.step_label("cc_search", {"kind": "design", "query": "conductorCSA=95"}) == "Searched designs: conductorCSA=95"
    assert agent.step_label("cc_action", {"actions": [{"id": "design.copy"}]}) == "Ran design.copy"
    assert agent.step_label("something_new", {}) == "something new"
