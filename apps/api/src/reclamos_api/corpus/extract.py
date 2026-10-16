"""Text of a resolution PDF: the text layer where a page has one, OCR otherwise."""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# A page with fewer visible characters than this has no usable text layer. A
# scanned page that carries only a stamp or a page number lands below it.
MIN_TEXT_LAYER_CHARS = 40
OCR_DPI = 200
OCR_LANGUAGE = "spa"
# Page segmentation mode 6 reads a page as one block of text. Mode 3 (automatic)
# loses the operator name in the shaded header table of recent resolutions.
OCR_PAGE_SEGMENTATION = "6"
READING_TOOLS = ("pdfinfo", "pdftotext")
OCR_TOOLS = ("pdftoppm", "tesseract")


class ExtractionError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    method: str
    pages: int
    ocr_pages: int


def missing_tools(*, ocr: bool = True) -> list[str]:
    """Return programs the host lacks. `ocr` adds tools for scanned pages."""
    needed = READING_TOOLS + (OCR_TOOLS if ocr else ())
    missing = [tool for tool in needed if shutil.which(tool) is None]
    if ocr and "tesseract" not in missing:
        languages = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, check=False
        ).stdout.split()
        if OCR_LANGUAGE not in languages:
            missing.append(f"tesseract language {OCR_LANGUAGE}")
    return missing


async def _run(*command: str) -> str:
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ExtractionError(
            f"{command[0]}_failed", stderr.decode(errors="replace").strip()[-500:]
        )
    return stdout.decode(errors="replace")


def _require_tools(*, ocr: bool) -> None:
    absent = missing_tools(ocr=ocr)
    if absent:
        raise ExtractionError("tool_missing", ", ".join(absent))


def _visible(text: str) -> int:
    return len(re.sub(r"\s", "", text))


async def extract_text(pdf: bytes) -> Extracted:
    _require_tools(ocr=False)
    with tempfile.TemporaryDirectory(prefix="reclamos-extract-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(pdf)
        try:
            info = await _run("pdfinfo", str(source))
        except ExtractionError as error:
            raise ExtractionError("unreadable_pdf", error.detail) from None
        match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
        if match is None or int(match.group(1)) == 0:
            raise ExtractionError("unreadable_pdf", "no page count")
        pages = int(match.group(1))

        layer = (await _run("pdftotext", "-layout", str(source), "-")).split("\f")
        texts = [layer[index] if index < len(layer) else "" for index in range(pages)]
        scanned = [
            i for i, text in enumerate(texts) if _visible(text) < MIN_TEXT_LAYER_CHARS
        ]
        if scanned:
            _require_tools(ocr=True)
        for index in scanned:
            texts[index] = await _ocr_page(source, index + 1)

    if len(scanned) == pages:
        method = "ocr"
    elif scanned:
        method = "mixed"
    else:
        method = "text-layer"
    text = "\f".join(texts)
    if _visible(text) == 0:
        raise ExtractionError("no_text", f"{pages} pages, {len(scanned)} OCRed")
    return Extracted(text=text, method=method, pages=pages, ocr_pages=len(scanned))


async def _ocr_page(source: Path, page: int) -> str:
    stem = source.parent / f"page-{page}"
    await _run(
        "pdftoppm",
        "-r",
        str(OCR_DPI),
        "-gray",
        "-png",
        "-f",
        str(page),
        "-l",
        str(page),
        "-singlefile",
        str(source),
        str(stem),
    )
    return await _run(
        "tesseract",
        str(stem.with_suffix(".png")),
        "-",
        "-l",
        OCR_LANGUAGE,
        "--psm",
        OCR_PAGE_SEGMENTATION,
    )
