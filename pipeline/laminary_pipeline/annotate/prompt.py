"""Versioned annotation prompt and the request builder.

Request layout, ordered for prompt caching (render order: tools, system, messages):

- ``system``: the prompt file body, byte for byte, with a ``cache_control`` breakpoint. It is
  identical for every title, so it is the cached prefix.
- ``messages[0]``: the per-title part, after the breakpoint. A short header, then per source:
  an opening marker block, the source text as its own block (exactly the gated text), and a
  closing marker block.
- ``output_config.format``: ``model_output_schema()`` as a JSON-schema structured output.

Only Wikipedia-derived identifiers go into the header: the article title from each source's
en.wikipedia.org ref, and the media type. The title name and year in the record may come from
TMDB, so they are never sent (docs/NARRATIVE_SCHEMA.md section 1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from laminary_pipeline.annotate.config import MODELS, PROMPTS_DIR
from laminary_pipeline.annotate.inputs import (
    MIN_SUMMARY_WORDS,
    GatedInput,
    GateError,
    PlotInput,
    check_text,
    gate,
)
from laminary_pipeline.model_output import model_output_schema

# prompt_version -> (file name, sha256 of the body sent). Editing a prompt without adding a new
# version here fails load_prompt(), so a record's prompt_version always names one exact text.
PINNED_PROMPTS: dict[str, tuple[str, str]] = {
    "annotate-1.0.0": (
        "annotate_v1.md",
        "bb0b76670ab07abf9d60c645c2b72ab0b7780d92953c4a81c8b26adc44c94bf2",
    ),
}

MEDIA_LABELS = {"movie": "film", "tv_series": "TV series (annotate the whole series)"}


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    version: str
    path: Path
    body: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        raise PromptError("prompt file must start with a '---' front-matter block")
    end = raw.index("\n---\n", 4)
    meta = {}
    for line in raw[4:end].splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, raw[end + len("\n---\n") :]


def load_prompt(version: str, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    if version not in PINNED_PROMPTS:
        raise PromptError(f"unknown prompt version {version!r}; known: {sorted(PINNED_PROMPTS)}")
    filename, pinned = PINNED_PROMPTS[version]
    path = prompts_dir / filename
    meta, body = split_front_matter(path.read_text(encoding="utf-8"))
    if meta.get("prompt_version") != version:
        raise PromptError(f"{path} declares {meta.get('prompt_version')!r}, expected {version!r}")
    prompt = Prompt(version=version, path=path, body=body)
    if prompt.sha256 != pinned:
        raise PromptError(
            f"{path} body sha256 {prompt.sha256} != pinned {pinned}: the prompt changed; "
            "add a new prompt_version instead of editing a released one"
        )
    return prompt


def wikipedia_article_title(ref: str) -> str:
    """'https://en.wikipedia.org/wiki/The_Matrix' -> 'The Matrix'."""
    path = urlparse(ref).path
    return unquote(path.rsplit("/", 1)[-1]).replace("_", " ")


def _open_marker(i: int, meta: dict[str, Any]) -> str:
    return (
        f'<summary part="{i + 1}" source="English Wikipedia plot section" '
        f'article="{wikipedia_article_title(meta["ref"])}">\n'
    )


CLOSE_MARKER = "\n</summary>"
INSTRUCTION = (
    "Annotate this work from the summary above, following the system instructions. "
    "Return only the JSON object."
)


def _user_content(gated: GatedInput) -> list[dict[str, Any]]:
    plot = gated.plot
    n = len(plot.sources)
    header = (
        f"Work type: {MEDIA_LABELS[plot.title['media_type']]}\n"
        f"The plot summary follows in {n} part{'s' if n > 1 else ''}."
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": header}]
    for i, src in enumerate(plot.sources):
        blocks.append({"type": "text", "text": _open_marker(i, src.meta)})
        blocks.append({"type": "text", "text": src.text})
        blocks.append({"type": "text", "text": CLOSE_MARKER})
    blocks.append({"type": "text", "text": INSTRUCTION})
    return blocks


def source_block_indices(n_sources: int) -> list[int]:
    """Positions of the source-text blocks in the user content built above."""
    return [2 + 3 * i for i in range(n_sources)]


def build_request(
    plot: PlotInput,
    *,
    model: str,
    prompt: Prompt,
    effort: str | None,
    max_tokens: int,
) -> tuple[GatedInput, dict[str, Any]]:
    """Gate the plot, build the Messages API params, then re-check the built request.

    Raises GateError if the plot must not be sent. Returns the gated input and the params
    (JSON-serializable; usable as-is for messages.create and as a batch request's params).
    """
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; known: {sorted(MODELS)}")
    gated = gate(plot)
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": model_output_schema()}
    }
    if MODELS[model].supports_effort and effort:
        output_config["effort"] = effort
    params = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": prompt.body, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": _user_content(gated)}],
        "output_config": output_config,
    }
    verify_request(params, gated)
    return gated, params


def verify_request(params: dict[str, Any], gated: GatedInput) -> None:
    """Re-check the exact text in the built request against the gated sources (fail closed)."""
    messages = params["messages"]
    if len(messages) != 1 or messages[0]["role"] != "user":
        raise GateError("request must hold exactly one user message")
    content = messages[0]["content"]
    sources = gated.plot.sources
    indices = source_block_indices(len(sources))
    if len(content) != 2 + 3 * len(sources):
        raise GateError("request content does not match the gated sources")
    total = 0
    for i, (idx, src) in enumerate(zip(indices, sources, strict=True)):
        text = content[idx]["text"]
        check_text(text, src.meta, f"{gated.plot.key} request block {idx} (source {i})")
        total += len(text.split())
    if total < MIN_SUMMARY_WORDS:
        raise GateError(f"{gated.plot.key}: request carries {total} words of summary")
