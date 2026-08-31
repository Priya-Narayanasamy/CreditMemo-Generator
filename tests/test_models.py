"""Guards on the model bindings.

These are the tests that would have caught two things the rest of the suite
could not, because every other test runs offline and the provider clients are
constructed lazily:

- `langchain-anthropic` was in requirements.txt but not installed, so the real
  drafting path would have died on an ImportError at first use
- a pinned model string had gone stale, and `temperature` was being sent to a
  model family that rejects sampling parameters with a 400

Nothing here calls an API. They construct clients and inspect what was configured.
"""

from __future__ import annotations

import importlib
import re

import pytest

import config
from src.tools.models import (
    ModelUnavailable,
    drafting_model,
    extraction_model,
    log_model_call,
    model_identifiers,
    review_model,
)

FACTORIES = {
    "extraction": extraction_model,
    "drafting": drafting_model,
    "review": review_model,
}


# --- The provider packages are actually installed ---------------------------


@pytest.mark.parametrize("name", sorted(FACTORIES))
def test_the_provider_package_is_importable(name, monkeypatch):
    """A missing provider package must not hide behind a lazy import.

    Without credentials the factory should raise ModelUnavailable - a clean,
    named failure. An ImportError here means the dependency is declared but not
    installed.
    """
    monkeypatch.setattr(config, "NEBIUS_API_KEY", "", raising=False)
    monkeypatch.setattr("src.tools.models.NEBIUS_API_KEY", "", raising=False)
    monkeypatch.setattr("src.tools.models.ANTHROPIC_API_KEY", "", raising=False)

    with pytest.raises(ModelUnavailable):
        FACTORIES[name]()


@pytest.mark.parametrize("module", ["langchain_anthropic", "langchain_openai"])
def test_the_provider_sdks_are_installed(module):
    assert importlib.import_module(module) is not None


def test_every_requirement_that_is_imported_is_installed():
    """requirements.txt is the contract for a fresh clone. A package listed there
    and imported by the source must import here."""
    imported = {
        "langgraph": "langgraph",
        "langgraph.checkpoint.sqlite": "langgraph-checkpoint-sqlite",
        "langchain_anthropic": "langchain-anthropic",
        "langchain_openai": "langchain-openai",
        "pdfplumber": "pdfplumber",
        "reportlab": "reportlab",
        "docx": "python-docx",
        "jinja2": "Jinja2",
        "yaml": "PyYAML",
        "streamlit": "streamlit",
        "dotenv": "python-dotenv",
    }
    declared = config.ROOT.joinpath("requirements.txt").read_text(encoding="utf-8").lower()

    for module, package in imported.items():
        assert importlib.util.find_spec(module), f"{package} is imported but not installed"
        assert package.lower() in declared, f"{package} is imported but not in requirements.txt"


# --- Model identifiers ------------------------------------------------------


def test_model_strings_are_pinned_in_config_and_nowhere_else():
    """A model name inlined at a call site is a version string that will rot
    unnoticed."""
    offenders = []
    for path in config.ROOT.joinpath("src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"claude-[a-z0-9.-]+"', line):
                offenders.append(f"{path.name}:{number}")

    assert offenders == [], f"model strings inlined outside config.py: {offenders}"


def test_no_anthropic_model_id_carries_a_date_suffix():
    """Current Claude model IDs are complete as written. A date-suffixed variant
    is a stale recollection and resolves to nothing."""
    for name in (config.ANTHROPIC_DRAFTING_MODEL, config.ANTHROPIC_REVIEW_MODEL):
        assert not re.search(r"-\d{8}$", name), f"{name} carries a date suffix"


def test_the_pinned_anthropic_models_are_current():
    """Whichever provider is selected, the Anthropic pins must stay valid so
    switching back is a one-line change that works."""
    known = {
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-haiku-4-5",
    }

    assert config.ANTHROPIC_DRAFTING_MODEL in known
    assert config.ANTHROPIC_REVIEW_MODEL in known


def test_the_selected_provider_picks_the_matching_model():
    for provider, selected, nebius_pin, anthropic_pin in (
        (config.DRAFTING_PROVIDER, config.DRAFTING_MODEL,
         config.NEBIUS_DRAFTING_MODEL, config.ANTHROPIC_DRAFTING_MODEL),
        (config.REVIEW_PROVIDER, config.REVIEW_MODEL,
         config.NEBIUS_REVIEW_MODEL, config.ANTHROPIC_REVIEW_MODEL),
    ):
        assert provider in {"nebius", "anthropic"}
        assert selected == (nebius_pin if provider == "nebius" else anthropic_pin)


def test_an_unknown_provider_is_rejected():
    from src.tools.models import _for_provider

    with pytest.raises(ValueError, match="unknown model provider"):
        _for_provider("openai", "gpt-whatever")


def test_the_footer_records_the_provider_as_well_as_the_model():
    """The memo footer must name what actually produced the draft, not what the
    build spec originally pinned."""
    identifiers = model_identifiers()

    assert identifiers["extraction"] == f"nebius/{config.EXTRACTION_MODEL}"
    assert identifiers["drafting"] == f"{config.DRAFTING_PROVIDER}/{config.DRAFTING_MODEL}"
    assert identifiers["review"] == f"{config.REVIEW_PROVIDER}/{config.REVIEW_MODEL}"


# --- Sampling parameters ----------------------------------------------------


def test_no_temperature_is_sent_to_anthropic(monkeypatch):
    """The current Claude models reject sampling parameters with a 400. Sending
    temperature=0 would fail every drafting and review call.

    Tested against the Anthropic binding directly, so it keeps guarding the
    Anthropic path even while the configured provider is Nebius.
    """
    from src.tools.models import _anthropic

    monkeypatch.setattr("src.tools.models.ANTHROPIC_API_KEY", "test-key-not-real")

    for model_id in (config.ANTHROPIC_DRAFTING_MODEL, config.ANTHROPIC_REVIEW_MODEL):
        client = _anthropic(model_id)
        payload = client._get_request_payload([{"role": "user", "content": "hi"}])

        assert "temperature" not in payload, "temperature must not reach Anthropic"
        assert "top_p" not in payload and "top_k" not in payload


def test_temperature_zero_is_still_sent_to_the_extraction_model(monkeypatch):
    """Nebius is OpenAI-compatible and does accept it."""
    monkeypatch.setattr("src.tools.models.NEBIUS_API_KEY", "test-key-not-real")

    assert extraction_model().temperature == 0


def test_the_call_log_does_not_claim_a_temperature_that_was_never_sent(tmp_path, monkeypatch):
    import json

    log = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr("src.tools.models.MODEL_CALL_LOG", log)

    log_model_call(purpose="draft_file_overview", model=config.DRAFTING_MODEL,
                   provider="anthropic", prompt="p", response="r",
                   input_tokens=1, output_tokens=1, duration_ms=1)
    log_model_call(purpose="extract_payslip", model=config.EXTRACTION_MODEL,
                   provider="nebius", prompt="p", response="r",
                   input_tokens=1, output_tokens=1, duration_ms=1)

    anthropic_record, nebius_record = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]

    assert anthropic_record["temperature"] is None
    assert nebius_record["temperature"] == 0
