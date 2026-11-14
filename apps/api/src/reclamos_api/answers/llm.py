"""Calls to a hosted language model, and what each one costs."""

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from reclamos_api.config import Settings


@dataclass(frozen=True, slots=True)
class Prompt:
    """One request. `schema` is the JSON schema the reply must follow."""

    model: str
    system: str
    text: str
    schema: dict[str, Any]
    max_tokens: int
    # A timeout can fire after the model ran, so the call stays billed. Set it
    # long enough for `max_tokens` of output.
    timeout_s: float = 60.0


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    # "max_tokens" means the reply was cut, so its JSON is incomplete.
    stop_reason: str | None

    def parsed(self) -> dict[str, Any] | None:
        """The reply as a JSON object, or None when it is not one."""
        try:
            value: Any = json.loads(self.text)
        except json.JSONDecodeError:
            return None
        return cast("dict[str, Any]", value) if isinstance(value, dict) else None


class NotBilled(Exception):
    """A model error raised before any token was billed, such as an HTTP error
    status. Any other error leaves the reserved cost counted."""


class LanguageModel(Protocol):
    async def complete(self, prompt: Prompt) -> Completion: ...


@dataclass(frozen=True, slots=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float

    def usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output) / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelSpec:
    price: Price
    # The `thinking` setting that turns thinking off. Without one the model
    # thinks adaptively and its thinking tokens count against `max_tokens`, so
    # a short cap can end the reply before any text is written. Haiku 5.5 takes
    # "disabled"; Sonnet 5.5 rejects it and takes "between_tools".
    # https://platform.claude.com/docs/en/api/errors, read 2026-11-14.
    thinking_off: dict[str, str]


# https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-14.
# Haiku 5.5 doubles in price above 100,000 prompt tokens; MAX_PROMPT_CHARS keeps
# every prompt far below that. A model missing here cannot be called, because
# its cost could not be capped.
MODELS = {
    "claude-haiku-5-5": ModelSpec(
        price=Price(input=0.10, output=0.50), thinking_off={"type": "disabled"}
    ),
    "claude-sonnet-5-5": ModelSpec(
        price=Price(input=2.00, output=10.00),
        thinking_off={"type": "between_tools"},
    ),
}

MAX_PROMPT_CHARS = 40_000

# Spanish runs near four characters a token. Two is the bound used before a
# call, so the reserved cost is never below what the call is billed.
CHARS_PER_TOKEN_BOUND = 2


class UnpricedModel(Exception):
    pass


def spec_of(model: str) -> ModelSpec:
    try:
        return MODELS[model]
    except KeyError:
        raise UnpricedModel(
            f"no price is known for {model!r}; known: {', '.join(MODELS)}"
        ) from None


def price_of(model: str) -> Price:
    return spec_of(model).price


def worst_case_usd(prompt: Prompt) -> float:
    """The most `prompt` can cost: every character a token, every output token
    used."""
    characters = len(prompt.system) + len(prompt.text) + len(json.dumps(prompt.schema))
    return price_of(prompt.model).usd(
        characters // CHARS_PER_TOKEN_BOUND + 1, prompt.max_tokens
    )


def model_from_settings(settings: Settings) -> "LanguageModel | None":
    """The configured model, or None when no API key is set."""
    if settings.anthropic_api_key is None:
        return None
    return AnthropicModel(settings.anthropic_api_key)


class AnthropicModel:
    """The Anthropic Messages API."""

    def __init__(self, api_key: str | None = None, *, client: Any = None) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            # Retries are left to the caller: each retry is billed again.
            client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._client = client

    async def complete(self, prompt: Prompt) -> Completion:
        from anthropic import APIStatusError

        try:
            message = await self._client.messages.create(
                model=prompt.model,
                max_tokens=prompt.max_tokens,
                system=prompt.system,
                messages=[{"role": "user", "content": prompt.text}],
                thinking=spec_of(prompt.model).thinking_off,
                output_config={
                    "format": {"type": "json_schema", "schema": prompt.schema}
                },
                timeout=prompt.timeout_s,
            )
        except APIStatusError as error:
            raise NotBilled(f"{error.status_code}: {error.message}") from error
        text = "".join(block.text for block in message.content if block.type == "text")
        return Completion(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
        )
