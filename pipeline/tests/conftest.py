"""Test-wide guards: no test can reach the network or use a real Anthropic key.

Autouse, so every test gets them: ANTHROPIC_API_KEY (and the other Anthropic credential and
base-URL variables) are removed, constructing ``anthropic.Anthropic`` raises, and opening any
socket connection raises. Tests that need a client pass a fake, or monkeypatch
``anthropic.Anthropic`` themselves (which overrides the guard for that test only).
"""

from __future__ import annotations

import socket

import pytest

ANTHROPIC_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
)


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise NetworkBlocked("tests must not open network connections")


@pytest.fixture(autouse=True)
def _no_network_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ANTHROPIC_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    try:
        import anthropic
    except ImportError:  # pragma: no cover - the SDK is optional for most tests
        return

    def _no_client(*args, **kwargs):
        raise NetworkBlocked("tests must not construct a real anthropic.Anthropic client")

    monkeypatch.setattr(anthropic, "Anthropic", _no_client)
