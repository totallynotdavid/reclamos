"""Answer a question from the passages search returns, or refuse.

An answer is kept only when it cites passages, and each citation quotes the
passage it names word for word. Three things refuse: the best passage is not
similar enough to the question (no call is made), the model says the passages
do not answer, or none of its citations survive the check.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from reclamos_api.answers.llm import LanguageModel, Prompt
from reclamos_api.answers.spend import Budget, Ledger
from reclamos_api.retrieval.embedder import Embedder
from reclamos_api.retrieval.search import NO_FILTERS, Filters, Hit, search

# Passages sent with a question, and the characters they may hold together.
# A chunk is at most 1200 characters (corpus/chunk.py).
MAX_PASSAGES = 6
MAX_CONTEXT_CHARS = 9_000
MAX_ANSWER_TOKENS = 900

# A quote shorter than this proves little: "el reclamo" is in every passage.
MIN_QUOTE_CHARS = 20

Outcome = Literal["answered", "refused"]
Reason = Literal["no_passages", "low_similarity", "model_declined", "unverified"]

SYSTEM = """\
Eres un asistente que responde preguntas de usuarios de servicios de \
telecomunicaciones del Perú usando solo resoluciones del TRASU de OSIPTEL.

Reglas:
- Usa solo los pasajes numerados que se te dan. No uses otro conocimiento.
- Si los pasajes no responden la pregunta, responde con "supported": false y \
deja "answer" vacío y "citations" vacío.
- Si la responden, escribe una respuesta breve en español sencillo, de tres a \
seis oraciones, y marca cada afirmación con el número del pasaje entre \
corchetes, por ejemplo [2].
- Cada cita copia, carácter por carácter, una o dos oraciones del pasaje que \
la respalda. No corrijas errores de escritura ni cambies palabras.
- Explica lo que resolvió el tribunal en esos casos. No prometas el resultado \
del caso del usuario.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "passage": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["passage", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["supported", "answer", "citations"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class AnswerSettings:
    model: str
    # Cosine similarity of the best passage below which no call is made.
    min_similarity: float


@dataclass(frozen=True, slots=True)
class Citation:
    passage: int
    resolution_id: int
    ordinal: int
    quote: str


@dataclass(slots=True)
class Answer:
    question: str
    outcome: Outcome
    text: str = ""
    citations: list[Citation] = field(default_factory=list[Citation])
    reason: Reason | None = None
    passages: list[Hit] = field(default_factory=list[Hit])
    # Citations the check dropped, kept so the evaluation can count them.
    rejected: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    best_similarity: float | None = None
    usd: float = 0.0


async def answer_question(
    pool: Any,
    embedder: Embedder,
    ledger: Ledger,
    model: LanguageModel,
    settings: AnswerSettings,
    question: str,
    *,
    filters: Filters = NO_FILTERS,
    budget: Budget | None = None,
) -> Answer:
    hits = await search(pool, embedder, query=question, k=MAX_PASSAGES, filters=filters)
    passages = fit_context(hits)
    result = Answer(question, "refused", passages=passages)
    if not passages:
        result.reason = "no_passages"
        return result
    result.best_similarity = max(
        (h.vector_score for h in passages if h.vector_score is not None),
        default=None,
    )
    if (result.best_similarity or 0.0) < settings.min_similarity:
        result.reason = "low_similarity"
        return result
    metered = await ledger.complete(
        model,
        Prompt(
            model=settings.model,
            system=SYSTEM,
            text=user_prompt(question, passages),
            schema=SCHEMA,
            max_tokens=MAX_ANSWER_TOKENS,
        ),
        purpose="answer",
        budget=budget,
    )
    result.usd = metered.usd
    reply = metered.completion.parsed()
    if metered.completion.stop_reason == "max_tokens" or reply is None:
        result.reason = "unverified"
        return result
    if not reply.get("supported") or not str(reply.get("answer", "")).strip():
        result.reason = "model_declined"
        return result
    kept, rejected = check_citations(reply.get("citations") or [], passages)
    result.rejected = rejected
    if not kept:
        result.reason = "unverified"
        return result
    result.outcome = "answered"
    result.text = str(reply["answer"]).strip()
    result.citations = kept
    return result


def fit_context(hits: list[Hit]) -> list[Hit]:
    """The best hits whose text fits in `MAX_CONTEXT_CHARS` together."""
    kept: list[Hit] = []
    used = 0
    for hit in hits[:MAX_PASSAGES]:
        size = len(hit.fields["text"])
        if used + size > MAX_CONTEXT_CHARS:
            break
        kept.append(hit)
        used += size
    return kept


def user_prompt(question: str, passages: list[Hit]) -> str:
    blocks = [
        f"[{number}] Resolución {_label(hit)}\n{hit.fields['text']}"
        for number, hit in enumerate(passages, start=1)
    ]
    return "Pasajes:\n\n" + "\n\n".join(blocks) + f"\n\nPregunta: {question}"


def _label(hit: Hit) -> str:
    fields = hit.fields
    parts = [fields.get("number") or fields["expediente"]]
    if fields.get("operator"):
        parts.append(f"empresa {fields['operator']}")
    if fields.get("outcome"):
        parts.append(f"resultado {fields['outcome']}")
    return ", ".join(parts)


def check_citations(
    citations: list[Any], passages: list[Hit]
) -> tuple[list[Citation], list[dict[str, Any]]]:
    """Keep the citations whose quote is in the passage they name."""
    kept: list[Citation] = []
    rejected: list[dict[str, Any]] = []
    for citation in citations:
        entry = cast("dict[str, Any]", citation) if isinstance(citation, dict) else {}
        number: Any = entry.get("passage")
        quote = str(entry.get("quote", ""))
        valid = (
            isinstance(number, int)
            and 1 <= number <= len(passages)
            and len(normalize(quote)) >= MIN_QUOTE_CHARS
            and normalize(quote) in normalize(passages[number - 1].fields["text"])
        )
        if not valid:
            rejected.append({"passage": number, "quote": quote})
            continue
        assert isinstance(number, int)
        hit = passages[number - 1]
        kept.append(Citation(number, hit.resolution_id, hit.ordinal, quote.strip()))
    return kept, rejected


def normalize(text: str) -> str:
    """Compare text as a reader would: case, accents, runs of whitespace and
    the quote marks a model rewrites do not count."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = plain.translate(_NO_QUOTE_MARKS)
    return re.sub(r"\s+", " ", plain).strip()


# Straight, angle and curly quotes. Written as code points because ruff flags
# curly single quotes in source as look-alikes of the backtick.
_NO_QUOTE_MARKS: dict[int, None] = dict.fromkeys(
    (ord('"'), ord("'"), 0xAB, 0xBB, 0x201C, 0x201D, 0x2018, 0x2019)
)
