"""Shared OpenAI access for the LLM steps (one place to configure, and to mock in tests)."""

import base64

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings


class LLMUnavailable(Exception):
    """No API key configured, or the LLM call failed. Maps to HTTP 503 `llm_unavailable`."""


def make_client(settings: Settings) -> AsyncOpenAI:
    if not settings.openai_api_key:
        raise LLMUnavailable("No OPENAI_API_KEY is configured on the server.")
    return AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.openai_timeout_s, max_retries=settings.openai_max_retries
    )


def completion_options(settings: Settings) -> dict:
    """Extra chat-completion arguments shared by every LLM call (reasoning effort for gpt-5* models)."""
    return {"reasoning_effort": settings.openai_reasoning_effort} if settings.openai_reasoning_effort else {}


async def structured[T: BaseModel](
    settings: Settings,
    system: str,
    user: str,
    schema: type[T],
    client: AsyncOpenAI | None = None,
    images: list[bytes] | None = None,
) -> T:
    """One structured-output completion (optionally with PNG images for a vision model); any failure becomes
    LLMUnavailable."""
    client = client or make_client(settings)
    content: str | list[dict] = user
    if images:
        content = [{"type": "text", "text": user}] + [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode(), "detail": "high"}}
            for png in images
        ]  # fmt: skip
    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format=schema,
            **completion_options(settings),
        )
    except (OpenAIError, ValidationError, ValueError) as exc:
        raise LLMUnavailable(f"The LLM request failed: {type(exc).__name__}: {exc}") from exc
    message = completion.choices[0].message
    if message.parsed is None:
        raise LLMUnavailable(f"The LLM returned no usable answer ({message.refusal or 'empty response'}).")
    return message.parsed
