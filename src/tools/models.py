"""Model bindings and the model call log.

Every model call in this system goes through `logged_call`, which appends the
prompt, the response and the token counts to `logs/model_calls.jsonl`. There is no
other path to a model, so the log is complete by construction.

Model version strings live in `config.py` and nowhere else. Temperature is zero
everywhere. No API key is ever printed or logged.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from config import (
    ANTHROPIC_API_KEY,
    DRAFTING_MODEL,
    DRAFTING_PROVIDER,
    EXTRACTION_MODEL,
    MODEL_CALL_LOG,
    NEBIUS_API_KEY,
    NEBIUS_BASE_URL,
    REVIEW_MODEL,
    REVIEW_PROVIDER,
    TEMPERATURE,
)

T = TypeVar("T", bound=BaseModel)

_log_lock = threading.Lock()


class ModelUnavailable(RuntimeError):
    """No credentials for the requested provider.

    Raised at construction, not mid-run, so a run either has its models or never
    starts.
    """


def _redact(value: str) -> str:
    """Belt and braces. Nothing should reach the log holding a key in the first place."""
    for secret in (NEBIUS_API_KEY, ANTHROPIC_API_KEY, os.getenv("OPENAI_API_KEY", "")):
        if secret and len(secret) > 8:
            value = value.replace(secret, "[redacted]")
    return value


def log_model_call(
    *,
    purpose: str,
    model: str,
    provider: str,
    prompt: Any,
    response: Any,
    input_tokens: int | None,
    output_tokens: int | None,
    duration_ms: int,
    error: str | None = None,
) -> None:
    """Append one call to the JSONL log. Never raises into the caller."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "provider": provider,
        "model": model,
        # Null for Anthropic, which rejects sampling parameters. Recording a
        # temperature that was never sent would misrepresent the call.
        "temperature": TEMPERATURE if provider == "nebius" else None,
        "prompt": _redact(prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)),
        "response": _redact(response if isinstance(response, str) else json.dumps(response, default=str)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "error": error,
    }

    try:
        MODEL_CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock, MODEL_CALL_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Losing a log line must not take down a run.
        pass


def _token_counts(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_metadata", None) or {}
    if isinstance(usage, dict) and usage:
        return usage.get("input_tokens"), usage.get("output_tokens")

    metadata = getattr(response, "response_metadata", None) or {}
    usage = metadata.get("usage") or metadata.get("token_usage") or {}
    return (
        usage.get("input_tokens") or usage.get("prompt_tokens"),
        usage.get("output_tokens") or usage.get("completion_tokens"),
    )


def logged_call(
    purpose: str,
    provider: str,
    model: str,
    prompt: Any,
    invoke: Callable[[], Any],
) -> Any:
    """Run one model call, logging it whether it succeeds or fails."""
    started = time.monotonic()
    try:
        response = invoke()
    except Exception as exc:  # noqa: BLE001 - logged, then re-raised for the caller to handle
        log_model_call(
            purpose=purpose,
            model=model,
            provider=provider,
            prompt=prompt,
            response=None,
            input_tokens=None,
            output_tokens=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    input_tokens, output_tokens = _token_counts(response)
    log_model_call(
        purpose=purpose,
        model=model,
        provider=provider,
        prompt=prompt,
        response=(
            response.model_dump() if isinstance(response, BaseModel)
            else getattr(response, "content", response)
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return response


# --- Bindings ---------------------------------------------------------------


def extraction_model():
    """Nebius Token Factory, used for classification and field extraction."""
    if not NEBIUS_API_KEY:
        raise ModelUnavailable(
            "NEBIUS_API_KEY is not set. Copy .env.example to .env and fill it in, "
            "or run with a local extractor."
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=EXTRACTION_MODEL,
        temperature=TEMPERATURE,
        api_key=NEBIUS_API_KEY,
        base_url=NEBIUS_BASE_URL,
        timeout=60,
        max_retries=2,
    )


def _anthropic(model: str):
    if not ANTHROPIC_API_KEY:
        raise ModelUnavailable(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    from langchain_anthropic import ChatAnthropic

    # No `temperature`. The current Claude models reject sampling parameters and
    # return a 400 if one is sent. See the note in config.py - determinism here
    # comes from the ledger, not from the sampler.
    return ChatAnthropic(
        model=model,
        api_key=ANTHROPIC_API_KEY,
        timeout=120,
        max_retries=2,
    )


def _nebius(model: str):
    """A Nebius-hosted model, used for drafting or review when so configured."""
    if not NEBIUS_API_KEY:
        raise ModelUnavailable(
            "NEBIUS_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        temperature=TEMPERATURE,
        api_key=NEBIUS_API_KEY,
        base_url=NEBIUS_BASE_URL,
        timeout=180,
        max_retries=2,
    )


def _for_provider(provider: str, model: str):
    if provider == "nebius":
        return _nebius(model)
    if provider == "anthropic":
        return _anthropic(model)
    raise ValueError(f"unknown model provider {provider!r}; use 'nebius' or 'anthropic'")


def drafting_model():
    return _for_provider(DRAFTING_PROVIDER, DRAFTING_MODEL)


def review_model():
    return _for_provider(REVIEW_PROVIDER, REVIEW_MODEL)


def drafting_credentials_present() -> bool:
    """Whether the configured drafting provider can actually be called."""
    return bool(NEBIUS_API_KEY if DRAFTING_PROVIDER == "nebius" else ANTHROPIC_API_KEY)


def review_credentials_present() -> bool:
    return bool(NEBIUS_API_KEY if REVIEW_PROVIDER == "nebius" else ANTHROPIC_API_KEY)


def model_identifiers() -> dict[str, str]:
    """Recorded in the memo footer, so a draft can be traced to what produced it."""
    return {
        "extraction": f"nebius/{EXTRACTION_MODEL}",
        "drafting": f"{DRAFTING_PROVIDER}/{DRAFTING_MODEL}",
        "review": f"{REVIEW_PROVIDER}/{REVIEW_MODEL}",
    }


def read_call_log(limit: int | None = None) -> list[dict]:
    """The model call log, newest last. Used by the UI and the project write-up."""
    if not MODEL_CALL_LOG.exists():
        return []

    records = []
    for line in MODEL_CALL_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return records[-limit:] if limit else records
