"""Is a plot file annotatable? The same checks a run applies before any paid call.

QA, 2026-10-02: 29 pilot titles passed the 150-word rule but could never be sent, because their
plot files came from fetchers older than 1.4.0 and annotate-1.2.0 on refuses those. "Passing"
for the pilot, backfill and the gold sheet therefore means *annotatable*: the plot file parses
as annotation input, passes the fail-closed gate and the request builds (and re-verifies) under
the default prompt and the Phase 1 model. No network call is made; the request is only built.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from laminary_pipeline.annotate.config import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    PHASE1_MODEL,
)
from laminary_pipeline.annotate.inputs import (
    GateError,
    InputFormatError,
    NotAnnotatable,
    parse_plot,
)
from laminary_pipeline.annotate.prompt import Prompt, PromptError, build_request, load_prompt


@lru_cache(maxsize=4)
def _prompt(version: str) -> Prompt:
    return load_prompt(version)


def not_annotatable_reason(
    rec: dict[str, Any], prompt_version: str = DEFAULT_PROMPT_VERSION
) -> str | None:
    """None when the plot file can be annotated under ``prompt_version`` (default prompt) and
    the Phase 1 model; else why not (a skipped file gives its skip reason)."""
    if rec.get("status") != "ok":
        return f"skipped: {rec.get('skip_reason')}"
    try:
        build_request(parse_plot(rec, str(rec.get("qid") or "plot file")), model=PHASE1_MODEL,
                      prompt=_prompt(prompt_version), effort=DEFAULT_EFFORT,
                      max_tokens=DEFAULT_MAX_TOKENS)
    except (GateError, InputFormatError, NotAnnotatable, PromptError) as e:
        return str(e)[:300]
    return None


def is_annotatable(rec: dict[str, Any]) -> bool:
    return not_annotatable_reason(rec) is None
