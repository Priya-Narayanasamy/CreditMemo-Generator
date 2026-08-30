"""Document parsing, behind an interface.

Two reasons the interface exists:

- a parser can be swapped after benchmarking without touching any agent
- a parse failure surfaces as a handled value rather than an exception escaping a
  node. `parse` never raises for a bad document; it returns a `ParsedDoc` with
  `ok=False` and an error string the escalation can quote.

No agent may call pdfplumber, or any other parsing library, directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


def clean_cell(value: str | None) -> str:
    """Strip page furniture that has bled into a table cell.

    Collapses the line breaks pdfplumber introduces inside a wrapped cell. Content
    is never discarded here - page furniture is filtered before extraction, by
    size, in `PdfplumberParser`.
    """
    if not value:
        return ""
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    return " ".join(lines)


class ParsedPage(BaseModel):
    page_number: int          # 1-indexed, as it appears in a provenance record
    text: str = ""
    tables: list[list[list[str | None]]] = Field(default_factory=list)

    def clean_tables(self) -> list[list[list[str]]]:
        return [[[clean_cell(cell) for cell in row] for row in table] for table in self.tables]

    def table_pairs(self) -> dict[str, str]:
        """Two-column table rows as a mapping, which is how these documents are laid out."""
        pairs: dict[str, str] = {}
        for table in self.clean_tables():
            for row in table:
                if len(row) == 2 and row[0] and row[1]:
                    pairs.setdefault(row[0], row[1])
        return pairs


class ParsedDoc(BaseModel):
    path: str
    filename: str
    ok: bool
    pages: list[ParsedPage] = Field(default_factory=list)
    error: str | None = None
    parser: str = ""

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def pairs(self) -> dict[str, str]:
        """Key-value pairs across every page, first occurrence winning."""
        merged: dict[str, str] = {}
        for page in self.pages:
            for key, value in page.table_pairs().items():
                merged.setdefault(key, value)
        return merged

    def page_of(self, needle: str) -> int | None:
        """The page a string appears on, for provenance."""
        for page in self.pages:
            if needle in page.text:
                return page.page_number
        return None


@runtime_checkable
class DocumentParser(Protocol):
    """The single method every parser implementation provides."""

    name: str

    def parse(self, path: Path, pages: list[int] | None = None) -> ParsedDoc:
        ...


class PdfplumberParser:
    """The only implementation, deliberately. Others come after benchmarking."""

    name = "pdfplumber"

    # A watermark drawn across the page lands inside whatever table cells it
    # crosses, extracting as "T\nDaniel Okonkwo" or turning "4" into "E\n4".
    # It is set far larger than any body text, so filtering oversized glyphs
    # removes it without touching a single character of data. Body text in these
    # documents tops out at 16pt.
    WATERMARK_MIN_SIZE = 24.0

    def __init__(
        self,
        table_settings: dict | None = None,
        watermark_min_size: float | None = WATERMARK_MIN_SIZE,
    ) -> None:
        self.table_settings = table_settings
        self.watermark_min_size = watermark_min_size

    def _without_page_furniture(self, page):
        if self.watermark_min_size is None:
            return page
        return page.filter(
            lambda obj: obj.get("object_type") != "char"
            or obj.get("size", 0) < self.watermark_min_size
        )

    def parse(self, path: Path, pages: list[int] | None = None) -> ParsedDoc:
        path = Path(path)

        if not path.exists():
            return ParsedDoc(
                path=str(path),
                filename=path.name,
                ok=False,
                error=f"file not found: {path}",
                parser=self.name,
            )

        try:
            import pdfplumber

            parsed_pages: list[ParsedPage] = []
            with pdfplumber.open(path) as pdf:
                for index, raw_page in enumerate(pdf.pages, start=1):
                    if pages is not None and index not in pages:
                        continue
                    page = self._without_page_furniture(raw_page)
                    tables = (
                        page.extract_tables(self.table_settings)
                        if self.table_settings
                        else page.extract_tables()
                    )
                    parsed_pages.append(ParsedPage(
                        page_number=index,
                        # The unfiltered text keeps the watermark, so a downstream
                        # check that the document is marked synthetic still works.
                        text=raw_page.extract_text() or "",
                        tables=tables or [],
                    ))

            if not parsed_pages:
                return ParsedDoc(
                    path=str(path),
                    filename=path.name,
                    ok=False,
                    error="document contains no pages",
                    parser=self.name,
                )

            if not any(page.text.strip() for page in parsed_pages):
                # An image-only scan. Readable as a file, useless as evidence, and
                # worth distinguishing from a corrupt one in the escalation.
                return ParsedDoc(
                    path=str(path),
                    filename=path.name,
                    ok=False,
                    pages=parsed_pages,
                    error="no extractable text; the document may be an image-only scan",
                    parser=self.name,
                )

            return ParsedDoc(
                path=str(path),
                filename=path.name,
                ok=True,
                pages=parsed_pages,
                parser=self.name,
            )

        except Exception as exc:  # noqa: BLE001 - a parse failure is a value, not an escape
            return ParsedDoc(
                path=str(path),
                filename=path.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                parser=self.name,
            )


DEFAULT_PARSER: DocumentParser = PdfplumberParser()


# Alternative settings a retry may use on a document that failed to parse cleanly.
# A retry is only ever worth making with settings that differ from the last attempt.
RETRY_SETTINGS: list[dict | None] = [
    None,
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
]


def parser_for_attempt(attempt_index: int) -> DocumentParser:
    """A parser configured differently for each successive attempt at a document."""
    settings = RETRY_SETTINGS[min(attempt_index, len(RETRY_SETTINGS) - 1)]
    return PdfplumberParser(table_settings=settings)
