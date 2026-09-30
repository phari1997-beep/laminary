"""Thin wrapper over the Anthropic SDK: one request, Batch API submit/poll/results, token counts.

The pipeline talks to ``AnnotationAPI`` (normalized plain-data results), so tests use a fake and
never touch the network. ``SdkAnnotationAPI`` adapts the official ``anthropic`` SDK, which is
imported lazily: dry runs and tests need neither the package nor a key.

The API key is read only from the ANTHROPIC_API_KEY environment variable and passed explicitly
to the SDK, so no other credential source (profiles, auth tokens, files) is used. It is never
logged, printed or written to run files.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from laminary_pipeline.annotate.config import API_KEY_ENV

# SDK-level retries for transient HTTP failures (429, 5xx, connection errors). Validation
# retries are the pipeline's own and are counted separately.
SDK_MAX_RETRIES = 3


class MissingApiKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass(frozen=True)
class Response:
    """One Messages API response, reduced to what the pipeline uses."""

    model: str
    stop_reason: str | None
    text: str  # all text blocks joined; with structured outputs this is the JSON
    usage: Usage
    request_id: str | None = None
    stop_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class BatchResult:
    """One batch entry. ``kind`` is succeeded, errored, canceled or expired."""

    custom_id: str
    kind: str
    response: Response | None = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BatchStatus:
    batch_id: str
    processing_status: str  # in_progress, canceling, ended
    counts: dict[str, int] = field(default_factory=dict)


class AnnotationAPI(Protocol):
    def create(self, params: dict[str, Any]) -> Response: ...
    def submit_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str: ...
    def batch_status(self, batch_id: str) -> BatchStatus: ...
    def batch_results(self, batch_id: str) -> Iterator[BatchResult]: ...
    def count_tokens(self, params: dict[str, Any]) -> int: ...


class RequestRejectedError(RuntimeError):
    """The API refused the request itself (4xx other than rate limits), e.g. a schema that is
    too complex to compile. Not retried by the pipeline."""


def _usage(sdk_usage: Any) -> Usage:
    def get(name: str) -> int:
        return int(getattr(sdk_usage, name, 0) or 0)

    return Usage(
        get("input_tokens"),
        get("output_tokens"),
        get("cache_read_input_tokens"),
        get("cache_creation_input_tokens"),
    )


def response_from_sdk(message: Any, request_id: str | None = None) -> Response:
    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    details = getattr(message, "stop_details", None)
    if details is not None and hasattr(details, "to_dict"):
        details = details.to_dict()
    return Response(
        model=message.model,
        stop_reason=message.stop_reason,
        text=text,
        usage=_usage(message.usage),
        request_id=request_id if request_id is not None else getattr(message, "_request_id", None),
        stop_details=details,
    )


class SdkAnnotationAPI:
    """``AnnotationAPI`` on top of an ``anthropic.Anthropic`` client (or a test double)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(self, params: dict[str, Any]) -> Response:
        import anthropic

        try:
            message = self._client.messages.create(**params)
        except anthropic.RateLimitError:
            raise
        except anthropic.APIStatusError as e:
            if 400 <= e.status_code < 500:
                raise RequestRejectedError(f"HTTP {e.status_code}: {e.message}") from e
            raise
        return response_from_sdk(message)

    def submit_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        batch = self._client.messages.batches.create(
            requests=[{"custom_id": cid, "params": params} for cid, params in requests]
        )
        return batch.id

    def batch_status(self, batch_id: str) -> BatchStatus:
        batch = self._client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        return BatchStatus(
            batch_id=batch.id,
            processing_status=batch.processing_status,
            counts={
                k: int(getattr(counts, k, 0) or 0)
                for k in ("processing", "succeeded", "errored", "canceled", "expired")
            },
        )

    def batch_results(self, batch_id: str) -> Iterator[BatchResult]:
        for entry in self._client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                yield BatchResult(entry.custom_id, "succeeded", response_from_sdk(result.message))
            elif result.type == "errored":
                err = getattr(result.error, "error", result.error)
                yield BatchResult(
                    entry.custom_id,
                    "errored",
                    error_type=getattr(err, "type", None),
                    error_message=getattr(err, "message", None),
                )
            else:
                yield BatchResult(entry.custom_id, result.type)

    def count_tokens(self, params: dict[str, Any]) -> int:
        allowed = {"model", "system", "messages", "output_config", "cache_control"}
        resp = self._client.messages.count_tokens(
            **{k: v for k, v in params.items() if k in allowed}
        )
        return int(resp.input_tokens)


def make_api() -> SdkAnnotationAPI:
    """A live API wrapper. Reads the key from ANTHROPIC_API_KEY only."""
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise MissingApiKeyError(
            f"{API_KEY_ENV} is not set. Live calls need Hari's key in that environment "
            "variable (and his spend approval); dry runs need neither."
        )
    try:
        import anthropic
    except ImportError as e:
        raise MissingApiKeyError(
            "the Anthropic SDK is not installed: pip install 'anthropic>=1.9,<2'"
        ) from e
    return SdkAnnotationAPI(anthropic.Anthropic(api_key=key, max_retries=SDK_MAX_RETRIES))
