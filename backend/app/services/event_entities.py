from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.events.base import Universe
from app.models import (
    AlgoPosition,
    AlgoStrategy,
    EventEntity,
    Forecast,
    JournalEntry,
    Note,
    WatchlistItem,
)
from app.redis import get_redis
from app.services.wealthfolio import make_wealthfolio

_WORD = re.compile(r"[A-Za-z]+")
_CASHTAG = re.compile(r"\$([A-Z]{3,})\b")
_TICKER = re.compile(r"\b([A-Z]{3,})\b")


@dataclass(frozen=True)
class EntityRow:
    kind: str
    value: str
    method: str
    confidence: float
    relevance: list[Any]
    symbol: str | None = None


def _symbol_relevance(symbol: str, universe: Universe) -> list[list[str]]:
    refs = universe.refs.get(symbol.upper(), ())
    return [[kind, ref_id] for kind, ref_id in refs]


def _hint_relevance(kind: str, value: str, universe: Universe) -> list[list[str]]:
    if kind == "symbol":
        return _symbol_relevance(value, universe)
    if kind == "central_bank":
        return [["forecast", str(fid)] for fid in universe.macro_forecast_ids]
    return []


async def load_universe(session: AsyncSession, settings: Any) -> Universe:
    assert not settings.demo_mode, "caller never loads demo"
    symbols: set[str] = set()
    refs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    aliases: dict[str, str] = {}

    holdings = await make_wealthfolio(get_redis(), settings).holdings()
    for asset in holdings.by_asset:
        token = asset.symbol.upper()
        symbols.add(token)
        refs[token].append(("holding", token))
        for word in _WORD.findall((asset.name or "").lower()):
            if len(word) >= 4 and word not in aliases:
                aliases[word] = token

    watch_rows = (await session.execute(select(WatchlistItem))).scalars().all()
    for row in watch_rows:
        token = row.symbol.upper()
        symbols.add(token)
        refs[token].append(("watchlist", str(row.id)))

    journal_rows = (
        (
            await session.execute(
                select(JournalEntry).where(JournalEntry.status == "open")
            )
        )
        .scalars()
        .all()
    )
    for row in journal_rows:
        token = row.subject.upper()
        symbols.add(token)
        refs[token].append(("journal", str(row.id)))

    note_rows = (
        (await session.execute(select(Note).where(Note.kind == "thesis")))
        .scalars()
        .all()
    )
    for row in note_rows:
        for subject in row.subjects or []:
            token = str(subject).upper()
            symbols.add(token)
            refs[token].append(("note", str(row.id)))

    forecast_rows = (
        (await session.execute(select(Forecast).where(Forecast.status == "pending")))
        .scalars()
        .all()
    )
    macro_ids: list[int] = []
    for row in forecast_rows:
        if row.subject_kind == "macro":
            macro_ids.append(row.id)
        elif row.subject_kind == "symbol":
            token = row.subject.upper()
            symbols.add(token)
            refs[token].append(("forecast", str(row.id)))

    position_rows = (await session.execute(select(AlgoPosition))).scalars().all()
    for row in position_rows:
        token = row.symbol.upper()
        symbols.add(token)
        refs[token].append(("algo_position", str(row.id)))

    strategy_rows = (
        (
            await session.execute(
                select(AlgoStrategy).where(AlgoStrategy.status == "active")
            )
        )
        .scalars()
        .all()
    )
    for row in strategy_rows:
        for item in (row.config or {}).get("universe") or []:
            token = str(item).upper()
            symbols.add(token)
            refs[token].append(("algo_strategy", str(row.id)))

    return Universe(
        symbols=frozenset(symbols),
        cik_map={},
        refs={key: tuple(value) for key, value in refs.items()},
        aliases=aliases,
        macro_forecast_ids=tuple(macro_ids),
    )


def resolve(event_row: Any, universe: Universe) -> list[EntityRow]:
    title = getattr(event_row, "title", "") or ""
    hints = getattr(event_row, "entity_hints", None) or []
    chosen: dict[tuple[str, str], EntityRow] = {}

    def consider(row: EntityRow) -> None:
        key = (row.kind, row.value)
        previous = chosen.get(key)
        if previous is None or row.confidence > previous.confidence:
            chosen[key] = row

    for hint in hints:
        kind, value, method = hint[0], hint[1], hint[2]
        token = value.upper() if kind == "symbol" else value
        consider(
            EntityRow(
                kind=kind,
                value=token if kind == "symbol" else str(value),
                method=str(method),
                confidence=1.0,
                relevance=_hint_relevance(
                    kind, token if kind == "symbol" else str(value), universe
                ),
                symbol=token if kind == "symbol" else None,
            )
        )

    for match in _CASHTAG.findall(title):
        if match in universe.symbols:
            consider(
                EntityRow(
                    kind="symbol",
                    value=match,
                    method="cashtag",
                    confidence=0.9,
                    relevance=_symbol_relevance(match, universe),
                    symbol=match,
                )
            )

    for match in _TICKER.findall(title):
        if match in universe.symbols:
            consider(
                EntityRow(
                    kind="symbol",
                    value=match,
                    method="title",
                    confidence=0.7,
                    relevance=_symbol_relevance(match, universe),
                    symbol=match,
                )
            )

    lowered = title.lower()
    for word, symbol in universe.aliases.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            consider(
                EntityRow(
                    kind="symbol",
                    value=symbol,
                    method="alias",
                    confidence=0.6,
                    relevance=_symbol_relevance(symbol, universe),
                    symbol=symbol,
                )
            )

    return sorted(
        chosen.values(), key=lambda row: (-row.confidence, row.kind, row.value)
    )


async def write_entities(
    session: AsyncSession, event_id: int, rows: list[EntityRow]
) -> None:
    if not rows:
        return
    stmt = insert(EventEntity).values(
        [
            {
                "event_id": event_id,
                "kind": row.kind,
                "value": row.value,
                "symbol": row.symbol,
                "method": row.method,
                "confidence": row.confidence,
                "relevance": row.relevance,
            }
            for row in rows
        ]
    )
    stmt = stmt.on_conflict_do_nothing(constraint="uq_event_entities_event_kind_value")
    await session.execute(stmt)
