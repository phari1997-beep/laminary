"""The SDK adapter (against a fake SDK client, no network), key handling, and cost maths."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from laminary_pipeline.annotate import client as client_mod
from laminary_pipeline.annotate.client import (
    MissingApiKeyError,
    SdkAnnotationAPI,
    Usage,
    make_api,
    response_from_sdk,
)
from laminary_pipeline.annotate.config import MODELS
from laminary_pipeline.annotate.cost import (
    estimate,
    format_table,
    price_usage,
    projection_table,
)


def sdk_message(text='{"a": 1}', stop_reason="end_turn", model="claude-opus-5-5"):
    return NS(
        model=model,
        stop_reason=stop_reason,
        stop_details=None,
        content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
        usage=NS(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=30,
        ),
        _request_id="req_1",
    )


class FakeBatches:
    def __init__(self):
        self.created = None

    def create(self, requests):
        self.created = requests
        return NS(id="msgbatch_x")

    def retrieve(self, batch_id):
        return NS(
            id=batch_id,
            processing_status="ended",
            request_counts=NS(processing=0, succeeded=1, errored=1, canceled=0, expired=1),
        )

    def results(self, batch_id):
        yield NS(custom_id="a", result=NS(type="succeeded", message=sdk_message()))
        yield NS(
            custom_id="b",
            result=NS(
                type="errored",
                error=NS(type="error", error=NS(type="invalid_request_error", message="bad")),
            ),
        )
        yield NS(custom_id="c", result=NS(type="expired"))


class FakeMessages:
    def __init__(self):
        self.batches = FakeBatches()
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return sdk_message()

    def count_tokens(self, **kwargs):
        self.kwargs = kwargs
        return NS(input_tokens=1234)


@pytest.fixture
def api():
    return SdkAnnotationAPI(NS(messages=FakeMessages()))


def test_response_conversion_joins_text_and_normalizes_usage() -> None:
    r = response_from_sdk(sdk_message())
    assert r.text == '{"a": 1}' and r.request_id == "req_1"
    assert r.usage == Usage(10, 20, 0, 30)


def test_create_passes_params_through(api) -> None:
    pytest.importorskip("anthropic")
    params = {"model": "claude-opus-5-5", "max_tokens": 5, "messages": []}
    r = api.create(params)
    assert api._client.messages.kwargs == params and r.stop_reason == "end_turn"


def test_batch_submit_status_results(api) -> None:
    assert api.submit_batch([("a", {"model": "m"})]) == "msgbatch_x"
    assert api._client.messages.batches.created == [{"custom_id": "a", "params": {"model": "m"}}]
    status = api.batch_status("msgbatch_x")
    assert status.processing_status == "ended" and status.counts["expired"] == 1
    a, b, c = api.batch_results("msgbatch_x")
    assert a.kind == "succeeded" and a.response.text == '{"a": 1}'
    assert (b.kind, b.error_type, b.error_message) == ("errored", "invalid_request_error", "bad")
    assert c.kind == "expired" and c.response is None


def test_count_tokens_drops_max_tokens(api) -> None:
    n = api.count_tokens({"model": "m", "max_tokens": 5, "messages": [], "system": []})
    assert n == 1234 and "max_tokens" not in api._client.messages.kwargs


def test_make_api_requires_env_key_and_never_echoes_it(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError) as e:
        make_api()
    assert "ANTHROPIC_API_KEY" in str(e.value)


def test_make_api_passes_the_env_key_explicitly(monkeypatch) -> None:
    anthropic = pytest.importorskip("anthropic")
    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", Recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    api = make_api()
    assert seen["api_key"] == "sk-test-not-real"
    assert seen["max_retries"] == client_mod.SDK_MAX_RETRIES
    assert seen["base_url"] == "https://api.anthropic.com"  # ANTHROPIC_BASE_URL can't redirect
    assert "sk-test" not in repr(api)


# --- cost ----------------------------------------------------------------------------------


def test_price_usage_matches_the_table() -> None:
    spec = MODELS["claude-opus-5-5"]
    usage = Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    expected = (
        spec.input_per_mtok
        + spec.output_per_mtok
        + spec.cache_read_per_mtok
        + spec.input_per_mtok * 1.25
    )
    assert price_usage("claude-opus-5-5", usage, batch=False) == pytest.approx(expected)
    assert price_usage("claude-opus-5-5", usage, batch=True) == pytest.approx(expected / 2)


def test_estimate_orders_batch_caching_and_models() -> None:
    kw = dict(static_tokens=5000, variable_tokens=(1000, 2000))
    std = estimate("claude-opus-5-5", 100, batch=False, caching=True, **kw)
    bat = estimate("claude-opus-5-5", 100, batch=True, caching=True, **kw)
    nocache = estimate("claude-opus-5-5", 100, batch=True, caching=False, **kw)
    sonnet = estimate("claude-sonnet-5-5", 100, batch=True, caching=True, **kw)
    assert bat.usd_high == pytest.approx(std.usd_high / 2, rel=1e-3)
    assert bat.usd_low < nocache.usd_low and bat.usd_high < nocache.usd_high
    assert sonnet.usd_high < bat.usd_high
    assert std.usd_low <= std.usd_high


def test_prefix_below_cache_minimum_is_priced_uncached() -> None:
    kw = dict(static_tokens=3000, variable_tokens=(1000, 1000), batch=True)
    on = estimate("claude-haiku-4-5", 50, caching=True, **kw)  # Haiku minimum is 4096
    off = estimate("claude-haiku-4-5", 50, caching=False, **kw)
    assert on.usd_low == off.usd_low


def test_projection_table_labels_estimates_and_unconfirmed_prices() -> None:
    rows = projection_table(5000, ["claude-opus-5-5"])
    assert [(r.titles, r.batch, r.caching) for r in rows] == [
        (1, False, True),
        (100, True, True),
        (100, True, False),
        (500, True, True),
        (500, True, False),
    ]
    text = format_table(rows)
    assert "ESTIMATE" in text and "UNCONFIRMED" in text
