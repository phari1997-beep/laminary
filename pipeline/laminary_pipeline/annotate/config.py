"""Annotation run settings and the price table used for offline cost estimates.

PRICES ARE UNCONFIRMED. They come from the claude-api skill bundled with Claude Code
(version 2.1.285): the model table marked "cached: 2026-09-25", the cache economics in
shared/prompt-caching.md and the batch discount in shared/cost-optimization.md section 2.5,
read on 2026-09-30. Hari must confirm them against
https://platform.claude.com/docs/en/about-claude/pricing before any run is approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = PIPELINE_DIR / "prompts"
DATA_DIR = PIPELINE_DIR / "data"
PLOTS_DIR = DATA_DIR / "plots"
ANNOTATIONS_DIR = DATA_DIR / "annotations"

API_KEY_ENV = "ANTHROPIC_API_KEY"

DEFAULT_PROMPT_VERSION = "annotate-1.0.0"
# Phase 1 annotates with this model only (DECISIONS.md). Other models in MODELS stay for
# offline estimates; using one for a run needs an explicit override flag.
PHASE1_MODEL = "claude-opus-5-5"
API_BASE_URL = "https://api.anthropic.com"
# Structured JSON output is ~2K tokens; thinking comes on top and is billed as output.
DEFAULT_MAX_TOKENS = 16000
# Upper bound for --max-tokens. Non-streaming messages.create refuses much larger values (the
# SDK's expected-time check), and the worst-case budget bound scales with it.
MAX_TOKENS_CAP = 16000
# Opus 5.5 defaults to "medium" and Sonnet 5.5 to "high"; set it explicitly so runs are
# reproducible. To be swept on the gold set during prompt iteration.
DEFAULT_EFFORT = "medium"
# Total attempts per title (first try + retries) before a failure record is written.
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_CAP = 3
# Consecutive transient API errors (429, 5xx, timeouts, connection errors) after which a
# synchronous run stops cleanly instead of burning attempts on an unavailable API.
MAX_CONSECUTIVE_TRANSIENT_ERRORS = 5
# Seconds to wait after a transient API error, doubled each time (capped).
TRANSIENT_BACKOFF_SECONDS = 5.0
TRANSIENT_BACKOFF_CAP_SECONDS = 120.0
# PLAN 5.4 / agent rules: pilot on at most 500 titles before any larger run.
PILOT_TITLE_CAP = 500
# Hari approves any run whose WORST-CASE bound is over this (agent rules).
APPROVAL_THRESHOLD_USD = 50.0
BATCH_POLL_SECONDS = 60.0

PRICE_SOURCE = (
    "claude-api skill 2.1.285 (model table cached 2026-09-25; prompt-caching.md; "
    "cost-optimization.md 2.5), read 2026-09-30. UNCONFIRMED: Hari must check "
    "https://platform.claude.com/docs/en/about-claude/pricing"
)
BATCH_DISCOUNT = 0.5  # Batch API: 50% off every token, including cache reads and writes.
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL write, relative to the base input price.


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    min_cacheable_tokens: int
    supports_effort: bool
    # Thinking tokens are billed as output. Assumed range per request at DEFAULT_EFFORT;
    # a guess until the smoke call and the gold run measure it.
    thinking_tokens_assumed: tuple[int, int]
    note: str

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER


MODELS: dict[str, ModelSpec] = {
    spec.model_id: spec
    for spec in (
        ModelSpec(
            model_id="claude-opus-5-5",
            input_per_mtok=4.00,
            output_per_mtok=20.00,
            cache_read_per_mtok=0.20,
            min_cacheable_tokens=512,
            supports_effort=True,
            thinking_tokens_assumed=(500, 4000),
            note="Current Opus. Thinking always on; effort is the only control.",
        ),
        ModelSpec(
            model_id="claude-sonnet-5-5",
            input_per_mtok=2.00,
            output_per_mtok=10.00,
            cache_read_per_mtok=0.20,
            min_cacheable_tokens=512,
            supports_effort=True,
            thinking_tokens_assumed=(500, 4000),
            note="Current Sonnet. Adaptive thinking on by default.",
        ),
        ModelSpec(
            model_id="claude-haiku-4-5",
            input_per_mtok=1.00,
            output_per_mtok=5.00,
            # Not listed in the skill for Haiku 4.5; the general 0.1x-of-input rule assumed.
            cache_read_per_mtok=0.10,
            min_cacheable_tokens=4096,
            supports_effort=False,
            thinking_tokens_assumed=(0, 0),
            note="Cheapest. No effort parameter; runs without thinking here.",
        ),
    )
}
