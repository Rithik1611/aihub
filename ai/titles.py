"""A short chat title from the first exchange (LangChain chat model, Pydantic output, no tools)."""

from pydantic import BaseModel, Field

from ai.langchain import structured

PROMPT = """### Task:
Generate a concise title summarizing the chat history.
### Guidelines:
- The title should clearly represent the main theme or subject of the conversation.
- Keep it short: 2-4 words is best.
- Do not use emojis, quotation marks, or special formatting.
- Write the title in the chat's primary language; default to English if multilingual.
- Prioritize accuracy over creativity.
### Chat History:
<chat_history>
USER: %s
ASSISTANT: %s
</chat_history>"""


class Title(BaseModel):
    title: str = Field(description='2-4 words')


async def make_title(user_text: str, reply: str) -> str:
    """Falls back to the start of the user's message when the model call fails."""
    fallback = ' '.join(user_text.split()[:6])[:60] or 'New chat'
    try:
        out = await structured(Title).ainvoke(PROMPT % (user_text[:2000], reply[:2000]))
        return out.title.strip()[:80] or fallback
    except Exception:
        return fallback
