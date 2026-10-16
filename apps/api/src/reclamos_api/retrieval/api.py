"""The `POST /search` route."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from reclamos_api.corpus.parse import CLAIM_TYPE_NAMES, OUTCOME_NAMES
from reclamos_api.retrieval.search import Filters, IndexModelMismatch, Mode, search

MAX_QUERY_CHARS = 500
MAX_K = 50
DEFAULT_K = 10


def _one_of(allowed: tuple[str, ...]):
    def check(value: str) -> str:
        if value not in allowed:
            raise ValueError(f"must be one of: {', '.join(allowed)}")
        return value

    return AfterValidator(check)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    mode: Mode = "hybrid"
    claim_type: Annotated[str, _one_of(CLAIM_TYPE_NAMES)] | None = None
    operator: str | None = Field(default=None, min_length=1, max_length=120)
    outcome: Annotated[str, _one_of(OUTCOME_NAMES)] | None = None
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def dates_in_order(self) -> "SearchRequest":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


class SearchHit(BaseModel):
    resolution_id: int
    expediente: str
    number: str | None
    date: date | None
    operator: str | None
    claim_type: str | None
    outcome: str | None
    url: str
    ordinal: int
    section: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    text: str
    score: float
    vector_score: float | None
    text_score: float | None


class SearchResponse(BaseModel):
    query: str
    mode: Mode
    results: list[SearchHit]


router = APIRouter()


@router.post("/search")
async def search_chunks(request: Request, body: SearchRequest) -> SearchResponse:
    try:
        hits = await search(
            request.app.state.pool,
            request.app.state.embedder,
            query=body.query,
            k=body.k,
            mode=body.mode,
            filters=Filters(
                claim_type=body.claim_type,
                operator=body.operator,
                outcome=body.outcome,
                date_from=body.date_from,
                date_to=body.date_to,
            ),
        )
    except IndexModelMismatch as mismatch:
        raise HTTPException(status_code=503, detail=str(mismatch)) from None
    return SearchResponse(
        query=body.query,
        mode=body.mode,
        results=[_response_hit(hit.fields, hit) for hit in hits],
    )


def _response_hit(fields: dict[str, Any], hit: Any) -> SearchHit:
    return SearchHit(
        resolution_id=hit.resolution_id,
        expediente=fields["expediente"],
        number=fields["number"],
        date=fields["resolved_on"],
        operator=fields["operator"],
        claim_type=fields["claim_type"],
        outcome=fields["outcome"],
        url=fields["page_url"],
        ordinal=hit.ordinal,
        section=fields["section"],
        page_start=fields["page_start"],
        page_end=fields["page_end"],
        char_start=fields["char_start"],
        char_end=fields["char_end"],
        text=fields["text"],
        score=hit.score,
        vector_score=hit.vector_score,
        text_score=hit.text_score,
    )
