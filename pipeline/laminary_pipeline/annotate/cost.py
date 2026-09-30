"""Offline cost estimates from token counts and the price table in config.

Token counts: exact counting needs the count_tokens API endpoint (a network call with a key),
so offline counts are ESTIMATES from characters (CHARS_PER_TOKEN). Pass exact counts in when
they are available (``annotate ... --count-tokens`` calls the endpoint). The structured-output
schema is compiled server-side; how it is billed is not documented in the skill, so its JSON
is counted as uncached input, a deliberate over-estimate.

Prices are the unconfirmed table in ``config.py``; every report says so.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.client import Usage
from laminary_pipeline.annotate.config import (
    BATCH_DISCOUNT,
    MODELS,
    PRICE_SOURCE,
)
from laminary_pipeline.model_output import from_record, model_output_schema

# Conservative for English prose on current Claude tokenizers (more tokens than the common
# 4-chars rule of thumb), so estimates lean high.
CHARS_PER_TOKEN = 3.5
ESTIMATE_METHOD = f"ESTIMATE: characters / {CHARS_PER_TOKEN} (not a tokenizer count)"
# Projections when no plot files exist yet: Wikipedia plot sections (words).
SUMMARY_WORDS_RANGE = (400, 1200)
CHARS_PER_WORD = 6.0  # including the following space
# Share of titles assumed to need one retry, added to the high estimate only.
RETRY_RATE_HIGH = 0.10

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "tests" / "examples"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@cache
def schema_tokens() -> int:
    return estimate_tokens(json.dumps(model_output_schema(), separators=(",", ":")))


@cache
def output_json_tokens() -> int:
    """Estimated tokens of one full annotation output, from the illustrative examples."""
    sizes = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("outcome") == "annotated":
            sizes.append(estimate_tokens(json.dumps(from_record(record))))
    return max(sizes) if sizes else 2500


def request_token_split(params: dict[str, Any]) -> tuple[int, int]:
    """(cacheable system tokens, per-title uncached tokens) for one built request."""
    system = sum(estimate_tokens(b["text"]) for b in params["system"])
    user = sum(estimate_tokens(b["text"]) for m in params["messages"] for b in m["content"])
    return system, user + schema_tokens()


@dataclass(frozen=True)
class CostEstimate:
    model: str
    titles: int
    batch: bool
    caching: bool
    usd_low: float
    usd_high: float
    input_tokens_per_title: tuple[int, int]
    output_tokens_per_title: tuple[int, int]
    token_method: str
    price_source: str = PRICE_SOURCE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def price_usage(model: str, usage: Usage, *, batch: bool) -> float:
    """Dollar cost of measured usage (cache writes at the 5-minute TTL rate)."""
    spec = MODELS[model]
    usd = (
        usage.input_tokens * spec.input_per_mtok
        + usage.cache_creation_input_tokens * spec.cache_write_per_mtok
        + usage.cache_read_input_tokens * spec.cache_read_per_mtok
        + usage.output_tokens * spec.output_per_mtok
    ) / 1e6
    return usd * (BATCH_DISCOUNT if batch else 1.0)


def _run_cost(
    model: str,
    titles: int,
    static_tokens: int,
    variable_tokens: int,
    output_tokens: int,
    *,
    batch: bool,
    caching: bool,
) -> float:
    """Cost of ``titles`` requests sharing a static prefix. With caching, the first request
    writes the prefix and the rest read it (the best case: cache hits inside a batch are
    best-effort, and a prefix under the model's minimum is never cached)."""
    spec = MODELS[model]
    cacheable = caching and static_tokens >= spec.min_cacheable_tokens
    if cacheable:
        writes, reads, plain = static_tokens, static_tokens * (titles - 1), 0
    else:
        writes, reads, plain = 0, 0, static_tokens * titles
    usage = Usage(
        input_tokens=plain + variable_tokens * titles,
        output_tokens=output_tokens * titles,
        cache_read_input_tokens=reads,
        cache_creation_input_tokens=writes,
    )
    return price_usage(model, usage, batch=batch)


def estimate(
    model: str,
    titles: int,
    *,
    static_tokens: int,
    variable_tokens: tuple[int, int],
    batch: bool,
    caching: bool,
    token_method: str = ESTIMATE_METHOD,
) -> CostEstimate:
    """Low/high cost for ``titles`` requests. ``variable_tokens`` is the per-title uncached
    input range. Output: the estimated JSON size plus the model's assumed thinking range;
    the high end also assumes RETRY_RATE_HIGH of titles are retried once."""
    think_lo, think_hi = MODELS[model].thinking_tokens_assumed
    out_lo = output_json_tokens() + think_lo
    out_hi = math.ceil(output_json_tokens() * 1.3) + think_hi
    low = _run_cost(
        model, titles, static_tokens, variable_tokens[0], out_lo, batch=batch, caching=caching
    )
    high = _run_cost(
        model, titles, static_tokens, variable_tokens[1], out_hi, batch=batch, caching=caching
    )
    if titles > 1:
        high *= 1 + RETRY_RATE_HIGH
    return CostEstimate(
        model=model,
        titles=titles,
        batch=batch,
        caching=caching,
        usd_low=round(low, 4),
        usd_high=round(high, 4),
        input_tokens_per_title=(
            static_tokens + variable_tokens[0],
            static_tokens + variable_tokens[1],
        ),
        output_tokens_per_title=(out_lo, out_hi),
        token_method=token_method,
    )


def projected_variable_tokens(header_tokens: int = 120) -> tuple[int, int]:
    """Per-title uncached input range when no plot files exist: summary + markers + schema."""
    lo, hi = (math.ceil(w * CHARS_PER_WORD / CHARS_PER_TOKEN) for w in SUMMARY_WORDS_RANGE)
    return lo + header_tokens + schema_tokens(), hi + header_tokens + schema_tokens()


def projection_table(static_tokens: int, models: list[str]) -> list[CostEstimate]:
    """The standard table: smoke (1 title, no batch), gold run (100) and pilot (500) via Batch,
    each with and without caching."""
    rows = []
    variable = projected_variable_tokens()
    for model in models:
        rows.append(
            estimate(
                model,
                1,
                static_tokens=static_tokens,
                variable_tokens=variable,
                batch=False,
                caching=True,
            )
        )
        for titles in (100, 500):
            for caching in (True, False):
                rows.append(
                    estimate(
                        model,
                        titles,
                        static_tokens=static_tokens,
                        variable_tokens=variable,
                        batch=True,
                        caching=caching,
                    )
                )
    return rows


def format_table(rows: list[CostEstimate]) -> str:
    lines = [
        "| Model | Titles | Batch | Caching | Est. cost (USD) | Input tok/title "
        "| Output tok/title |",
        "|---|---:|---|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.model} | {r.titles} | {'yes' if r.batch else 'no'} | "
            f"{'yes' if r.caching else 'no'} | ${r.usd_low:,.2f} to ${r.usd_high:,.2f} | "
            f"{r.input_tokens_per_title[0]:,} to {r.input_tokens_per_title[1]:,} | "
            f"{r.output_tokens_per_title[0]:,} to {r.output_tokens_per_title[1]:,} |"
        )
    lines.append("")
    lines.append(f"Tokens: {rows[0].token_method if rows else ESTIMATE_METHOD}.")
    lines.append(f"Prices: {PRICE_SOURCE}.")
    return "\n".join(lines)
