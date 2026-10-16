from datetime import date

import pytest
from reclamos_api.corpus.parse import parse_resolution

HEADER = """\
TRIBUNAL ADMINISTRATIVO DE SOLUCIÓN DE RECLAMOS DE USUARIOS
{heading}

EXPEDIENTE N?* {expediente}

RESOLUCIÓN FINAL

RESOLUCIÓN N.” {number}

Lima, {day} de {month} de {year}

RECLAMANTE :

EMPRESA OPERADORA : {operator}

SERVICIO : A

{subject_label} : {subject}

CÓDIGO DE RECLAMO : FC-1127071-2023

RESOLUCIÓN DEL TRIBUNAL : {outcome}

VISTO: El expediente de la referencia.
"""


def resolution(**overrides: str) -> str:
    fields = {
        "heading": "RECURSO DE APELACIÓN",
        "expediente": "0024297-2023/TRASU/ST-RA",
        "number": "0060397-2023-TRASU/OSIPTEL",
        "day": "29",
        "month": "setiembre",
        "year": "2023",
        "operator": "TELEFÓNICA DEL PERÚ S.A.A.",
        "subject_label": "MATERIA RECLAMADA",
        "subject": "Cobro por penalidad generado en la Nota de Débito",
        "outcome": "INFUNDADO",
    }
    return HEADER.format(**{**fields, **overrides})


def test_reads_every_field_of_an_appeal() -> None:
    parsed = parse_resolution(resolution())

    assert parsed.missing == []
    assert parsed.number == "0060397-2023-TRASU/OSIPTEL"
    assert parsed.resolved_on == date(2023, 9, 29)
    assert parsed.operator == "TELEFONICA DEL PERU S.A.A."
    assert parsed.appeal_kind == "apelacion"
    assert parsed.subject == "Cobro por penalidad generado en la Nota de Débito"
    assert parsed.claim_type == "billing"
    assert parsed.outcome == "infundado"


def test_reads_a_complaint_against_procedure() -> None:
    parsed = parse_resolution(
        resolution(
            heading="QUEJA",
            expediente="0026151-2023/TRASU/ST-RQJ",
            subject_label="ESCRITO DE QUEJA",
            subject="De fecha 04/10/2023, por la negativa a recibir el reclamo",
            outcome="INFUNDADA",
        )
    )

    assert parsed.missing == []
    assert parsed.appeal_kind == "queja"
    assert parsed.claim_type == "procedural"
    assert parsed.outcome == "infundado"


@pytest.mark.parametrize(
    ("subject", "claim_type"),
    [
        ("Facturación de cargo fijo y venta en cuotas", "billing"),
        ("Contratación no solicitada", "unrequested_contract"),
        ("Renovación no solicitada", "unrequested_contract"),
        ("Falta de servicio (baja injustificada)", "service_cut"),
        ("Calidad en la prestación del servicio de internet fijo", "service_quality"),
        ("Problemas con la portabilidad numérica", "porting"),
        ("Incumplimiento de ofertas y promociones", "contract_terms"),
    ],
)
def test_classifies_the_subject(subject: str, claim_type: str) -> None:
    assert parse_resolution(resolution(subject=subject)).claim_type == claim_type


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("FUNDADO", "fundado"),
        ("FUNDADA", "fundado"),
        ("INFUNDADO", "infundado"),
        ("IMPROCEDENTE", "improcedente"),
        ("FUNDADO EN PARTE", "fundado_en_parte"),
        ("INFUNDAD0", "infundado"),
        (
            (
                "IMPROCEDENTE: Queja por falta de respuesta al reclamo\n"
                "FUNDADA: la queja cuando la empresa no aplica el silencio"
            ),
            "mixto",
        ),
    ],
)
def test_normalizes_the_outcome(outcome: str, expected: str) -> None:
    assert parse_resolution(resolution(outcome=outcome)).outcome == expected


