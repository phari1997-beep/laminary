"""Versioned annotation prompt and the request builder.

Request layout, ordered for prompt caching (render order: tools, system, messages):

- ``system``: the prompt file body, byte for byte, with a ``cache_control`` breakpoint. It is
  identical for every title, so it is the cached prefix.
- ``messages[0]``: the per-title part, after the breakpoint. A short header, then per source:
  an opening marker block, the source text as its own block (exactly the gated text), and a
  closing marker block.
- ``output_config.format``: ``model_output_schema()`` as a JSON-schema structured output.

Besides the summary, the request carries the article title from each source's
en.wikipedia.org ref (validated, see ``wikipedia_article_title``), the media type and, from
annotate-1.1.0 on, the release year. The release year is the only non-Wikipedia field
(Wikidata, CC0; DECISIONS 2026-09-30): the prompt's ``historical_past`` vs ``contemporary``
definitions are relative to it. It is sent only as a validated integer between
MIN_RELEASE_YEAR and the current year + 2 (``prompt_release_year``); any other value refuses the
title, and a missing year omits the line (the prompt says how to judge without it). The title's
display name is never sent, so the model reads only the summary (docs/NARRATIVE_SCHEMA.md
section 1).

``verify_request`` re-checks the built request block by block: the system prompt is the pinned
body, every non-source block equals its expected constant, the header rebuilt from the
validated year, or a marker rebuilt from the validated ref, and every source block passes the
hash and word-count checks.

What the hash check proves: ``content_sha256`` and ``word_count`` come from the same local plot
file as the text, so a match shows the text is internally consistent and unchanged since
ingest wrote the file. It does not prove the text is what Wikipedia served for that revision;
that rests on ingest (and its HTTP cache) and on the local data directory not being tampered
with.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from laminary_pipeline.annotate.config import MODELS, PROMPTS_DIR
from laminary_pipeline.annotate.inputs import (
    MIN_SUMMARY_WORDS,
    GatedInput,
    GateError,
    PlotInput,
    check_text,
    gate,
)
from laminary_pipeline.annotation import wikipedia_article_title as _article_title
from laminary_pipeline.model_output import model_output_schema


class PinnedPrompt(NamedTuple):
    filename: str
    sha256: str  # of the body sent
    sends_release_year: bool  # the user-message header carries "Release year: <year>"


# prompt_version -> pinned file. Editing a prompt without adding a new version here fails
# load_prompt(), so a record's prompt_version always names one exact text and header format.
PINNED_PROMPTS: dict[str, PinnedPrompt] = {
    "annotate-1.0.0": PinnedPrompt(
        "annotate_v1.md",
        "bb0b76670ab07abf9d60c645c2b72ab0b7780d92953c4a81c8b26adc44c94bf2",
        sends_release_year=False,
    ),
    "annotate-1.1.0": PinnedPrompt(
        "annotate_v1_1.md",
        "f481bcdb60b7802339a8f4b2e8ad57488c690bd03a15a8459705c3856ea2d397",
        sends_release_year=True,
    ),
}
MIN_RELEASE_YEAR = 1880
MAX_YEARS_AHEAD = 2

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

    @property
    def sends_release_year(self) -> bool:
        return PINNED_PROMPTS[self.version].sends_release_year


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
    filename, pinned, _ = PINNED_PROMPTS[version]
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
    """``annotation.wikipedia_article_title`` (the same check the input gate runs), raising
    GateError."""
    try:
        return _article_title(ref)
    except ValueError as e:
        raise GateError(str(e)) from e


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


def _current_year() -> int:
    return datetime.now(UTC).year


def prompt_release_year(title: dict[str, Any]) -> int | None:
    """The release year to send, or None if the title has none. Raises GateError unless it is
    a plain int (not a bool or a string) from MIN_RELEASE_YEAR to the current year + 2."""
    year = title.get("release_year")
    if year is None:
        return None
    if type(year) is not int:
        raise GateError(f"release_year {year!r} is not an integer")
    latest = _current_year() + MAX_YEARS_AHEAD
    if not MIN_RELEASE_YEAR <= year <= latest:
        raise GateError(f"release_year {year} is outside {MIN_RELEASE_YEAR}-{latest}")
    return year


def _header(gated: GatedInput, prompt: Prompt) -> str:
    plot = gated.plot
    n = len(plot.sources)
    lines = [f"Work type: {MEDIA_LABELS[plot.title['media_type']]}"]
    if prompt.sends_release_year:
        year = prompt_release_year(plot.title)
        if year is not None:
            lines.append(f"Release year: {year}")
    lines.append(f"The plot summary follows in {n} part{'s' if n > 1 else ''}.")
    return "\n".join(lines)


def _user_content(gated: GatedInput, prompt: Prompt) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": _header(gated, prompt)}]
    for i, src in enumerate(gated.plot.sources):
        blocks.append({"type": "text", "text": _open_marker(i, src.meta)})
        blocks.append({"type": "text", "text": src.text})
        blocks.append({"type": "text", "text": CLOSE_MARKER})
    blocks.append({"type": "text", "text": INSTRUCTION})
    return blocks


def labeler_text(gated: GatedInput) -> str:
    """The summary gold labelers read, matching the request exactly (DECISIONS 2026-09-26 and
    2026-10-01). One source: its text, byte for byte (so its SHA-256 is the source's
    ``content_sha256``). Several sources (season articles): each summary block as the request
    carries it, opening marker naming the article, text, closing marker, in request order,
    joined by a blank line."""
    sources = gated.plot.sources
    if len(sources) == 1:
        return sources[0].text
    return "\n\n".join(
        _open_marker(i, src.meta) + src.text + CLOSE_MARKER for i, src in enumerate(sources)
    )


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
        "messages": [{"role": "user", "content": _user_content(gated, prompt)}],
        "output_config": output_config,
    }
    verify_request(params, gated, prompt)
    return gated, params


def _expected_non_source_blocks(gated: GatedInput, prompt: Prompt) -> dict[int, str]:
    """Position -> exact text of every block that isn't summary text."""
    sources = gated.plot.sources
    expected = {0: _header(gated, prompt)}
    for i, src in enumerate(sources):
        expected[1 + 3 * i] = _open_marker(i, src.meta)
        expected[3 + 3 * i] = CLOSE_MARKER
    expected[1 + 3 * len(sources)] = INSTRUCTION
    return expected


