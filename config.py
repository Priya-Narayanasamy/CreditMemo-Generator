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

EXTRACTION_MODEL = "Qwen/Qwen2.5-72B-Instruct"          # Nebius Token Factory
DRAFTING_MODEL = "claude-sonnet-4-5-20250929"           # Anthropic
REVIEW_MODEL = "claude-sonnet-4-5-20250929"             # Anthropic

TEMPERATURE = 0

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
