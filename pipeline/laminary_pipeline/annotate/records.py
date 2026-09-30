"""Turn one model response into a validated stored record, or a failure reason.

A response becomes a record only if all of these pass (docs/NARRATIVE_SCHEMA.md sections 11-12):
stop reason is ``end_turn``; the model that answered is the model requested; the text is JSON;
it validates against ``model_output_schema()``; ``to_record`` accepts it (the arc label is
derived there, from the points only); and ``annotation.validate_record`` (schema plus semantic
checks) returns no errors. Anything else is an ``Invalid`` with its reason. Nothing is dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from laminary_pipeline.annotate.client import Response, Usage
from laminary_pipeline.annotate.inputs import GatedInput
from laminary_pipeline.annotation import validate_record
from laminary_pipeline.model_output import model_output_schema, to_record

RAW_EXCERPT_CHARS = 4000
MAX_REASON_CHARS = 1000


@dataclass(frozen=True)
class RunContext:
    run_id: str
    model: str
    prompt_version: str
    effort: str | None
    batch: bool


@dataclass(frozen=True)
class Invalid:
    reason: str
    retryable: bool
    raw_excerpt: str | None = None


@cache
def _output_validator() -> Draft202012Validator:
    return Draft202012Validator(model_output_schema())


def now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str, n: int = MAX_REASON_CHARS) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def build_provenance(
    gated: GatedInput, ctx: RunContext, usage: Usage, attempts: int, request_id: str | None
) -> dict[str, Any]:
    notes = (
        f"attempts={attempts}; effort={ctx.effort}; "
        f"cache_creation_input_tokens={usage.cache_creation_input_tokens}; "
        f"request_id={request_id}"
    )
    return {
        "annotated_at": now_rfc3339(),
        "annotator": {
            "model_version": ctx.model,
            "prompt_version": ctx.prompt_version,
            "run_id": ctx.run_id,
        },
        "sources": gated.source_metas,
        "input_word_count": gated.input_word_count,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "batch": ctx.batch,
        },
        "notes": _clip(notes, 500),
    }


def interpret(
    response: Response,
    gated: GatedInput,
    ctx: RunContext,
    *,
    usage_so_far: Usage,
    attempts: int,
) -> dict[str, Any] | Invalid:
    """A validated llm_annotation record, or why the response can't be stored.

    ``usage_so_far`` is the summed usage of every attempt for this title including this one,
    so the stored usage is the title's full cost, retries included.
    """
    raw = response.text
    excerpt = raw[:RAW_EXCERPT_CHARS] if raw else None
    if response.stop_reason == "refusal":
        return Invalid(f"refusal: {response.stop_details}", retryable=False, raw_excerpt=excerpt)
    if response.model != ctx.model:
        return Invalid(f"answered by {response.model!r}, requested {ctx.model!r}", False, excerpt)
    if response.stop_reason != "end_turn":
        return Invalid(f"stop_reason {response.stop_reason!r}", True, excerpt)
    try:
        output = json.loads(raw)
    except json.JSONDecodeError as e:
        return Invalid(f"output is not JSON: {e}", True, excerpt)
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in _output_validator().iter_errors(output)
    ]
    if errors:
        return Invalid(_clip("output schema: " + "; ".join(errors[:10])), True, excerpt)
    provenance = build_provenance(gated, ctx, usage_so_far, attempts, response.request_id)
    try:
        record = to_record(
            output, record_kind="llm_annotation", title=gated.plot.title, provenance=provenance
        )
    except (ValueError, KeyError, TypeError) as e:
        return Invalid(_clip(f"to_record: {e}"), True, excerpt)
    problems = validate_record(record)
    if problems:
        return Invalid(_clip("record: " + "; ".join(problems[:10])), True, excerpt)
    return record


def failure_record(
    gated: GatedInput,
    ctx: RunContext,
    attempts: list[dict[str, Any]],
    final: Invalid,
) -> dict[str, Any]:
    """What goes to failures.jsonl when a title runs out of attempts or can't be retried."""
    return {
        "title_key": gated.plot.key,
        "title": gated.plot.title,
        "sources": gated.source_metas,
        "run_id": ctx.run_id,
        "model_version": ctx.model,
        "prompt_version": ctx.prompt_version,
        "failed_at": now_rfc3339(),
        "reason": final.reason,
        "retryable": final.retryable,
        "raw_output_excerpt": final.raw_excerpt,
        "attempts": attempts,
    }
