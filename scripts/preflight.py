"""Check the live model path before spending a run on it.

Run this after changing a model pin, adding credentials, or moving to another
machine:

    python -m scripts.preflight

It answers the question the offline test suite cannot: are the configured models
real, reachable, and do they return what this system needs. A model ID that does
not exist on the endpoint fails as a 404 inside classification, which surfaces as
every document coming back `unknown` and every application escalating - a
symptom that looks nothing like its cause.

No secret is printed. Costs two small model calls when credentials are present.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    ANTHROPIC_API_KEY,
    DB_PATH,
    DOCUMENTS_DIR,
    DRAFTING_MODEL,
    DRAFTING_PROVIDER,
    EXTRACTION_MODEL,
    NEBIUS_API_KEY,
    NEBIUS_BASE_URL,
    REVIEW_MODEL,
    REVIEW_PROVIDER,
)

OK, BAD, SKIP = "  ok  ", " FAIL ", " skip "
failures: list[str] = []


def report(status: str, line: str) -> None:
    print(f"[{status}] {line}")
    if status == BAD:
        failures.append(line)


def check_data() -> None:
    report(OK if DB_PATH.exists() else BAD,
           f"database {DB_PATH.name}" + ("" if DB_PATH.exists() else " - run data/generate_db.py"))

    folders = len(list(DOCUMENTS_DIR.iterdir())) if DOCUMENTS_DIR.is_dir() else 0
    report(OK if folders else BAD,
           f"documents: {folders} application folders"
           + ("" if folders else " - run data/generate_docs.py"))


def check_credentials() -> tuple[bool, bool]:
    nebius, anthropic = bool(NEBIUS_API_KEY), bool(ANTHROPIC_API_KEY)
    report(OK if nebius else SKIP, f"NEBIUS_API_KEY {'set' if nebius else 'not set - offline mode'}")

    anthropic_needed = "anthropic" in (DRAFTING_PROVIDER, REVIEW_PROVIDER)
    if anthropic_needed:
        report(OK if anthropic else BAD,
               f"ANTHROPIC_API_KEY {'set' if anthropic else 'not set but required by the '
               'configured provider'}")
    else:
        report(SKIP, "ANTHROPIC_API_KEY not needed - both providers are nebius")

    drafting_ready = nebius if DRAFTING_PROVIDER == "nebius" else anthropic
    return nebius, drafting_ready


def check_extraction_model() -> None:
    """The check that matters most: is the pinned model actually on the endpoint."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=NEBIUS_API_KEY, base_url=NEBIUS_BASE_URL)
        available = sorted(model.id for model in client.models.list().data)
    except Exception as exc:  # noqa: BLE001
        report(BAD, f"could not list Nebius models: {type(exc).__name__}: {exc}")
        return

    if EXTRACTION_MODEL in available:
        report(OK, f"extraction model {EXTRACTION_MODEL} is available")
        return

    report(BAD, f"extraction model {EXTRACTION_MODEL} is NOT on the endpoint")
    suggestions = [m for m in available if "Instruct" in m or "instruct" in m][:6]
    print(f"        {len(available)} models available. Candidates:")
    for candidate in suggestions or available[:6]:
        print(f"          {candidate}")


def check_extraction_call() -> None:
    from src.tools.documents import list_documents
    from src.tools.extraction import NebiusExtractor
    from src.tools.parsing import PdfplumberParser

    refs = list_documents("APP-2026-0001")
    if not refs:
        report(SKIP, "no documents for APP-2026-0001 to test extraction against")
        return

    parsed = next(
        (p for p in (PdfplumberParser().parse(r.path) for r in refs) if p.ok), None
    )
    if parsed is None:
        report(BAD, "no readable document to test extraction against")
        return

    try:
        extractor = NebiusExtractor()
        classification = extractor.classify(parsed)
    except Exception as exc:  # noqa: BLE001
        report(BAD, f"extraction client failed to build: {type(exc).__name__}: {exc}")
        return

    if classification.document_type == "unknown":
        report(BAD, f"classification returned unknown: {classification.reasoning[:160]}")
    else:
        report(OK, f"classified a real document as {classification.document_type} "
                   f"at confidence {classification.confidence}")


def check_drafting_call() -> None:
    from src.tools.models import drafting_model

    try:
        response = drafting_model().invoke("Reply with the single word: ready")
        text = getattr(response, "content", str(response))
        report(OK, f"drafting model {DRAFTING_PROVIDER}/{DRAFTING_MODEL} responded "
                   f"({str(text)[:40].strip()})")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        report(BAD, f"drafting model {DRAFTING_PROVIDER}/{DRAFTING_MODEL} failed: "
                    f"{type(exc).__name__}: {message[:200]}")
        if "temperature" in message.lower():
            print("        The current Claude models reject sampling parameters. "
                  "See the note in config.py.")
        if "credit balance" in message.lower():
            print("        Switch DRAFTING_PROVIDER / REVIEW_PROVIDER in config.py "
                  "to 'nebius', or top up the Anthropic account.")


def main() -> int:
    print(f"Extraction  nebius/{EXTRACTION_MODEL}  via  {NEBIUS_BASE_URL}")
    print(f"Drafting    {DRAFTING_PROVIDER}/{DRAFTING_MODEL}")
    print(f"Review      {REVIEW_PROVIDER}/{REVIEW_MODEL}")
    print()

    check_data()
    nebius, drafting_ready = check_credentials()

    if nebius:
        check_extraction_model()
        check_extraction_call()
    if drafting_ready:
        check_drafting_call()

    print()
    if failures:
        print(f"{len(failures)} problem(s) found. The live path will not work until they are fixed.")
        return 1

    if not (nebius and drafting_ready):
        print("Offline mode is ready. Add the keys the configured providers need.")
    else:
        print("Ready. Both live model paths are reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
