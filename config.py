"""Central configuration. Pinned model versions live here and nowhere else."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "deals.db"
DOCUMENTS_DIR = DATA_DIR / "documents"
DEFECTS_PATH = DATA_DIR / "defects.json"
POLICY_DIR = ROOT / "policy"
TEMPLATES_DIR = ROOT / "templates"
LOGS_DIR = ROOT / "logs"
MODEL_CALL_LOG = LOGS_DIR / "model_calls.jsonl"
CHECKPOINT_DB = ROOT / "checkpoints.sqlite"

# --- Models -----------------------------------------------------------------
# Pinned version strings. Never inline a model name anywhere else.

# Verified present on the Nebius Token Factory endpoint. Check with
# `python -m scripts.preflight` before changing it - a model ID that does not
# exist there fails as a 404 inside classification, which surfaces as every
# document coming back `unknown` and every run escalating, rather than as an
# obvious error.
EXTRACTION_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"   # Nebius Token Factory

# --- Drafting and review provider -------------------------------------------
#
# The build spec pins drafting and review to Anthropic Claude. That is still the
# intended path and switching back is one setting each. They run on Nebius by
# default here only because the Anthropic account has no credit, and a system
# that cannot draft is worse than one that drafts on a second-choice model.
#
# Whichever provider is chosen, the constraints are unchanged: structured output
# against the same schemas, every call logged, and no figure permitted into the
# narrative that is not in the evidence ledger.

DRAFTING_PROVIDER = "nebius"        # "nebius" | "anthropic"
REVIEW_PROVIDER = "nebius"          # "nebius" | "anthropic"

ANTHROPIC_DRAFTING_MODEL = "claude-opus-5"
ANTHROPIC_REVIEW_MODEL = "claude-opus-5"

NEBIUS_DRAFTING_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"
NEBIUS_REVIEW_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"

DRAFTING_MODEL = (
    NEBIUS_DRAFTING_MODEL if DRAFTING_PROVIDER == "nebius" else ANTHROPIC_DRAFTING_MODEL
)
REVIEW_MODEL = (
    NEBIUS_REVIEW_MODEL if REVIEW_PROVIDER == "nebius" else ANTHROPIC_REVIEW_MODEL
)

# Temperature zero, where the provider still accepts it.
#
# The current Claude models reject sampling parameters outright - passing
# `temperature` to Opus 5 returns a 400. This setting therefore applies to the
# Nebius extraction model only, and `src/tools/models.py` does not send it to
# Anthropic.
#
# Nothing about determinism rests on it. Every figure in the memo comes from the
# ledger, which is built by database reads and pure functions; the model's only
# outputs are classification, extraction against a schema, source selection and
# narrative prose. See tests/test_determinism.py.
TEMPERATURE = 0
ANTHROPIC_ACCEPTS_TEMPERATURE = False

NEBIUS_API_KEY = os.getenv("NEBIUS_API_KEY", "")
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.com/v1")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Policy -----------------------------------------------------------------

POLICY_VERSION = "v1"
POLICY_PATH = POLICY_DIR / f"credit_policy_{POLICY_VERSION}.yaml"

# --- Evidence loop ----------------------------------------------------------

MAX_ATTEMPTS_PER_FIELD = 3      # per field, never global
MAX_REVISIONS = 2               # review -> drafting round trips

WATERMARK_TEXT = "SAMPLE — SYNTHETIC DATA"
