import asyncio

import pytest
from gobpe import FIXTURES
from reclamos_api.corpus.extract import ExtractionError, extract_text, missing_tools
from textpdf import text_pdf

needs_poppler = pytest.mark.skipif(
    bool(missing_tools(ocr=False)), reason=f"needs {missing_tools(ocr=False)}"
)
needs_ocr = pytest.mark.skipif(bool(missing_tools()), reason=f"needs {missing_tools()}")


@needs_poppler
def test_a_text_layer_is_read_without_ocr() -> None:
    pdf = text_pdf(
        ["RESOLUCION N. 0000001-2023-TRASU/OSIPTEL", "primera pagina"],
        ["segunda pagina con texto suficiente para contar como texto"],
    )

    extracted = asyncio.run(extract_text(pdf))

    assert extracted.method == "text-layer"
    assert (extracted.pages, extracted.ocr_pages) == (2, 0)
    assert "0000001-2023-TRASU/OSIPTEL" in extracted.text


@needs_ocr
def test_a_scan_is_read_by_ocr() -> None:
    extracted = asyncio.run(extract_text((FIXTURES / "6134941.pdf").read_bytes()))

    assert extracted.method == "ocr"
    assert extracted.pages == extracted.ocr_pages == 3
    assert "TRASU/ST-RQJ" in extracted.text
    assert "IMPROCEDENTE" in extracted.text


@needs_poppler
def test_a_file_that_is_not_a_pdf_is_unreadable() -> None:
    with pytest.raises(ExtractionError) as failure:
        asyncio.run(extract_text(b"<html>no soy un pdf</html>"))

    assert failure.value.reason == "unreadable_pdf"
