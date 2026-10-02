"""Shared fixtures for the annotate/evaluate tests: synthetic plot inputs, model outputs, a fake
API and gold records. The plot text is invented for tests; it is not from any real summary."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.client import BatchResult, BatchStatus, Response, Usage
from laminary_pipeline.annotate.inputs import count_words, sha256_text
from laminary_pipeline.model_output import from_record

EXAMPLES = Path(__file__).resolve().parent / "examples"

STORY = (
    "Mara Venn keeps the lighthouse on a cold northern island and has not left it in eleven "
    "years. When a storm wrecks a supply boat on the rocks, she drags its only survivor, a "
    "boy named Tobiah, out of the surf and nurses him back to health. The boy carries a sealed "
    "letter addressed to a harbor town on the mainland and insists it must arrive before the "
    "winter solstice. Mara refuses at first, afraid of the crossing that drowned her brother, "
    "but a retired pilot named Oskar repairs her old sailboat and teaches her to read the "
    "currents again. The three set out together and are driven off course by fog, lose their "
    "food to a raiding crew, and shelter for a week on an abandoned whaling station where "
    "Mara learns the letter is a pardon for Tobiah's father. On the final night Oskar falls "
    "ill and Mara must bring the boat through the reef alone. She reaches the harbor at dawn, "
    "the pardon is delivered in time, and Tobiah's father is freed. Mara returns to the "
    "island in spring, but she now sails to the mainland every month and teaches the harbor "
    "children how to navigate by the stars. The story closes with her lighting the lamp for "
    "a boat she knows is carrying friends home."
)
assert count_words(STORY) >= 150


def source_meta(text: str = STORY, **overrides: Any) -> dict[str, Any]:
    meta = {
        "kind": "wikipedia_plot",
        "ref": "https://en.wikipedia.org/wiki/The_Lamp_Keeper_(2019_film)",
        "revision": "1234567890",
        "retrieved_at": "2026-09-30T00:00:00Z",
        "license": "CC-BY-SA-4.0",
        "word_count": count_words(text),
        "content_sha256": sha256_text(text),
    }
    meta.update(overrides)
    return meta


def ingest_plot(
    qid: str = "Q1001", tmdb_id: int | None = 5001, text: str = STORY, **source_overrides: Any
) -> dict[str, Any]:
    """A plot file in the ingest agent's shape (data/plots/<QID>.json)."""
    return {
        "fetcher_version": "1.4.0",
        "qid": qid,
        "status": "ok",
        "skip_reason": None,
        "candidate": {
            "title": f"Film {qid}",
            "year": 2019,
            "media_type": "movie",
            "tmdb_id": tmdb_id,
            "series_status": None,
        },
        "source": source_meta(text, **source_overrides),
        "text": text,
    }


def write_plots(plots_dir: Path, plots: list[dict[str, Any]]) -> None:
    plots_dir.mkdir(parents=True, exist_ok=True)
    for p in plots:
        (plots_dir / f"{p['qid']}.json").write_text(json.dumps(p), encoding="utf-8")


def load_example(name: str = "the_matrix") -> dict[str, Any]:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def valid_output(name: str = "the_matrix") -> dict[str, Any]:
    """A schema-valid model output (from an illustrative example)."""
    return from_record(load_example(name))


def response(
    output: dict[str, Any] | str | None = None,
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    usage: Usage | None = None,
) -> Response:
    text = output if isinstance(output, str) else json.dumps(output or valid_output())
    return Response(
        model=model,
        stop_reason=stop_reason,
        text=text,
        usage=usage or Usage(1000, 2000, 5000, 0),
        request_id="req_test",
    )


def invalid_output() -> dict[str, Any]:
    """Schema-valid JSON that fails a semantic check: primary plot judged absent."""
    out = valid_output()
    plot = out["layers"]["archetypal_plot"]
    plot["plots"][plot["primary"]["label"]]["present"] = False
    return out


class FakeAPI:
    """Scripted AnnotationAPI. ``responses`` is consumed per create() call; batch results are
    produced by ``batch_script(round_no, requests) -> list[BatchResult]``."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        batch_script: Any = None,
        polls_before_end: int = 1,
    ) -> None:
        self.responses = list(responses or [])
        self.created: list[dict[str, Any]] = []
        self.batch_script = batch_script
        self.polls_before_end = polls_before_end
        self.batches: dict[str, list[BatchResult]] = {}
        self.submitted: list[list[tuple[str, dict[str, Any]]]] = []
        self.polls: dict[str, int] = {}
        self.counted: list[dict[str, Any]] = []

    def create(self, params: dict[str, Any]) -> Response:
        self.created.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def submit_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        if isinstance(self.batch_script, Exception):
            raise self.batch_script
        self.submitted.append(requests)
        batch_id = f"msgbatch_{len(self.submitted)}"
        self.batches[batch_id] = self.batch_script(len(self.submitted), requests)
        self.polls[batch_id] = 0
        return batch_id

    def batch_status(self, batch_id: str) -> BatchStatus:
        self.polls[batch_id] += 1
        ended = self.polls[batch_id] > self.polls_before_end
        return BatchStatus(batch_id, "ended" if ended else "in_progress", {})

    def batch_results(self, batch_id: str) -> Iterator[BatchResult]:
        assert self.polls[batch_id] > self.polls_before_end, "results read before batch ended"
        yield from self.batches[batch_id]

    def count_tokens(self, params: dict[str, Any]) -> int:
        self.counted.append(params)
        return 9999


def gold_from_example(
    name: str,
    key_tmdb: int,
    *,
    primary: str | None = None,
    text: str = STORY,
    arc_label: str | None = None,
    outcome: str = "annotated",
) -> dict[str, Any]:
    """A light gold_label record built from an illustrative example."""
    ex = load_example(name)
    rec: dict[str, Any] = {
        "schema_version": ex["schema_version"],
        "record_kind": "gold_label",
        "title": {
            "media_type": "movie",
            "name": f"Gold {key_tmdb}",
            "release_year": 2019,
            "tmdb_id": key_tmdb,
            "wikidata_id": None,
        },
        "provenance": {
            "annotated_at": "2026-09-30T00:00:00Z",
            "annotator": {"labeler_id": "hari", "guide_version": "1.0.0"},
            "sources": [source_meta(text)],
            "input_word_count": count_words(text),
        },
        "outcome": outcome,
    }
    if outcome == "abstained":
        rec["abstain_reason"] = "summary_too_thin"
        return rec
    layers = ex["layers"]
    plot = copy.deepcopy(layers["archetypal_plot"])
    if primary:
        plot["primary"]["label"] = primary
    arc = layers["structural_skeleton"]["emotional_arc"]
    rec["layers"] = {
        "archetypal_plot": {"primary": {"label": plot["primary"]["label"]}, "plots": plot["plots"]},
        "mythic_blueprint": {
            "blueprint": {"label": layers["mythic_blueprint"]["blueprint"]["label"]},
            "stages": layers["mythic_blueprint"]["stages"],
        },
        "structural_skeleton": {
            "emotional_arc": {"label": arc_label or arc["label"], "method": "labeler_assigned"}
        },
    }
    rec["beat_tags"] = {"tags": ex["beat_tags"]["tags"]}
    return rec
