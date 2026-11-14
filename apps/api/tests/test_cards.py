import json
from typing import Any

import pytest
from fakeembed import HashingEmbedder
from fakemodel import FakeModel, as_rewritten
from reclamos_api import cli
from reclamos_api.cards import judge
from reclamos_api.config import Settings
from reclamos_api.llm.client import Prompt
from test_search import BILLING, CONTRACT, ENTEL, QUEJA, query

INVENTED = "el tribunal ordenó devolver diez mil soles al usuario"
# Lines of the BILLING fixture, which parse reads as INFUNDADO, AMERICA MOVIL
# PERU S.A.C. and 2022-08-24.
OUTCOME_LINE = "Declarar INFUNDADO el Recurso de Apelación"
OPERATOR_LINE = "EMPRESA OPERADORA : AMERICA MOVIL PERU S.A.C."
DATE_LINE = "Lima, 24 de agosto de 2022"


def body_of(prompt: Prompt) -> str:
    return prompt.text.split("Texto de la resolución:\n\n", 1)[1]


def field(text: str, *quotes: str) -> dict[str, Any]:
    return {"text": text, "quotes": list(quotes)}


def reading(value: str, quote: str) -> dict[str, str]:
    return {"value": value, "quote": quote}


def card_or_verdict(prompt: Prompt) -> str:
    """A card that quotes the body it was sent, with one invented quote, one
    field backed only by that quote, and a misread outcome. A verdict that
    finds one unsupported claim."""
    if prompt.system == judge.SYSTEM:
        return json.dumps({"unsupported": [{"field": "claim", "claim": "pidió"}]})
    body = body_of(prompt)
    return json.dumps(
        {
            "claim": field(
                "El usuario reclamó.", as_rewritten(body[400:520]), INVENTED
            ),
            "operator_position": field("La empresa devolvió todo.", INVENTED),
            "reasoning": field("", ""),
            "evidence": field("Faltó la prueba.", body[1000:1100]),
            "outcome": reading("fundado", OUTCOME_LINE),
            "operator": reading("América Móvil Perú S.A.C.", OPERATOR_LINE),
            "resolved_on": reading("2022-08-24", DATE_LINE),
        }
    )


def cards(*argv: str, model: FakeModel, capsys: pytest.CaptureFixture[str]) -> str:
    cli.main(["cards", *argv], model=model)
    return capsys.readouterr().out


@pytest.mark.integration
def test_cards_are_built_within_a_budget_checked_judged_and_reported(
    indexed: Settings,
    embedder: HashingEmbedder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Each card reserves about US$0.04 and, at 3,000 output tokens, is billed
    # about US$0.03. The second card cannot fit in US$0.06.
    model = FakeModel(card_or_verdict, output_tokens=3000)
    out = cards(
        "build",
        "--budget-usd",
        "0.06",
        "--concurrency",
        "1",
        model=model,
        capsys=capsys,
    )
    assert "cards written: 1 of 4 without a current card" in out
    assert "stopped: the run has spent" in out
    assert len(model.prompts) == 1

    model.output_tokens = 20
    out = cards("build", "--budget-usd", "1", model=model, capsys=capsys)
    assert "cards written: 3 of 3 without a current card" in out
    assert "stopped" not in out
    out = cards("build", "--budget-usd", "1", model=model, capsys=capsys)
    assert "cards written: 0 of 0" in out
    assert len(model.prompts) == 4, "a current card is not written again"
    assert all(p.model == "claude-sonnet-5-5" for p in model.prompts)

    rows = query(indexed, "SELECT * FROM cards ORDER BY resolution_id")
    assert [r["resolution_id"] for r in rows] == [BILLING, CONTRACT, QUEJA, ENTEL]
    billing = rows[0]
    fields = json.loads(billing["fields"])
    assert set(fields) == {"claim", "evidence"}, (
        "a field with only invented quotes, or no text, is dropped"
    )
    assert len(fields["claim"]["quotes"]) == 1
    rejected = json.loads(billing["rejected"])
    assert {"field": "claim", "quote": INVENTED} in rejected
    assert {
        "field": "operator_position",
        "text": "La empresa devolvió todo.",
    } in rejected
    assert set(json.loads(billing["readings"])) == {
        "outcome",
        "operator",
        "resolved_on",
    }
    entel = json.loads(rows[3]["readings"])
    assert "operator" not in entel and "resolved_on" not in entel, (
        "a reading quoting another resolution's lines is rejected"
    )

    out = cards("judge", "--budget-usd", "1", model=model, capsys=capsys)
    assert "cards judged: 4 of 4 without a verdict" in out
    judged = model.prompts[4]
    assert judged.system == judge.SYSTEM
    assert fields["claim"]["quotes"][0] in judged.text
    assert INVENTED not in judged.text, "the judge reads only the checked card"

    out = cards("report", model=model, capsys=capsys)
    assert "cards: 4 (4 current)" in out
    assert "fields: claim 4, operator_position 0, reasoning 0, evidence 4" in out
    assert "judged: 4, faithful 0 (0.0%)" in out
    assert "unsupported by field: claim 4" in out
    assert "disagreements with parse: 2" in out
    for resolution in (BILLING, CONTRACT):
        assert f"{resolution} outcome: parse infundado, card fundado" in out, (
            "the operator matches parse on its letters, the date on its value"
        )
