"""The check that a model's quote is in the text it names, word for word."""

import re
import unicodedata

# A quote shorter than this proves little: "el reclamo" is in every passage.
MIN_QUOTE_CHARS = 20


def is_quoted(quote: str, text: str) -> bool:
    """Whether `quote` is long enough and is in `text`."""
    plain = normalize(quote)
    return len(plain) >= MIN_QUOTE_CHARS and plain in normalize(text)


def normalize(text: str) -> str:
    """Compare text as a reader would: case, accents, runs of whitespace and
    the quote marks a model rewrites do not count."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    plain = plain.translate(_NO_QUOTE_MARKS)
    return re.sub(r"\s+", " ", plain).strip()


# Straight, angle and curly quotes. Written as code points because ruff flags
# curly single quotes in source as look-alikes of the backtick.
_NO_QUOTE_MARKS: dict[int, None] = dict.fromkeys(
    (ord('"'), ord("'"), 0xAB, 0xBB, 0x201C, 0x201D, 0x2018, 0x2019)
)