@pytest.mark.parametrize(
    "operator",
    [
        "ÓNICA DEL PERÚ S.A.A.",
        "TELEFONICA DEL PERU S.A.A. 33% E",
        "TELEFÓNICA DEL PERÚ SAA.",
    ],
)
def test_repairs_a_damaged_operator_name(operator: str) -> None:
    assert parse_resolution(resolution(operator=operator)).operator == (
        "TELEFONICA DEL PERU S.A.A."
    )


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("ENTELPERUSA. e E E", "ENTEL PERU S.A."),
        ("WOWTELSA.C. SS PS", "WOW TEL S.A.C."),
        ("AMÉRICA MÓVIL PERÚ S.A.C. 5% 55", "AMERICA MOVIL PERU S.A.C."),
    ],
)
def test_reads_a_known_operator_through_scan_noise(
    operator: str, expected: str
) -> None:
    assert parse_resolution(resolution(operator=operator)).operator == expected


def test_one_operator_misread_hyphenated_keeps_one_spelling() -> None:
    parsed = parse_resolution(resolution(operator="WI-NET TELECOM S.A.C."))

    assert parsed.operator == "WIN NET - TELECOM S.A.C."


def test_keeps_an_operator_it_does_not_know() -> None:
    parsed = parse_resolution(resolution(operator="Nueva Red Móvil S.A.C."))

    assert parsed.operator == "NUEVA RED MOVIL S.A.C."


def test_an_unreadable_operator_fails_instead_of_guessing() -> None:
    parsed = parse_resolution(resolution(operator="Sada\nPERU S.A."))

    assert parsed.operator is None
    assert parsed.missing == ["operator"]


def test_names_what_is_missing() -> None:
    parsed = parse_resolution(resolution(subject="Un asunto sin clasificar"))

    assert parsed.number
    assert parsed.claim_type is None
    assert parsed.missing == ["claim_type"]
    assert parsed.detail() == "missing: claim_type"


def test_a_text_without_a_header_misses_everything() -> None:
    parsed = parse_resolution("Contrato de arrendamiento entre las partes.")

    assert parsed.missing == [
        "date",
        "operator",
        "kind",
        "subject",
        "claim_type",
        "outcome",
    ]


def test_reads_the_labels_of_2026_resolutions() -> None:
    text = resolution(
        operator="INTEGRATEL PERU S.A.A. (ANTES TELEFÓNICA DEL\nPERÚ S.A.A.)",
        subject_label="CONCEPTO CUESTIONADO",
        subject="Incumplimiento en la renovación del equipo",
    ).replace("RESOLUCIÓN DEL TRIBUNAL", "SENTIDO DE LA RESOLUCIÓN")

    parsed = parse_resolution(text)

    assert parsed.missing == []
    assert parsed.operator == "INTEGRATEL PERU S.A.A."
    assert parsed.claim_type == "equipment"
    assert parsed.outcome == "infundado"


def test_the_operator_on_the_page_wins_over_the_scan() -> None:
    parsed = parse_resolution(
        resolution(operator="A."), summary="Empresa: ENTEL PERÚ S.A.\nSumilla: x"
    )

    assert parsed.operator == "ENTEL PERU S.A."


def test_the_page_summary_gives_the_outcome_when_the_scan_lost_its_label() -> None:
    text = resolution(
        heading="QUEJA",
        expediente="0007510-2024/TRASU/ST-RQJ",
        subject_label="SERVICIO",
        subject="MO EE",
        outcome="",
    ).replace("RESOLUCIÓN DEL TRIBUNAL", "RESOLUCIÓN DEL")

    parsed = parse_resolution(
        text, summary="Sumilla: Declaran\nFUNDADA\nla queja por silencio positivo"
    )

    assert parsed.missing == []
    assert parsed.outcome == "fundado"
    assert parsed.claim_type == "procedural"


def test_a_resolution_that_voids_a_step_has_no_claim_to_classify() -> None:
    parsed = parse_resolution(
        resolution(
            subject_label="OBSERVACIÓN",
            subject="",
            outcome="DEJAR SIN EFECTO LA ELEVACIÓN DEL EXPEDIENTE",
        )
    )

    assert parsed.outcome == "sin_efecto"
    assert parsed.claim_type == "procedural"
    assert parsed.missing == []


