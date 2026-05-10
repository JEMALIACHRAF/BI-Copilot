"""Thin OpenAI wrapper with JSON-mode helpers and token accounting.

Centralizing this in one place means:
  • prompt templates and structured output parsing live next to each other,
  • we can swap the model (or the provider) in one file without touching agents,
  • token counts and costs aggregate cleanly into the graph state.
"""

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

# GPT-4o pricing as of late 2024: $2.50 / 1M input, $10.00 / 1M output
_INPUT_PRICE_PER_1K = 0.0025
_OUTPUT_PRICE_PER_1K = 0.010


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def parse_json(self) -> dict[str, Any]:
        """Parse the response content as JSON, tolerating markdown fences."""
        text = self.content.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:].lstrip("\n")
            text = text.rstrip().rstrip("`").rstrip()
        return json.loads(text)


class LLMClient:
    """OpenAI chat-completion wrapper with retries, JSON mode, and cost tracking."""

    def __init__(self, client: OpenAI | None = None) -> None:
        self._settings = get_settings()
        self._client = client or OpenAI(
            api_key=self._settings.openai_api_key,
            timeout=self._settings.llm_timeout_seconds,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        """One round-trip to the LLM. Returns content + token accounting."""
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": (
                temperature if temperature is not None else self._settings.llm_temperature
            ),
            "max_tokens": self._settings.llm_max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cost = (
            input_tokens / 1000 * _INPUT_PRICE_PER_1K
            + output_tokens / 1000 * _OUTPUT_PRICE_PER_1K
        )

        logger.debug(
            "llm.completion",
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 4),
        )

        return LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
