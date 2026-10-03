"""Where pipeline data lives (local-first, DECISIONS 2026-09-26: JSON/JSONL files, no database).

Default: ``pipeline/data/`` next to the package (an editable install, as in CI and dev).
Override with ``--data-dir`` or the ``LAMINARY_DATA_DIR`` environment variable.

Everything under the data dir is gitignored except ``README.md`` and ``config/`` (hand-written
inputs such as the gold seed list and the similarity pairs).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PIPELINE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PIPELINE_DIR / "data"
DATA_DIR_ENV = "LAMINARY_DATA_DIR"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike[str] | None = None) -> DataPaths:
        chosen = data_dir or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
        return cls(Path(chosen).resolve())

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def cache(self) -> Path:
        return self.root / "cache" / "http"

    @property
    def candidates(self) -> Path:
        return self.root / "pilot_candidates.jsonl"

    @property
    def effective_pilot(self) -> Path:
        """Titles the annotation pilot should use: passing plots, reserves filling gaps."""
        return self.root / "pilot_effective.jsonl"

    @property
    def pairs_candidates(self) -> Path:
        """Titles the similarity pairs need that the candidates lack (``ingest pairs``)."""
        return self.root / "pairs_candidates.jsonl"

    @property
    def pairs_resolved(self) -> Path:
        """The similarity pairs as QIDs, in the ``evaluate/pairs.py`` format."""
        return self.root / "similarity_pairs_resolved.csv"

    @property
    def plots(self) -> Path:
        return self.root / "plots"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def gold(self) -> Path:
        """The upload folder: only files meant for the shared Drive folder (the sheet, its
        CSV fallbacks, ``texts/``) and the imported labels."""
        return self.root / "gold"

    @property
    def gold_internal(self) -> Path:
        """Gold files that stay local: the selection, with the selector's guesses."""
        return self.root / "gold_internal"

    @property
    def gold_seeds(self) -> Path:
        return self.config / "gold_seed_titles.csv"

    @property
    def similarity_pairs(self) -> Path:
        return self.config / "similarity_pairs.csv"

    def plot_file(self, qid: str) -> Path:
        return self.plots / f"{qid}.json"


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file and rename, so an interrupted run never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    n = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False))
                fh.write("\n")
                n += 1
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return n


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
