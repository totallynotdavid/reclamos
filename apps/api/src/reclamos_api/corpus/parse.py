"""Read what a TRASU resolution states from its text.

Every resolution opens with the same block of labelled lines (expediente,
resolution number, date, operator, subject, outcome). The text comes from OCR
for scanned PDFs, so the patterns tolerate dropped accents, stray punctuation
and misread characters in the labels.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from difflib import get_close_matches

HEADER_CHARS = 3500

MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# These labels end the value of the preceding field.
_LABELS = (
    r"RECLAMANTE|SERVICIO|EMPRESA\s+OPERADORA|MATERIA\s+RECLAMADA|"
    r"CONCEPTO\s+(?:RECLAMADO|CUESTIONADO)|ESCRITO\s+DE\s+QUEJA|C.DIGO\s+DE\s+RECLAMO|"
    r"RESOLUCI.N\s+(?:IMPUGNADA|DE\s+(?:LA\s+)?EMPRESA|DEL\s+TRIBUNAL|DEL\s+TRASU)|"
    r"SENTIDO\s+DE\s+LA\s+RESOLUCI.N|N.MERO\s+DE\s+SERVICIO|SUMILLA|"
    r"REFERENCIA|RECLAMO\s+EN\s+.{1,4}\s+INSTANCIA|VISTO|ANTECEDENTES"
)

# Operators whose names scans often damage. A name that is close to one of
# these takes the spelling here; any other name is kept as printed.
KNOWN_OPERATORS = (
    "AMERICA MOVIL PERU S.A.C.",
    "ENTEL PERU S.A.",
    "INTEGRATEL PERU S.A.A.",
    "TELEFONICA DEL PERU S.A.A.",
    "VIETTEL PERU S.A.C.",
    "WIN NET - TELECOM S.A.C.",
    "WOW TEL S.A.C.",
)

_NUMBER = re.compile(
    r"(\d{6,8})\s*-\s*(\d{4})\s*-\s*TRASU\s*/\s*OSIPTEL", re.IGNORECASE
)
_EXPEDIENTE = re.compile(
    r"\d{6,8}\s*-\s*\d{4}\s*/\s*TRASU\s*/\s*ST\s*-\s*(RA|RQJ)", re.IGNORECASE
)
_DATE = re.compile(
    r"Lima,?\s+(\d{1,2})\s+de\s+([^\W\d_]+)\s+de(?:l)?\s+(\d{4})", re.IGNORECASE
)
_LEGAL_SUFFIX = re.compile(r"\b(?:S\.A\.A|S\.A\.C|S\.A|S\.R\.L|E\.I\.R\.L)\b\.?")

# The first group whose keywords appear in the subject wins, so the order puts
# the narrow, unambiguous topics before the broad ones.
CLAIM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unrequested_contract", ("no solicitad",)),
    ("porting", ("portabilidad", "cambio de operador")),
    (
        "service_cut",
        (
            "suspension",
            "baja",
            "corte",
            "falta de servicio",
            "reconexion",
            "cancelacion",
        ),
    ),
    (
        "service_quality",
        (
            "calidad",
            "velocidad",
            "averia",
            "interrupcion",
            "intermitencia",
            "instalacion",
            "traslado",
            "reparacion",
        ),
    ),
    (
        "billing",
        (
            "factur",
            "cobro",
            "recibo",
            "tarifa",
            "consumo",
            "cargo",
            "monto",
            "pago",
            "deuda",
            "penalidad",
            "saldo",
        ),
    ),
    ("equipment", ("equipo", "terminal", "garantia")),
    (
        "contract_terms",
        (
            "condiciones contractuales",
            "oferta",
            "promocion",
            "contrato",
            "plan tarifario",
        ),
    ),
)


@dataclass(slots=True)
class Parsed:
    number: str | None = None
    resolved_on: date | None = None
    operator: str | None = None
    subject: str | None = None
    claim_type: str | None = None
    appeal_kind: str | None = None
    outcome: str | None = None
    missing: list[str] = field(default_factory=list)

    def detail(self) -> str:
        return "missing: " + ", ".join(self.missing)


def parse_resolution(body: str, *, summary: str = "") -> Parsed:
    header = body[:HEADER_CHARS]
    parsed = Parsed(
        number=_number(header),
        resolved_on=_date(header),
        operator=_operator(header, summary),
        appeal_kind=_appeal_kind(header),
    )
    parsed.subject = _subject(header)
    parsed.outcome = _outcome(header) or _ruling_in(summary)
    if parsed.outcome == "sin_efecto":
        # The Tribunal sent the file back to the operator without ruling on a claim.
        parsed.claim_type = "procedural"
    elif parsed.subject or parsed.appeal_kind == "queja":
        parsed.claim_type = claim_type(
            parsed.subject or "", appeal_kind=parsed.appeal_kind
        )

    checks = (
        ("number", parsed.number),
        ("date", parsed.resolved_on),
        ("operator", parsed.operator),
        ("kind", parsed.appeal_kind),
        ("subject", parsed.subject or parsed.claim_type),
        ("claim_type", parsed.claim_type),
        ("outcome", parsed.outcome),
    )
    parsed.missing = [name for name, value in checks if value is None]
    return parsed


def claim_type(subject: str, *, appeal_kind: str | None) -> str | None:
    if appeal_kind == "queja":
        return "procedural"
    text = _plain(subject).lower()
    for name, keywords in CLAIM_TYPES:
        if any(re.search(rf"\b{keyword}", text) for keyword in keywords):
            return name
    return None


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _number(header: str) -> str | None:
    match = _NUMBER.search(header)
    return f"{match[1]}-{match[2]}-TRASU/OSIPTEL" if match else None


def _date(header: str) -> date | None:
    match = _DATE.search(header)
    if not match:
        return None
    month_name = match[2].lower()
    month = MONTHS.get(month_name)
    if month is None:
        close = get_close_matches(_plain(month_name), MONTHS, n=1, cutoff=0.8)
        month = MONTHS[close[0]] if close else None
    if month is None:
        return None
    try:
        return date(int(match[3]), month, int(match[1]))
    except ValueError:
        return None


def _appeal_kind(header: str) -> str | None:
    match = _EXPEDIENTE.search(header)
    if match:
        return "apelacion" if match[1].upper() == "RA" else "queja"
    upper = _plain(header).upper()
    if "RECURSO DE APELACION" in upper:
        return "apelacion"
    if re.search(r"^\s*QUEJA\s*$", upper, re.MULTILINE):
        return "queja"
    return None


def _field(header: str, label: str) -> str | None:
    """The text after a label, up to the next label. Line breaks are kept."""
    pattern = rf"^[ \t]*(?:{label})[ \t]*[:;>_]?(.*?)(?=^[ \t]*(?:{_LABELS})\b|\Z)"
    match = re.search(pattern, header, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not match:
        return None
    return match[1].lstrip(":;>_ \t").strip() or None


def _operator(header: str, summary: str) -> str | None:
    """The operator's name. The page's own `Empresa:` line is typed text, so it
    wins over the scan when the page has one."""
    named = re.search(r"^Empresa:(.*)$", summary, re.IGNORECASE | re.MULTILINE)
    value = named[1] if named else _field(header, r"EMPRESA\s+OPERADORA")
    if not value or not value.strip():
        return None
    name = _squash(_plain(value.splitlines()[0]).upper())
    # Scan noise follows the name on the same line, with or without a space.
    letters = _letters(name)
    for known in KNOWN_OPERATORS:
        if letters.startswith(_letters(_LEGAL_SUFFIX.sub("", known))):
            return known
    suffix = _LEGAL_SUFFIX.search(name)
    if not suffix:
        # A name with neither a known operator nor a legal form is a misread
        # box, and a guess would put a wrong operator in the corpus.
        return None
    name = name[: suffix.end()].rstrip(".") + "."
    close = get_close_matches(name, KNOWN_OPERATORS, n=1, cutoff=0.85)
    return close[0] if close else name


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z]", "", _plain(text).upper())


def _subject(header: str) -> str | None:
    value = _field(
        header,
        r"MATERIA\s+RECLAMADA|CONCEPTO\s+(?:RECLAMADO|CUESTIONADO)|ESCRITO\s+DE\s+QUEJA",
    )
    return _squash(value) if value else None


OUTCOMES = ("infundado", "fundado", "improcedente")


def _outcome(header: str) -> str | None:
    """One outcome, or `mixto` when the Tribunal ruled differently on several claims."""
    value = _field(
        header,
        r"RESOLUCI.N\s+DEL\s+(?:TRIBUNAL|TRASU)|SENTIDO\s+DE\s+LA\s+RESOLUCI.N",
    )
    return _ruling_in(value) if value else None


def _ruling_in(value: str) -> str | None:
    if re.search(r"parcial|en\s+parte", _plain(value), re.IGNORECASE):
        return "fundado_en_parte"
    if re.search(r"sin\s+efecto", _plain(value), re.IGNORECASE):
        return "sin_efecto"
    # Each ruling is written in capitals. The prose after it is not.
    rulings = {
        ruling
        for word in re.findall(r"\b[A-ZÁÉÍÓÚÑ0-9]{6,}\b", value)
        if (ruling := _ruling(word))
    }
    if len(rulings) > 1:
        return "mixto"
    return next(iter(rulings), None)


def _ruling(word: str) -> str | None:
    """The outcome a capitalized word names, allowing for the feminine and scan errors."""
    text = _plain(word).lower()
    if text.endswith("a"):
        text = text[:-1] + "o"
    close = get_close_matches(text, OUTCOMES, n=1, cutoff=0.8)
    return close[0] if close else None
