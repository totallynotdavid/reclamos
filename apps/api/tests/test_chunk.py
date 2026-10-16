import itertools

import pytest
from gobpe import FIXTURES
from reclamos_api.corpus.chunk import chunk_text

RESOLUTIONS = [6134934, 6134935, 6134941]


def body(resolution_id: int) -> str:
    return (FIXTURES / f"{resolution_id}.txt").read_text()


@pytest.mark.parametrize("resolution_id", RESOLUTIONS)
def test_every_chunk_is_an_exact_slice_of_the_text_within_the_cap(
    resolution_id: int,
) -> None:
    text = body(resolution_id)

    chunks = chunk_text(text, max_chars=600, overlap_chars=100)

    assert len(chunks) > 3
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.text.strip()
        assert len(chunk.text) <= 600


@pytest.mark.parametrize("resolution_id", RESOLUTIONS)
def test_the_chunks_cover_all_of_the_text_and_neighbours_overlap(
    resolution_id: int,
) -> None:
    text = body(resolution_id)

    chunks = chunk_text(text, max_chars=600, overlap_chars=100)

    covered = [False] * len(text)
    for chunk in chunks:
        for position in range(chunk.char_start, chunk.char_end):
            covered[position] = True
    missed = [
        text[i] for i, seen in enumerate(covered) if not seen and not text[i].isspace()
    ]
    assert missed == []
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)
    # Within a section, the next chunk starts before the previous one ends.
    overlapping = [
        (a, b)
        for a, b in itertools.pairwise(chunks)
        if a.section == b.section and b.char_start < a.char_end
    ]
    assert overlapping


def test_chunks_follow_the_headings_of_the_resolution() -> None:
    chunks = chunk_text(body(6134934))

    sections = [c.section for c in chunks]
    assert sections[0] == "encabezado"
    assert (
        sections.index("visto")
        < sections.index("antecedentes")
        < sections.index("analisis")
    )
    assert sections[-1] == "resuelve"
    # A chunk never spans a heading: the section is the one it starts under.
    resuelve = next(c for c in chunks if c.section == "resuelve")
    assert (
        resuelve.text.lstrip()
        .upper()
        .startswith(("SE RESUELVE", "RESUELVE", "HA RESUELTO"))
    )
    antecedentes = next(c for c in chunks if c.section == "antecedentes")
    assert "ANTECEDENTES" not in antecedentes.text.split("\n", 1)[1]


def test_numbered_paragraphs_after_visto_without_a_heading_are_fundamentos() -> None:
    text = (
        "RESOLUCIÓN: 1\nEXPEDIENTE: 12323-2006/TRASU/GUS-RA\n\n"
        "VISTO: El recurso de apelación interpuesto por la empresa.\n\n"
        "Que, la empresa presentó su recurso dentro del plazo.\n\n"
        "1. El reclamo versa sobre facturación.\n\n"
        "2. La empresa no probó el consumo.\n\n"
        "SE RESUELVE: declarar fundado el recurso."
    )

    chunks = chunk_text(text, max_chars=80, overlap_chars=0)

    labelled = [(c.section, c.text.split("\n")[0][:12]) for c in chunks]
    assert labelled[0][0] == "encabezado"
    assert ("visto", "VISTO: El re") in labelled
    assert [s for s, _ in labelled if s == "fundamentos"]
    assert all(
        s == "fundamentos"
        for s, t in labelled
        if t.startswith(("Que, la", "1. El recl", "2. La empr"))
    )
    assert labelled[-1][0] == "resuelve"


def test_a_numbered_paragraph_starts_its_own_chunk_when_the_previous_one_is_full() -> (
    None
):
    text = "ANTECEDENTES:\n\n" + "\n\n".join(
        f"{n}. " + ("El reclamante presentó su reclamo. " * 8).strip()
        for n in range(1, 7)
    )

    chunks = chunk_text(text, max_chars=700, overlap_chars=0)

    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert chunk.text.lstrip()[0].isdigit()


def test_a_paragraph_longer_than_the_cap_is_split_at_sentences() -> None:
    sentence = "La empresa operadora no acreditó la contratación del servicio. "
    text = "ANÁLISIS DEL CASO:\n\n1. " + sentence * 40

    chunks = chunk_text(text, max_chars=500, overlap_chars=80)

    assert len(chunks) > 3
    for chunk in chunks:
        assert len(chunk.text) <= 500
        assert text[chunk.char_start : chunk.char_end] == chunk.text
    assert all(c.text.rstrip().endswith(".") for c in chunks)


def test_text_without_spaces_is_still_split_within_the_cap() -> None:
    text = "x" * 2500

    chunks = chunk_text(text, max_chars=1000, overlap_chars=100)

    assert all(len(c.text) <= 1000 for c in chunks)
    assert "".join(text[c.char_start : c.char_end] for c in chunks)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == 2500


def test_page_ranges_follow_the_form_feeds_between_pages() -> None:
    first = "ANTECEDENTES:\n\n1. " + "Primera página con texto del caso. " * 6
    second = "2. " + "Segunda página con más texto del caso. " * 6
    third = "RESUELVE:\n\nPRIMERO.- Declarar infundado el recurso."
    text = f"{first}\x0c{second}\x0c{third}"

    chunks = chunk_text(text, max_chars=400, overlap_chars=60)

    for chunk in chunks:
        assert chunk.page_start == 1 + text[: chunk.char_start].count("\f")
        assert chunk.page_end == 1 + text[: chunk.char_end - 1].count("\f")
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 3
    assert any(c.page_start != c.page_end for c in chunks)


def test_headings_survive_ocr_noise() -> None:
    text = (
        "TRIBUNAL\nL ANTECEDENTES:\n\n1. Hechos del caso.\n\n"
        "IL ANÁLISIS DEL CASO:\n\n2. Análisis.\n\n"
        "III. SE RESUELVE:\n\nPRIMERO.- Confirmar."
    )

    sections = [c.section for c in chunk_text(text, max_chars=60, overlap_chars=0)]

    assert "antecedentes" in sections
    assert "analisis" in sections
    assert sections[-1] == "resuelve"


@pytest.mark.parametrize(
    "heading",
    ["E HA RESUELTO: .", ". HA RESUELTO: | .", "HA RESUElTO:", "N HA RESUELTO:"],
)
def test_a_resolutive_heading_with_stray_marks_still_opens_its_section(
    heading: str,
) -> None:
    text = f"ANTECEDENTES\n\n1. Hechos.\n\n{heading}\n\nPRIMERO.- Confirmar."

    sections = [c.section for c in chunk_text(text, max_chars=60, overlap_chars=0)]

    assert sections[-1] == "resuelve"


def test_prose_that_mentions_a_heading_word_is_not_a_heading() -> None:
    text = "ANTECEDENTES\n\n1. El Tribunal ha resuelto el recurso.\n\nHa resuelto: no."

    assert {c.section for c in chunk_text(text, max_chars=60, overlap_chars=0)} == {
        "antecedentes"
    }


@pytest.mark.parametrize("text", ["", "   \n\n \f \n"])
def test_text_with_nothing_to_quote_has_no_chunks(text: str) -> None:
    assert chunk_text(text) == []
