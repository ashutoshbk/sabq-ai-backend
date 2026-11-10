"""Chat logic: builds the messages, calls OpenAI, returns the reply (or streams it)."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import load_prompt
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)


def _sse(payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


class ChatService:
    def __init__(self, client: AsyncOpenAI, settings: Settings, system_prompt: str | None = None):
        self._client = client
        self._settings = settings
        self._system_prompt = system_prompt or load_prompt("chat_system.md")

    # -- request building -------------------------------------------------

    def _build_input(self, req: ChatRequest) -> list[dict[str, str]]:
        messages = [{"role": m.role, "content": m.content} for m in req.history]
        messages.append({"role": "user", "content": req.message})
        return messages

    def _build_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._settings.chat_model,
            "instructions": self._system_prompt,
            "input": self._build_input(req),
            "max_output_tokens": self._settings.chat_max_output_tokens,
            "store": False,  # do not keep the conversation on OpenAI's side
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning
        return kwargs

    # -- normal reply -----------------------------------------------------

    async def reply(self, req: ChatRequest) -> ChatResponse:
        resp = await self._client.responses.create(**self._build_kwargs(req))
        text = (resp.output_text or "").strip()
        if not text:
            logger.warning("Empty chat reply (status=%s)", getattr(resp, "status", None))
            raise AppError(
                "empty_response",
                "The model returned no text. Try again, or raise CHAT_MAX_OUTPUT_TOKENS.",
                502,
            )
        return ChatResponse(reply=text, model=self._settings.chat_model)

    # -- streaming --------------------------------------------------------

    async def open_stream(self, req: ChatRequest):
        """Start the OpenAI stream. Errors here become normal HTTP errors (before any bytes)."""
        return await self._client.responses.create(**self._build_kwargs(req), stream=True)

    async def iter_sse(self, stream) -> AsyncIterator[str]:
        """Turn OpenAI stream events into Server-Sent Events.

        Events:  data: {"delta": "text"}   ...   data: [DONE]
        On a mid-stream failure:  data: {"error": {...}}  then [DONE].
        """
        try:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    yield _sse({"delta": event.delta})
                elif etype in ("error", "response.failed"):
                    message = getattr(event, "message", None) or "The model reported an error."
                    yield _sse({"error": {"code": "openai_stream_error", "message": message}})
                    break
        except Exception as exc:  # noqa: BLE001 - we must not crash the open stream
            logger.warning("Stream failed: %s: %s", type(exc).__name__, exc)
            yield _sse({"error": {"code": "stream_interrupted", "message": "The stream was interrupted."}})
        yield _sse("[DONE]")
