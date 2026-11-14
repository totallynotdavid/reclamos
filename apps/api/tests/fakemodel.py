"""A stand-in for the hosted language model.

It records every prompt it is sent and replies with what `reply` returns, billed
at fixed token counts. The answer replies read the numbered passages out of the
prompt, so a test can quote a passage the way a model would.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from reclamos_api.answers.llm import Completion, Prompt


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


def passage(prompt: Prompt, number: int) -> str:
    """The text of passage `number` as the answer prompt numbers it."""
    after = prompt.text.split(f"[{number}] Resolución ", 1)[1]
    body = after.split("\n", 1)[1]
    return body.split(f"\n\n[{number + 1}] Resolución ", 1)[0].split(
        "\n\nPregunta: ", 1
    )[0]


def as_rewritten(text: str) -> str:
    """`text` as a model may copy it: upper case, one space between words and
    curly quotes around it."""
    return chr(0x201C) + " ".join(text.split()).upper() + chr(0x201D)


def answer_reply(citations: list[dict[str, Any]], *, supported: bool = True) -> str:
    return json.dumps(
        {
            "supported": supported,
            "answer": "El tribunal declaró infundado el reclamo [1]."
            if supported
            else "",
            "citations": citations,
        }
    )


def quoting_first_passage(prompt: Prompt) -> str:
    """Cites passage 1 faithfully, plus one quote it invented and one passage
    that does not exist."""
    if "faithful" in prompt.schema["properties"]:
        return json.dumps({"faithful": True, "unsupported": []})
    return answer_reply(
        [
            {"passage": 1, "quote": as_rewritten(passage(prompt, 1)[:160])},
            {"passage": 1, "quote": "el tribunal ordenó devolver diez mil soles"},
            {"passage": 99, "quote": passage(prompt, 1)[:160]},
        ]
    )
