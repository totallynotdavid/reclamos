"""A stand-in for the hosted language model.

It records every prompt it is sent and replies with what `reply` returns, billed
at fixed token counts.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from reclamos_api.llm.client import Completion, Prompt


@dataclass
class FakeModel:
    reply: Callable[[Prompt], str]
    input_tokens: int = 10
    output_tokens: int = 20
    stop_reason: str = "end_turn"
    error: BaseException | None = None
    # Set to hold every call until it is set.
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    prompts: list[Prompt] = field(default_factory=list[Prompt])

    async def complete(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return Completion(
            text=self.reply(prompt),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            stop_reason=self.stop_reason,
        )


def as_rewritten(text: str) -> str:
    """`text` as a model may copy it: upper case, one space between words and
    curly quotes around it."""
    return chr(0x201C) + " ".join(text.split()).upper() + chr(0x201D)
