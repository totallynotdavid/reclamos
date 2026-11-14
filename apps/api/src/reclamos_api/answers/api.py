"""The `POST /answer` route."""

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from reclamos_api.answers.answer import Answer, Outcome, Reason, answer_question
from reclamos_api.answers.spend import SpendRefused
from reclamos_api.retrieval.api import MAX_QUERY_CHARS
from reclamos_api.retrieval.search import IndexModelMismatch


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class CitedPassage(BaseModel):
    passage: int
    resolution_id: int
    expediente: str
    number: str | None
    date: date | None
    outcome: str | None
    url: str
    ordinal: int
    page_start: int
    page_end: int
    quote: str


class AnswerResponse(BaseModel):
    question: str
    outcome: Outcome
    answer: str
    reason: Reason | None
    citations: list[CitedPassage]


router = APIRouter()


@router.post("/answer")
async def answer(request: Request, body: AnswerRequest) -> AnswerResponse:
    state = request.app.state
    if state.model is None:
        raise HTTPException(status_code=503, detail="answers are not configured")
    try:
        result = await answer_question(
            state.pool,
            state.embedder,
            state.ledger,
            state.model,
            state.answer_settings,
            body.question,
        )
    except IndexModelMismatch as mismatch:
        raise HTTPException(status_code=503, detail=str(mismatch)) from None
    except SpendRefused:
        # The refusal's message names amounts spent, which a visitor need not see.
        raise HTTPException(
            status_code=429, detail="the daily spend cap is reached"
        ) from None
    return _response(result)


def _response(result: Answer) -> AnswerResponse:
    citations: list[CitedPassage] = []
    for citation in result.citations:
        fields = result.passages[citation.passage - 1].fields
        citations.append(
            CitedPassage(
                passage=citation.passage,
                resolution_id=citation.resolution_id,
                expediente=fields["expediente"],
                number=fields["number"],
                date=fields["resolved_on"],
                outcome=fields["outcome"],
                url=fields["page_url"],
                ordinal=citation.ordinal,
                page_start=fields["page_start"],
                page_end=fields["page_end"],
                quote=citation.quote,
            )
        )
    return AnswerResponse(
        question=result.question,
        outcome=result.outcome,
        answer=result.text,
        reason=result.reason,
        citations=citations,
    )
