"""Broker adapter factory — dispatch `settings.algo_broker` to a `BrokerAdapter`.

`paper` (`PaperBroker`) is the only enabled broker — it fill-simulates against
the F2 price chain. `nautilus` and `alpaca-paper` are REGISTERED but not
enabled — they prove the seam is a framework and document the drop-in path for
a real paper/live venue: implement `BrokerAdapter` over the SDK, register it
here, done — no service/router change. Mirrors
`backend/app/adapters/backtest/factory.py`.
"""
from __future__ import annotations

from app.adapters.broker.base import BrokerAdapter, PaperBroker
from app.config import Settings


class _NautilusBrokerStub(BrokerAdapter):
    """Documented drop-in slot for Nautilus Trader's paper-trading venue. Not
    enabled: needs its own broker/exchange integration + credentials wiring."""

    name = "nautilus"
    available = False

    async def mark(self, symbols: list[str]) -> dict[str, float]:
        raise ValueError(
            "nautilus is a documented future drop-in; install + register to enable"
        )

    async def health(self) -> bool:
        return False


class _AlpacaPaperBrokerStub(BrokerAdapter):
    """Documented drop-in slot for Alpaca's paper-trading REST API. Not
    enabled: needs API key wiring + its own rate-limit handling."""

    name = "alpaca-paper"
    available = False

    async def mark(self, symbols: list[str]) -> dict[str, float]:
        raise ValueError(
            "alpaca-paper is a documented future drop-in; install + register to enable"
        )

    async def health(self) -> bool:
        return False


KNOWN_BROKERS: dict[str, type[BrokerAdapter]] = {
    PaperBroker.name: PaperBroker,
    _NautilusBrokerStub.name: _NautilusBrokerStub,
    _AlpacaPaperBrokerStub.name: _AlpacaPaperBrokerStub,
}

_NOTES: dict[str, str] = {
    PaperBroker.name: "fill-simulation adapter wrapping the F2 price chain",
    _NautilusBrokerStub.name: "documented drop-in: Nautilus Trader paper venue (not enabled)",
    _AlpacaPaperBrokerStub.name: "documented drop-in: Alpaca paper-trading REST API (not enabled)",
}


def build_broker(name: str, settings: Settings) -> BrokerAdapter:
    cls = KNOWN_BROKERS.get(name)
    if cls is None:
        raise ValueError(f"unknown broker '{name}' (have: {', '.join(sorted(KNOWN_BROKERS))})")
    if not cls.available:
        raise ValueError(
            f"{name} is a documented future drop-in; install + register to enable"
        )
    return cls(settings)


def known_brokers() -> list[dict]:
    return [
        {"name": name, "available": cls.available, "note": _NOTES[name]}
        for name, cls in KNOWN_BROKERS.items()
    ]
