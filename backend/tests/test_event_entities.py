"""Phase 20 event entity universe + resolver."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.adapters.events.base import Universe
from app.config import Settings
from app.models import (
    AlgoPosition,
    AlgoStrategy,
    EventEntity,
    Forecast,
    JournalEntry,
    MarketEvent,
    Note,
    WatchlistItem,
)
from app.services import event_entities as entities
from app.services.event_entities import (
    EntityRow,
    load_universe,
    resolve,
    write_entities,
)
from app.services.wealthfolio import HoldingAsset, Holdings
from tests.test_events_ingest import NOW, _with_db


def _settings(**overrides) -> Settings:
    values = dict(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        demo_mode=False,
    )
    values.update(overrides)
    return Settings(**values)


def _event(title: str, hints=None) -> SimpleNamespace:
    return SimpleNamespace(title=title, entity_hints=hints or [])


def _universe(**overrides) -> Universe:
    values = dict(
        symbols=frozenset({"AAPL", "MSFT"}),
        refs={"AAPL": (("holding", "AAPL"),), "MSFT": (("watchlist", "1"),)},
        aliases={"apple": "AAPL"},
        macro_forecast_ids=(9,),
    )
    values.update(overrides)
    return Universe(**values)


def test_edgar_hint_nondescript_title_holding_relevant():
    rows = resolve(
        _event(
            "8-K — Apple Inc.",
            [("cik", "0000320193", "cik_map"), ("symbol", "AAPL", "cik_map")],
        ),
        _universe(),
    )
    symbol = next(row for row in rows if row.kind == "symbol")
    assert symbol.value == "AAPL"
    assert symbol.method == "cik_map"
    assert symbol.confidence == 1.0
    assert symbol.relevance == [["holding", "AAPL"]]


def test_ir_configured_hint():
    rows = resolve(
        _event("Quarterly results", [("symbol", "AAPL", "configured")]), _universe()
    )
    assert rows[0].method == "configured"
    assert rows[0].value == "AAPL"
    assert rows[0].relevance == [["holding", "AAPL"]]


def test_central_bank_pending_macro_only():
    rows = resolve(
        _event("Monetary policy decision", [("central_bank", "ECB", "configured")]),
        _universe(),
    )
    bank = next(row for row in rows if row.kind == "central_bank")
    assert bank.relevance == [["forecast", "9"]]
    empty = resolve(
        _event("Monetary policy decision", [("central_bank", "ECB", "configured")]),
        _universe(macro_forecast_ids=()),
    )
    assert next(row for row in empty if row.kind == "central_bank").relevance == []


def test_two_char_title_token_ignored():
    rows = resolve(
        _event("AA buys MSFT", []),
        Universe(
            symbols=frozenset({"AA", "MSFT"}), refs={"MSFT": (("holding", "MSFT"),)}
        ),
    )
    assert [row.value for row in rows] == ["MSFT"]


def test_alias_word_boundaries():
    rows = resolve(_event("pineapple harvest"), _universe())
    assert rows == []
    matched = resolve(_event("Apple reports earnings"), _universe())
    assert matched[0].method == "alias"
    assert matched[0].value == "AAPL"
    assert matched[0].confidence == 0.6


def test_higher_method_wins():
    rows = resolve(
        _event("$AAPL and AAPL", [("symbol", "AAPL", "configured")]),
        _universe(),
    )
    assert len(rows) == 1
    assert rows[0].method == "configured"
    assert rows[0].confidence == 1.0
    titled = resolve(_event("$AAPL and AAPL"), _universe())
    assert titled[0].method == "cashtag"
    assert titled[0].confidence == 0.9


def test_demo_assert_does_not_load(monkeypatch):
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("demo load")

    monkeypatch.setattr(entities, "make_wealthfolio", boom)
    with pytest.raises(AssertionError, match="demo"):
        asyncio.run(load_universe(SimpleNamespace(), _settings(demo_mode=True)))
    assert called["n"] == 0


def test_universe_sources_and_write_idempotency(monkeypatch):
    assets = [
        HoldingAsset(
            symbol="AAPL",
            name="Apple Inc",
            asset_class="EQUITY",
            quantity=Decimal("1"),
            value=Decimal("1"),
        )
    ]

    class _WF:
        async def holdings(self) -> Holdings:
            return Holdings(
                base="EUR",
                investments=Decimal("1"),
                cash=Decimal("0"),
                total=Decimal("1"),
                by_asset=assets,
            )

    monkeypatch.setattr(entities, "make_wealthfolio", lambda redis, settings: _WF())
    monkeypatch.setattr(entities, "get_redis", lambda: object())

    async def body(sessions):
        async with sessions() as session:
            session.add(WatchlistItem(symbol="MSFT"))
            session.add(
                JournalEntry(
                    entry_date=date(2026, 10, 1),
                    title="t",
                    view="v",
                    probability=0.5,
                    review_date=date(2026, 10, 10),
                    horizon_days=9,
                    subject="NVDA",
                    status="open",
                )
            )
            session.add(
                JournalEntry(
                    entry_date=date(2026, 10, 1),
                    title="old",
                    view="v",
                    probability=0.5,
                    review_date=date(2026, 10, 10),
                    horizon_days=9,
                    subject="TSLA",
                    status="reviewed",
                )
            )
            session.add(
                Note(
                    slug="thesis-aapl",
                    kind="thesis",
                    title="t",
                    status="draft",
                    one_sentence="x",
                    subjects=["META"],
                )
            )
            session.add(
                Note(
                    slug="research-x",
                    kind="research_note",
                    title="t",
                    status="draft",
                    one_sentence="x",
                    subjects=["UBER"],
                )
            )
            now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
            session.add(
                Forecast(
                    issued_by="human",
                    issuer_version="human",
                    subject_kind="symbol",
                    subject="AMD",
                    statement="AMD closes higher.",
                    kind="binary",
                    probability=0.6,
                    target={"metric": "forward_return"},
                    horizon_days=28,
                    decision_date=date(2026, 10, 5),
                    due_date=date(2026, 11, 2),
                    published_at=now,
                    first_seen_at=now,
                    available_at=now,
                    resolution_source="manual",
                    resolution_rule={"symbol": "AMD"},
                    evidence_refs=[],
                    status="pending",
                )
            )
            session.add(
                Forecast(
                    issued_by="human",
                    issuer_version="human",
                    subject_kind="macro",
                    subject="CPI",
                    statement="CPI prints lower.",
                    kind="binary",
                    probability=0.6,
                    target={"metric": "macro"},
                    horizon_days=28,
                    decision_date=date(2026, 10, 5),
                    due_date=date(2026, 11, 2),
                    published_at=now,
                    first_seen_at=now,
                    available_at=now,
                    resolution_source="manual",
                    resolution_rule={"subject": "CPI"},
                    evidence_refs=[],
                    status="pending",
                )
            )
            session.add(AlgoPosition(symbol="COIN", qty=1, avg_cost=1))
            session.add(
                AlgoStrategy(
                    name="s1",
                    status="active",
                    config={"signal_id": "x", "universe": ["PLTR"]},
                )
            )
            session.add(
                AlgoStrategy(
                    name="s0",
                    status="paused",
                    config={"signal_id": "x", "universe": ["INTC"]},
                )
            )
            await session.commit()
        async with sessions() as session:
            universe = await load_universe(session, _settings())
            assert universe.cik_map == {}
            assert universe.symbols == frozenset(
                {"AAPL", "MSFT", "NVDA", "META", "AMD", "COIN", "PLTR"}
            )
            assert "TSLA" not in universe.symbols
            assert "UBER" not in universe.symbols
            assert "INTC" not in universe.symbols
            assert universe.aliases["apple"] == "AAPL"
            assert universe.macro_forecast_ids
            watch_id = (
                await session.execute(
                    sa.select(WatchlistItem.id).where(WatchlistItem.symbol == "MSFT")
                )
            ).scalar_one()
            journal_id = (
                await session.execute(
                    sa.select(JournalEntry.id).where(JournalEntry.subject == "NVDA")
                )
            ).scalar_one()
            note_id = (
                await session.execute(
                    sa.select(Note.id).where(Note.slug == "thesis-aapl")
                )
            ).scalar_one()
            forecast_id = (
                await session.execute(
                    sa.select(Forecast.id).where(Forecast.subject == "AMD")
                )
            ).scalar_one()
            position_id = (
                await session.execute(
                    sa.select(AlgoPosition.id).where(AlgoPosition.symbol == "COIN")
                )
            ).scalar_one()
            strategy_id = (
                await session.execute(
                    sa.select(AlgoStrategy.id).where(AlgoStrategy.name == "s1")
                )
            ).scalar_one()
            assert universe.refs["AAPL"] == (("holding", "AAPL"),)
            assert universe.refs["MSFT"] == (("watchlist", str(watch_id)),)
            assert universe.refs["NVDA"] == (("journal", str(journal_id)),)
            assert universe.refs["META"] == (("note", str(note_id)),)
            assert universe.refs["AMD"] == (("forecast", str(forecast_id)),)
            assert universe.refs["COIN"] == (("algo_position", str(position_id)),)
            assert universe.refs["PLTR"] == (("algo_strategy", str(strategy_id)),)
        async with sessions() as session:
            event_id = (
                await session.execute(
                    sa.insert(MarketEvent.__table__)
                    .values(
                        source="rss",
                        feed="unit",
                        source_key="k1",
                        content_hash="a" * 64,
                        event_type="news",
                        title="t",
                        url="https://example.test",
                        published_at=NOW,
                        first_seen_at=NOW,
                        available_at=NOW,
                        raw={},
                    )
                    .returning(MarketEvent.id)
                )
            ).scalar_one()
            rows = [
                EntityRow(
                    kind="symbol",
                    value="AAPL",
                    method="configured",
                    confidence=1.0,
                    relevance=[["holding", "AAPL"]],
                    symbol="AAPL",
                )
            ]
            await write_entities(session, event_id, rows)
            await write_entities(session, event_id, rows)
            await session.commit()
        async with sessions() as session:
            stored = (await session.execute(sa.select(EventEntity))).scalars().all()
            assert len(stored) == 1
            assert stored[0].method == "configured"
            assert stored[0].value == "AAPL"

    _with_db(body)
