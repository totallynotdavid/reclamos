"""Judge whether an answer says only what its cited passages support.

The evaluation runs the judge; the API does not. A stronger model than the one
that wrote the answer reads the answer and the passages it cites.
"""

from dataclasses import dataclass
from typing import Any, cast

from reclamos_api.answers.answer import Answer
from reclamos_api.answers.llm import LanguageModel, Prompt
from reclamos_api.answers.spend import Budget, Ledger

MAX_JUDGE_TOKENS = 400

SYSTEM = """\
Revisas respuestas escritas a partir de resoluciones del TRASU de OSIPTEL.

Se te da una pregunta, los pasajes que la respuesta cita y la respuesta. Una \
respuesta es fiel cuando cada afirmación que hace está respaldada por los \
pasajes. Conocimiento que no está en los pasajes no la respalda, aunque sea \
cierto.

Responde con "faithful": true o false, y en "unsupported" copia las \
afirmaciones de la respuesta que los pasajes no respaldan. Si es fiel, deja \
"unsupported" vacío.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "faithful": {"type": "boolean"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["faithful", "unsupported"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Verdict:
    # None when the judge's reply could not be read.
    faithful: bool | None
    unsupported: tuple[str, ...]
    usd: float


async def judge_answer(
    ledger: Ledger,
    model: LanguageModel,
    judge_model: str,
    answer: Answer,
    *,
    budget: Budget | None = None,
) -> Verdict:
    """Judge an answered `answer` against the passages it cites."""
    cited = sorted({c.passage for c in answer.citations})
    passages = "\n\n".join(
        f"[{number}]\n{answer.passages[number - 1].fields['text']}" for number in cited
    )
    metered = await ledger.complete(
        model,
        Prompt(
            model=judge_model,
            system=SYSTEM,
            text=(
                f"Pregunta: {answer.question}\n\nPasajes citados:\n\n{passages}"
                f"\n\nRespuesta:\n{answer.text}"
            ),
            schema=SCHEMA,
            max_tokens=MAX_JUDGE_TOKENS,
        ),
        purpose="judge",
        budget=budget,
    )
    reply = metered.completion.parsed()
    if (
        metered.completion.stop_reason == "max_tokens"
        or reply is None
        or not isinstance(reply.get("faithful"), bool)
    ):
        return Verdict(None, (), metered.usd)
    claims: Any = reply.get("unsupported")
    unsupported = cast("list[Any]", claims) if isinstance(claims, list) else []
    return Verdict(
        reply["faithful"], tuple(str(claim) for claim in unsupported), metered.usd
    )
