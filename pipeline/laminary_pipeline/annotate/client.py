"""Thin wrapper over the Anthropic SDK: one request, Batch API submit/poll/results, token counts.

The pipeline talks to ``AnnotationAPI`` (normalized plain-data results), so tests use a fake and
never touch the network. ``SdkAnnotationAPI`` adapts the official ``anthropic`` SDK, which is
imported lazily: dry runs and tests need neither the package nor a key.

The API key is read only from the ANTHROPIC_API_KEY environment variable and passed explicitly
to the SDK, together with a pinned ``base_url``, so no other credential source (profiles, auth
tokens, files) is used and ``ANTHROPIC_BASE_URL`` can't redirect the key. It is never logged,
printed or written to run files.

Retries: requests that can cost money (``messages.create``, ``messages.batches.create``) are sent
with SDK retries OFF. Every retry is the pipeline's own, logged in attempts.jsonl and counted
against the budget. Failures are normalized to the exceptions below so the runner never needs the
SDK: ``RequestRejectedError`` (4xx about this request; not billed; don't retry it),
``TransientAPIError`` (429, 5xx, timeouts, connection errors; ``possibly_billed`` when the
request may have reached the model) and ``FatalAPIError`` (auth/permission problems, or a batch
call that failed: stop the run).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from laminary_pipeline.annotate.config import API_BASE_URL, API_KEY_ENV

# SDK-level retries, used ONLY for read-only calls (batch status and results), which cost
# nothing. Billable calls run with max_retries=0 (see the module docstring).
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


class AnnotationAPIError(RuntimeError):
    """Base class for normalized API failures."""


class RequestRejectedError(AnnotationAPIError):
    """The API refused the request itself (4xx other than rate limits and auth), e.g. a schema
    that is too complex to compile. Not billed; not retried by the pipeline."""


class TransientAPIError(AnnotationAPIError):
    """429, 5xx, timeout or connection failure. ``possibly_billed`` is True when the request may
    have reached the model (timeouts, dropped connections, 5xx), so the spend tracker reserves
    that request's worst-case cost."""

    def __init__(self, message: str, *, possibly_billed: bool) -> None:
        super().__init__(message)
        self.possibly_billed = possibly_billed


class FatalAPIError(AnnotationAPIError):
    """A failure that ends the run (bad key, no permission, batch call failed)."""


FATAL_STATUSES = frozenset({401, 403, 404})


def classify_sdk_error(e: Exception) -> AnnotationAPIError:
    """Map an ``anthropic`` exception to the pipeline's normalized errors."""
    import anthropic

    if isinstance(e, anthropic.APITimeoutError):
        return TransientAPIError(f"timeout: {e}", possibly_billed=True)
    if isinstance(e, anthropic.APIConnectionError):
        return TransientAPIError(f"connection error: {e}", possibly_billed=True)
    if isinstance(e, anthropic.RateLimitError):
        return TransientAPIError(f"HTTP 429: {e.message}", possibly_billed=False)
    if isinstance(e, anthropic.APIStatusError):
        code = e.status_code
        if code >= 500 or code == 408:
            return TransientAPIError(f"HTTP {code}: {e.message}", possibly_billed=True)
        if code in FATAL_STATUSES:
            return FatalAPIError(f"HTTP {code}: {e.message}")
        if code == 409:
            return TransientAPIError(f"HTTP {code}: {e.message}", possibly_billed=False)
        return RequestRejectedError(f"HTTP {code}: {e.message}")
    # Anything else (e.g. APIResponseValidationError after a 200 that was billed) may have
    # reached the model: retryable, with the request's worst case reserved.
    return TransientAPIError(f"{type(e).__name__}: {e}", possibly_billed=True)


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

    def _no_retry(self) -> Any:
        """The client with SDK retries off, for billable calls."""
        with_options = getattr(self._client, "with_options", None)
        return with_options(max_retries=0) if with_options else self._client

    def create(self, params: dict[str, Any]) -> Response:
        import anthropic

        try:
            message = self._no_retry().messages.create(**params)
        except anthropic.APIError as e:
            raise classify_sdk_error(e) from e
        return response_from_sdk(message)

    def submit_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        """No retry: a retried create after a lost response could create (and bill) a second
        batch. Any failure is fatal; the runner stops and says to check the Console."""
        import anthropic

        try:
            batch = self._no_retry().messages.batches.create(
                requests=[{"custom_id": cid, "params": params} for cid, params in requests]
            )
        except anthropic.APIError as e:
            raise FatalAPIError(f"batch submission failed: {classify_sdk_error(e)}") from e
        return batch.id

    def batch_status(self, batch_id: str) -> BatchStatus:
        import anthropic

        try:
            batch = self._client.messages.batches.retrieve(batch_id)
        except anthropic.APIError as e:
            raise FatalAPIError(f"batch status failed: {classify_sdk_error(e)}") from e
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
        import anthropic

        try:
            entries = list(self._client.messages.batches.results(batch_id))
        except anthropic.APIError as e:
            raise FatalAPIError(f"batch results failed: {classify_sdk_error(e)}") from e
        for entry in entries:
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
    return SdkAnnotationAPI(
        anthropic.Anthropic(api_key=key, base_url=API_BASE_URL, max_retries=SDK_MAX_RETRIES)
    )
