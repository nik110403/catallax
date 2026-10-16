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


# ── redaction and bounds on untrusted upstream data ─────────────────────────────
async def test_openbb_error_detail_is_redacted_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    resp = _FakeResponse(500, {"detail": "boom https://x/y?api_key=SECRETKEY99&file_type=json"})
    with caplog.at_level("DEBUG"), pytest.raises(UpstreamError):
        await _adapter()._results(_FakeClient(response=resp), "/p", {})
    assert "SECRETKEY99" not in caplog.text and "api_key=***" in caplog.text
    resp = _FakeResponse(500, None)
    resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
    resp.text = "plain api_key=SECRETKEY77 text"
    caplog.clear()
    with caplog.at_level("DEBUG"), pytest.raises(UpstreamError):
        await _adapter()._results(_FakeClient(response=resp), "/p", {})
    assert "SECRETKEY77" not in caplog.text


class _Redis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def get(self, k):  # noqa: ANN001, ANN201
        return self.kv.get(k)

    async def set(self, k, v, ex=None):  # noqa: ANN001, ANN201
        self.kv[k] = v


def _live_adapter(redis: _Redis) -> MacroAdapter:
    return MacroAdapter(redis=redis, settings=Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", upstream_mode="live"))


async def test_series_drops_non_finite_values_so_the_cached_copy_revalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters.macro_adapter import MacroSeries

    redis = _Redis()
    a = _live_adapter(redis)

    async def _rows(self, client, path, params):  # noqa: ANN001
        return [{"country": "United States", "date": "2026-01-01", "value": float("nan")},
                {"country": "United States", "date": "2026-02-01", "value": float("inf")},
                {"country": "United States", "date": "2026-03-01", "value": 1.5}]

    monkeypatch.setattr(MacroAdapter, "_results", _rows)
    monkeypatch.setattr(MacroAdapter, "_client", lambda self: __import__("contextlib").nullcontext(object()))
    env = await a.series("CPI.united_states", "toya")
    assert [p.value for p in env.data.points] == [1.5]
    assert MacroSeries.model_validate_json(redis.kv["macro:series:CPI.united_states:toya"]).points[0].value == 1.5


async def test_lists_and_strings_are_capped_before_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.adapters.macro_adapter as ma

    redis = _Redis()
    a = _live_adapter(redis)
    n = ma._MAX_LIST + 50

    async def _rows(self, client, path, params):  # noqa: ANN001
        return [{"country": "United States", "date": "x" * 5000 if i == 0 else f"2000-{i}", "value": 1.0} for i in range(n)]

    monkeypatch.setattr(MacroAdapter, "_results", _rows)
    monkeypatch.setattr(MacroAdapter, "_client", lambda self: __import__("contextlib").nullcontext(object()))
    env = await a.series("CPI.united_states", "toya")
    assert len(env.data.points) <= ma._MAX_LIST and all(len(p.date) <= ma._MAX_STR for p in env.data.points)


async def test_oversized_payload_is_not_cached(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import app.adapters.macro_adapter as ma

    redis = _Redis()
    a = _live_adapter(redis)
    monkeypatch.setattr(ma, "_MAX_PAYLOAD_BYTES", 100)

    async def _rows(self, client, path, params):  # noqa: ANN001
        return [{"country": "United States", "date": f"2026-01-{i:02d}", "value": 1.0} for i in range(1, 20)]

    monkeypatch.setattr(MacroAdapter, "_results", _rows)
    monkeypatch.setattr(MacroAdapter, "_client", lambda self: __import__("contextlib").nullcontext(object()))
    with caplog.at_level("WARNING"):
        env = await a.series("CPI.united_states", "toya")
    assert env.data.points and not redis.kv and "macro cache skipped" in caplog.text