def verify_request(params: dict[str, Any], gated: GatedInput, prompt: Prompt) -> None:
    """Re-check the built request against the gated sources (fail closed): the system prompt is
    the pinned body; one user message; every block is a plain text block; every non-source
    block is exactly the expected constant, the header rebuilt for ``prompt`` from the
    validated release year, or a marker rebuilt from the validated ref; every source block
    passes the hash and word-count checks."""
    system = params.get("system")
    if not isinstance(system, list) or len(system) != 1 or system[0].get("text") != prompt.body:
        raise GateError("request system prompt is not the pinned prompt body")
    messages = params["messages"]
    if len(messages) != 1 or messages[0]["role"] != "user":
        raise GateError("request must hold exactly one user message")
    content = messages[0]["content"]
    sources = gated.plot.sources
    indices = source_block_indices(len(sources))
    if len(content) != 2 + 3 * len(sources):
        raise GateError("request content does not match the gated sources")
    for idx, block in enumerate(content):
        if set(block) != {"type", "text"} or block["type"] != "text":
            raise GateError(f"request block {idx} is not a plain text block")
    for idx, text in _expected_non_source_blocks(gated, prompt).items():
        if content[idx]["text"] != text:
            raise GateError(f"request block {idx} is not the expected marker or instruction")
    total = 0
    for i, (idx, src) in enumerate(zip(indices, sources, strict=True)):
        text = content[idx]["text"]
        check_text(text, src.meta, f"{gated.plot.key} request block {idx} (source {i})")
        total += len(text.split())
    if total < MIN_SUMMARY_WORDS:
        raise GateError(f"{gated.plot.key}: request carries {total} words of summary")
