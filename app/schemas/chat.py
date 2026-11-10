from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20

MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: MessageText


class ChatRequest(BaseModel):
    message: MessageText = Field(description="The new user message.")
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=MAX_HISTORY_MESSAGES,
        description="Earlier messages, oldest first. The caller stores and sends these.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "How do I start a new improvement initiative?",
                "history": [],
            }
        }
    }


class ChatResponse(BaseModel):
    reply: str
    model: str


class QuickQuestionsResponse(BaseModel):
    questions: list[str]
