"""MacroAdapter: distinguishing an absent openbb-data sidecar from a live-but-
broken one (Phase 14d — every sidecar-down surface says how to enable itself).

`_results` is exercised directly with a fake httpx client stand-in (mirrors
test_fallback.py's fake-adapter style) so the contract is checked without a
real openbb-data sidecar.
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters.macro_adapter import MacroAdapter
from app.config import Settings
from app.errors import SidecarNotEnabled, UpstreamError


def _settings() -> Settings:
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r",
    )


def _adapter() -> MacroAdapter:
    return MacroAdapter(redis=object(), settings=_settings())


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = str(body)

    def json(self):
        return self._body


class _FakeClient:
    """Stand-in for httpx.AsyncClient: raises `exc` or returns `response`."""

    def __init__(self, *, exc=None, response=None):
        self._exc = exc
        self._response = response

    async def get(self, url, params=None):
        if self._exc is not None:
            raise self._exc
        return self._response


async def test_connect_error_raises_sidecar_not_enabled() -> None:
    client = _FakeClient(exc=httpx.ConnectError("Connection refused"))
    with pytest.raises(SidecarNotEnabled) as exc_info:
        await _adapter()._results(client, "/economy/indicators", {})
    err = exc_info.value
    assert err.code == "SIDECAR_NOT_ENABLED"
    assert "--profile macro" in err.public_message
    assert err.envelope()["error"]["code"] == "SIDECAR_NOT_ENABLED"


async def test_connect_timeout_raises_upstream_error_not_sidecar_not_enabled() -> None:
    # A RUNNING sidecar behind a firewall DROP also raises ConnectTimeout
    # (security review, 2026-08-15: hit exactly this during a host
    # migration) — telling the operator "it isn't running" would misdirect
    # them, so only ConnectError (refused/no route) is SidecarNotEnabled.
    client = _FakeClient(exc=httpx.ConnectTimeout("timed out connecting"))
    with pytest.raises(UpstreamError) as exc_info:
        await _adapter()._results(client, "/economy/indicators", {})
    assert not isinstance(exc_info.value, SidecarNotEnabled)


async def test_read_timeout_is_upstream_error_not_sidecar_not_enabled() -> None:
    # A read timeout means the connection was established (the sidecar IS
    # listening) — it's just slow/live-but-broken, not absent.
    client = _FakeClient(exc=httpx.ReadTimeout("read timed out"))
    with pytest.raises(UpstreamError) as exc_info:
        await _adapter()._results(client, "/economy/indicators", {})
    assert not isinstance(exc_info.value, SidecarNotEnabled)


async def test_http_500_raises_upstream_error() -> None:
    client = _FakeClient(response=_FakeResponse(500, {"detail": "boom"}))
    with pytest.raises(UpstreamError) as exc_info:
        await _adapter()._results(client, "/economy/indicators", {})
    assert exc_info.value.code == "UPSTREAM_ERROR"
