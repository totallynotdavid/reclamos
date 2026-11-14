"""The `POST /cases` route."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from reclamos_api.cases.find import (
    DEFAULT_CASES,
    MAX_CASES,
    Case,
    find_cases,
    outcome_counts,
)
from reclamos_api.retrieval.api import QueryRequest
from reclamos_api.retrieval.search import IndexModelMismatch


class CasesRequest(QueryRequest):
    limit: int = Field(default=DEFAULT_CASES, ge=1, le=MAX_CASES)


class Passage(BaseModel):
    ordinal: int
    section: str
    page_start: int
    page_end: int
    text: str


class CardField(BaseModel):
    text: str
    quotes: list[str]


class FoundCase(BaseModel):
    resolution_id: int
    expediente: str
    number: str | None
    date: date | None
    operator: str | None
    claim_type: str | None
    outcome: str | None
    url: str
    similarity: float
    passage: Passage
    # Absent until the resolution has a judged card. Each field is one the
    # judge found backed by its quotes.
    card: dict[str, CardField] | None


class CasesResponse(BaseModel):
    query: str
    outcome: Literal["found", "no_similar_case"]
    outcomes: dict[str, int]
    cases: list[FoundCase]


router = APIRouter()


@router.post("/cases")
async def cases(request: Request, body: CasesRequest) -> CasesResponse:
    state = request.app.state
    try:
        found = await find_cases(
            state.pool,
            state.embedder,
            query=body.query,
            limit=body.limit,
            min_similarity=state.min_similarity,
            filters=body.filters(),
        )
    except IndexModelMismatch as mismatch:
        raise HTTPException(status_code=503, detail=str(mismatch)) from None
    return CasesResponse(
        query=body.query,
        outcome="found" if found else "no_similar_case",
        outcomes=outcome_counts(found),
        cases=[_found_case(case) for case in found],
    )


def _found_case(case: Case) -> FoundCase:
    fields = case.passage.fields
    return FoundCase(
        resolution_id=case.passage.resolution_id,
        expediente=fields["expediente"],
        number=fields["number"],
        date=fields["resolved_on"],
        operator=fields["operator"],
        claim_type=fields["claim_type"],
        outcome=fields["outcome"],
        url=fields["page_url"],
        similarity=round(case.similarity, 4),
        passage=Passage(
            ordinal=case.passage.ordinal,
            section=fields["section"],
            page_start=fields["page_start"],
            page_end=fields["page_end"],
            text=fields["text"],
        ),
        card=None
        if case.card is None
        else {
            name: CardField(text=value.text, quotes=list(value.quotes))
            for name, value in case.card.items()
        },
    )
