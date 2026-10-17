"""Split a resolution into passages."""

import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass

MAX_CHARS = 1200
OVERLAP_CHARS = 200

PREAMBLE = "encabezado"

# A heading is written in capitals, alone or followed by a colon and the first
# words of its text. Scans prefix the numeral with stray marks ("IL ANALISIS"),
# put a stray mark or a letter or two before the words ("E HA RESUELTO"), and
# misread a capital L as a lower-case one ("RESUElTO").
_HEADING = re.compile(
    r"""^(?:[^\w\s]{1,3}\s*|[A-Za-z]{1,2}\s+){0,2}
        (?:[IVXLil1|]{1,5}\s*[.)]\s*|[IVXLil1|]{1,5}\s+)?
        (?P<name>VISTO|VISTOS|ANTECEDENTES|ANALISIS(?:\ DEL\ CASO)?|FUNDAMENTOS|
                 CUESTION\ PREVIA|CONSIDERANDO|CONSIDERACIONES|HA\ RESUELTO|
                 SE\ RESUELVE|RESUELVE|DECISION|CONCLUSION|CONCLUSIONES)
        \s*[:.]?(?:\s|$)""",
    re.VERBOSE,
)
_SECTIONS = {
    "VISTO": "visto",
    "VISTOS": "visto",
    "ANTECEDENTES": "antecedentes",
    "CUESTION": "cuestion_previa",
    "ANALISIS": "analisis",
    "FUNDAMENTOS": "fundamentos",
    "CONSIDERANDO": "fundamentos",
    "CONSIDERACIONES": "fundamentos",
    "HA": "resuelve",
    "SE": "resuelve",
    "RESUELVE": "resuelve",
    "DECISION": "resuelve",
    "CONCLUSION": "conclusion",
    "CONCLUSIONES": "conclusion",
}
_PARAGRAPH_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.)]\s+\S")
_SENTENCE_END = re.compile(r"(?<=[.;:?!])\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    section: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int


def chunk_text(
    body: str, *, max_chars: int = MAX_CHARS, overlap_chars: int = OVERLAP_CHARS
) -> list[Chunk]:
    """Split `body`, the text of a resolution with pages joined by form feeds."""
    page_starts = [0] + [m.end() for m in re.finditer("\f", body)]
    chunks: list[Chunk] = []
    for section, units in _sections(body):
        pieces = [
            part for unit in units for part in _fit(body, unit, max_chars=max_chars)
        ]
        for span in _pack(body, pieces, max_chars=max_chars, overlap=overlap_chars):
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    section=section,
                    page_start=bisect_right(page_starts, span.start),
                    page_end=bisect_right(page_starts, span.end - 1),
                    char_start=span.start,
                    char_end=span.end,
                    text=body[span.start : span.end],
                )
            )
    return chunks


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _sections(body: str) -> list[tuple[str, list[_Span]]]:
    """Paragraph spans grouped by the section heading above them."""
    sections: list[tuple[str, list[_Span]]] = [(PREAMBLE, [])]
    start: int | None = None
    end = 0

    def close() -> None:
        nonlocal start
        if start is not None:
            sections[-1][1].append(_Span(start, end))
            if sections[-1][0] == "visto":
                # VISTO is one paragraph. Before 2020 it leads straight into
                # numbered paragraphs, the "siguientes fundamentos" it announces.
                sections.append(("fundamentos", []))
        start = None

    for match in re.finditer(r"[^\n]*(?:\n|$)", body):
        line = match.group().rstrip("\n")
        stripped = line.strip(" \t\f\r")
        if not stripped:
            close()
            continue
        heading = _HEADING.match(_plain(stripped).replace("l", "L"))
        if heading:
            close()
            # The heading and the words after it are one paragraph.
            sections.append((_SECTIONS[heading["name"].split()[0]], []))
        elif _PARAGRAPH_NUMBER.match(line):
            close()
        if start is None:
            start = match.start() + (len(line) - len(line.lstrip(" \t\f\r")))
        end = match.start() + len(line.rstrip(" \t\f\r"))
    close()
    return [(name, units) for name, units in sections if units]


def _fit(body: str, unit: _Span, *, max_chars: int) -> list[_Span]:
    """The unit itself, or sentence groups when it is longer than a chunk."""
    if unit.end - unit.start <= max_chars:
        return [unit]
    sentences: list[_Span] = []
    cursor = unit.start
    for boundary in _SENTENCE_END.finditer(body, unit.start, unit.end):
        sentences.append(_Span(cursor, boundary.start()))
        cursor = boundary.end()
    sentences.append(_Span(cursor, unit.end))

    atoms: list[_Span] = []
    for sentence in sentences:
        atoms.extend(_cut_words(body, sentence, max_chars))
    groups: list[_Span] = []
    for atom in atoms:
        if groups and atom.end - groups[-1].start <= max_chars:
            groups[-1] = _Span(groups[-1].start, atom.end)
        else:
            groups.append(atom)
    return groups


def _cut_words(body: str, span: _Span, max_chars: int) -> list[_Span]:
    """Cut a span with no sentence end at the last space inside the cap."""
    parts: list[_Span] = []
    start = span.start
    while span.end - start > max_chars:
        space = body.rfind(" ", start + 1, start + max_chars)
        cut = space if space > start else start + max_chars
        parts.append(_Span(start, cut))
        start = cut
        while start < span.end and body[start].isspace():
            start += 1
    parts.append(_Span(start, span.end))
    return parts


def _pack(
    body: str, pieces: list[_Span], *, max_chars: int, overlap: int
) -> list[_Span]:
    """Join consecutive pieces up to the cap. Each chunk after the first opens
    at a sentence of the one before it, at most `overlap` characters from its
    end."""
    chunks: list[_Span] = []
    pieces = list(pieces)
    while pieces:
        last = 0
        while (
            last + 1 < len(pieces)
            and pieces[last + 1].end - pieces[0].start <= max_chars
        ):
            last += 1
        chunk = _Span(pieces[0].start, pieces[last].end)
        chunks.append(chunk)
        if last + 1 >= len(pieces):
            break
        # The next chunk must still hold the whole piece that did not fit.
        earliest = max(chunk.end - overlap, pieces[last + 1].end - max_chars)
        start = _sentence_start(body, chunk, earliest)
        pieces = pieces[last + 1 :]
        if start is not None:
            pieces.insert(0, _Span(start, chunk.end))
    return chunks


def _sentence_start(body: str, chunk: _Span, earliest: int) -> int | None:
    """The first sentence start inside the chunk at or after `earliest`, but not
    at the chunk's own start."""
    for boundary in _SENTENCE_END.finditer(
        body, max(earliest - 1, chunk.start), chunk.end
    ):
        if boundary.end() >= earliest and boundary.end() < chunk.end:
            return boundary.end()
    return None
