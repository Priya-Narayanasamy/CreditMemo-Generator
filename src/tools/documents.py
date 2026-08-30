"""Finding and classifying the documents belonging to an application.

Filenames carry no information - the agent identifies a document from its content.
Nothing in this module reads a filename for anything except addressing the file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from config import DOCUMENTS_DIR
from src.state import Provenance
from src.tools.extraction import Classification, DocumentType, Extractor
from src.tools.parsing import DEFAULT_PARSER, DocumentParser, ParsedDoc


class DocumentRef(BaseModel):
    filename: str
    path: str
    size_bytes: int

    def provenance(self, document_type: str, page: int | None = None) -> Provenance:
        return Provenance(
            source_kind="document",
            detail={"filename": self.filename, "page": page, "document_type": document_type},
        )


def list_documents(application_number: str, documents_dir: Path | None = None) -> list[DocumentRef]:
    """Every file in the application's document folder, in a stable order.

    An application with no folder returns an empty list rather than raising - that
    is a gap for the evidence agent to escalate, not a crash.
    """
    folder = Path(documents_dir or DOCUMENTS_DIR) / application_number
    if not folder.is_dir():
        return []

    return [
        DocumentRef(filename=path.name, path=str(path), size_bytes=path.stat().st_size)
        for path in sorted(folder.iterdir())
        if path.is_file()
    ]


def classify_document(
    doc: ParsedDoc,
    extractor: Extractor,
) -> Classification:
    """Classify a parsed document from its content.

    `unknown` is a valid and expected outcome. Nothing here forces a guess, and an
    unparseable document is `unknown` at zero confidence with the parse error as
    the reasoning.
    """
    return extractor.classify(doc)


class LoadedDocument(BaseModel):
    """A document, parsed and classified, ready for extraction."""

    ref: DocumentRef
    parsed: ParsedDoc
    classification: Classification

    @property
    def document_type(self) -> DocumentType:
        return self.classification.document_type

    @property
    def usable(self) -> bool:
        return self.parsed.ok and self.document_type != "unknown"


def load_and_classify(
    ref: DocumentRef,
    extractor: Extractor,
    parser: DocumentParser | None = None,
) -> LoadedDocument:
    """Parse then classify one document. Never raises for a bad document."""
    parsed = (parser or DEFAULT_PARSER).parse(Path(ref.path))
    return LoadedDocument(
        ref=ref,
        parsed=parsed,
        classification=classify_document(parsed, extractor),
    )