# Resolutions before 2020 print no full number, spell the date out and label the
# subject `CONCEPTOS RECLAMADOS`. Each text is an excerpt of a real scan.
SCAN_2006 = """\
S>OSIPTEL S
RESOLUCIÓN: 1 EXPEDIENTE N* 12323-2006/TRASU/GUS-RA |
o l RECURSO DE APELACION bla pa Enri i
Lima, catorce de noviembre del dos mil seis |
CONCEPTOS RECLAMADOS : Cargo fijo del Plan Súper Control 55 en el recibo de
julio de dos mil seis.
CICLO DE FACTURACIÓN : 20
( o EMPRESA OPERADORA : AMERICA MOVIL PERU S.A.C. : i
o “| NUMERO'DE RECLAMO : 0000042652 : > : : a es
RESOLUCIÓN DE EMPRESA : Carta GCC/BOCLG-R-11355-06
. OPERADORA
RESOLUCIÓN DEL TRIBUNAL : FUNDADO úl
VISTO : El expediente de la referencia.
"""

SCAN_2016_COLUMNS = """\
EXPEDIENTE No 16733-2016lTRASUlST-RQJ
QUEJA
Lima, 4 de Agosto del 2016

RECLAMANTE

EMPRESA OPERADORA
REFERENCIA
                                                         : VIETTEL PERU S.A.C.

                                                         : Carta No RQ-00398-16 de fecha 09 de junio de
ESCRITO DE QUEJA                                          De fecha 06 de junio de 2016, por no permitir la
                                                          presentación de una queja.

RESOLUCIÓN DEL TRIBUNAL                                  : INFUNDADA
VISTO: El expediente de la referencia.
"""


def test_reads_a_resolution_from_before_2020() -> None:
    parsed = parse_resolution(SCAN_2006)

    assert parsed.missing == []
    assert parsed.number is None
    assert parsed.resolved_on == date(2006, 11, 14)
    assert parsed.operator == "AMERICA MOVIL PERU S.A.C."
    assert parsed.appeal_kind == "apelacion"
    assert parsed.claim_type == "billing"
    assert parsed.outcome == "fundado"


def test_reads_a_complaint_whose_scan_put_the_values_after_the_labels() -> None:
    parsed = parse_resolution(SCAN_2016_COLUMNS)

    assert parsed.missing == []
    assert parsed.resolved_on == date(2016, 8, 4)
    assert parsed.operator == "VIETTEL PERU S.A.C."
    assert parsed.appeal_kind == "queja"
    assert parsed.claim_type == "procedural"
    assert parsed.outcome == "infundado"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Lima, uno de febrero de dos mil siete.", date(2007, 2, 1)),
        ("Lima, veintidós de noviembre del dos mil cinco.", date(2005, 11, 22)),
        ("Lima, treinta y uno de enero de dos mil nueve", date(2009, 1, 31)),
        ("Lima, 30 de diciembre del año dos mil tres.", date(2003, 12, 30)),
        ("Lima, 29 de diciembre det 2017", date(2017, 12, 29)),
        ("Lima, 26 de junio dal 2018 Ñ", date(2018, 6, 26)),
        ("Lima. 25 de marzo de 2014", date(2014, 3, 25)),
    ],
)
def test_reads_a_date_spelled_in_words_or_misread(line: str, expected: date) -> None:
    assert parse_resolution(f"RECURSO DE APELACION\n{line}\n").resolved_on == expected


def test_a_date_that_does_not_exist_is_not_read() -> None:
    assert parse_resolution("Lima, 31 de febrero de 2020\n").resolved_on is None


def test_the_page_summary_fills_what_the_scan_lost() -> None:
    scan = SCAN_2006.replace("RESOLUCIÓN DEL TRIBUNAL : FUNDADO úl", "").replace(
        "CONCEPTOS RECLAMADOS :", "XXXX :"
    )
    summary = (
        "Sumilla: Declaran\nfundada\nla Apelación por la facturación del cargo fijo."
    )

    parsed = parse_resolution(scan, summary=summary)

    assert parsed.outcome == "fundado"
    assert parsed.claim_type == "billing"


@pytest.mark.parametrize(
    ("summary", "outcome"),
    [
        ("Sumilla: Declaran INFUNDADO el recurso", "infundado"),
        ("Sumilla: Declaran\nparcialmente\nfundado\nel recurso", "fundado_en_parte"),
        ("Sumilla: Declaran improcedente Queja en la cual", "improcedente"),
        ("Se declara\nFUNDADA\nla queja por silencio", "fundado"),
    ],
)
def test_reads_the_ruling_the_page_summary_states(summary: str, outcome: str) -> None:
    assert parse_resolution("sin cabecera", summary=summary).outcome == outcome
