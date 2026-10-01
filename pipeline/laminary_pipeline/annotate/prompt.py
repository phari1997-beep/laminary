"""Versioned annotation prompt and the request builder.

Request layout, ordered for prompt caching (render order: tools, system, messages):

- ``system``: the prompt file body, byte for byte, with a ``cache_control`` breakpoint. It is
  identical for every title, so it is the cached prefix.
- ``messages[0]``: the per-title part, after the breakpoint. A short header, then per source:
  an opening marker block, the source text as its own block (exactly the gated text), and a
  closing marker block.
- ``output_config.format``: ``model_output_schema()`` as a JSON-schema structured output.

Only Wikipedia-derived identifiers go into the request besides the summary: the article title
from each source's en.wikipedia.org ref (validated, see ``wikipedia_article_title``) and the
media type. The title's display name and year are never sent; they come from Wikidata
candidates and are kept out so the model reads only the summary (docs/NARRATIVE_SCHEMA.md
section 1).

``verify_request`` re-checks the built request block by block: the system prompt is the pinned
body, every non-source block equals its expected constant or a marker rebuilt from the
validated ref, and every source block passes the hash and word-count checks.

What the hash check proves: ``content_sha256`` and ``word_count`` come from the same local plot
file as the text, so a match shows the text is internally consistent and unchanged since
ingest wrote the file. It does not prove the text is what Wikipedia served for that revision;
that rests on ingest (and its HTTP cache) and on the local data directory not being tampered
with.
"""

from __future__ import annotations

import hashlib
import unicodedata
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
from laminary_pipeline.annotation import WIKIPEDIA_ARTICLE_REF
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


ARTICLE_PREFIX = "https://en.wikipedia.org/wiki/"
FORBIDDEN_TITLE_CHARS = frozenset('<>[]{}|#"')
MAX_TITLE_CHARS = 255


def wikipedia_article_title(ref: str) -> str:
    """'https://en.wikipedia.org/wiki/The_Matrix' -> 'The Matrix'. Raises GateError unless
    the ref is a plain article URL and the decoded title is safe to put inside the marker:
    no ``<>[]{}|#"``, no control or line-separator characters, at most 255 characters (the
    MediaWiki title limit)."""
    if not isinstance(ref, str) or not WIKIPEDIA_ARTICLE_REF.match(ref):
        raise GateError(f"ref {ref!r} is not a plain en.wikipedia.org article URL")
    path = urlparse(ref).path
    if not path.startswith("/wiki/"):
        raise GateError(f"ref {ref!r} is not an article URL")
    try:
        title = unquote(path[len("/wiki/") :], errors="strict").replace("_", " ")
    except UnicodeDecodeError as e:
        raise GateError(f"ref {ref!r}: article title is not valid UTF-8") from e
    bad = sorted(
        {c for c in title if c in FORBIDDEN_TITLE_CHARS or unicodedata.category(c)[0] in "CZ"}
        - {" "}
    )
    if bad or not title.strip() or len(title) > MAX_TITLE_CHARS:
        raise GateError(f"ref {ref!r}: unsafe or invalid article title {title!r}")
    return title


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


def _header(gated: GatedInput) -> str:
    plot = gated.plot
    n = len(plot.sources)
    return (
        f"Work type: {MEDIA_LABELS[plot.title['media_type']]}\n"
        f"The plot summary follows in {n} part{'s' if n > 1 else ''}."
    )


def _user_content(gated: GatedInput) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": _header(gated)}]
    for i, src in enumerate(gated.plot.sources):
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
    verify_request(params, gated, prompt)
    return gated, params


def _expected_non_source_blocks(gated: GatedInput) -> dict[int, str]:
    """Position -> exact text of every block that isn't summary text."""
    sources = gated.plot.sources
    expected = {0: _header(gated)}
    for i, src in enumerate(sources):
        expected[1 + 3 * i] = _open_marker(i, src.meta)
        expected[3 + 3 * i] = CLOSE_MARKER
    expected[1 + 3 * len(sources)] = INSTRUCTION
    return expected


def verify_request(
    params: dict[str, Any], gated: GatedInput, prompt: Prompt | None = None
) -> None:
    """Re-check the built request against the gated sources (fail closed): one user message;
    every block is a plain text block; every non-source block is exactly the expected constant
    or a marker rebuilt from the validated ref; every source block passes the hash and
    word-count checks; with ``prompt``, the system prompt is the pinned body."""
    if prompt is not None:
        system = params.get("system")
        if (
            not isinstance(system, list)
            or len(system) != 1
            or system[0].get("text") != prompt.body
        ):
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
    for idx, text in _expected_non_source_blocks(gated).items():
        if content[idx]["text"] != text:
            raise GateError(f"request block {idx} is not the expected marker or instruction")
    total = 0
    for i, (idx, src) in enumerate(zip(indices, sources, strict=True)):
        text = content[idx]["text"]
        check_text(text, src.meta, f"{gated.plot.key} request block {idx} (source {i})")
        total += len(text.split())
    if total < MIN_SUMMARY_WORDS:
        raise GateError(f"{gated.plot.key}: request carries {total} words of summary")
