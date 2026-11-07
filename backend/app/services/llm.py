"""Shared OpenAI access for the LLM steps (one place to configure, and to mock in tests)."""

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings


class LLMUnavailable(Exception):
    """No API key configured, or the LLM call failed. Maps to HTTP 503 `llm_unavailable`."""


def make_client(settings: Settings) -> AsyncOpenAI:
    if not settings.openai_api_key:
        raise LLMUnavailable("No OPENAI_API_KEY is configured on the server.")
    return AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_s)


async def structured[T: BaseModel](
    settings: Settings, system: str, user: str, schema: type[T], client: AsyncOpenAI | None = None
) -> T:
    """One structured-output completion; any failure becomes LLMUnavailable."""
    client = client or make_client(settings)
    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
        )
    except (OpenAIError, ValidationError, ValueError) as exc:
        raise LLMUnavailable(f"The LLM request failed: {type(exc).__name__}: {exc}") from exc
    message = completion.choices[0].message
    if message.parsed is None:
        raise LLMUnavailable(f"The LLM returned no usable answer ({message.refusal or 'empty response'}).")
    return message.parsed
