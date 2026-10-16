"""Phase 20 S9a event router tests; demo paths never open a session."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.routers.event_sources as sources_router
import app.routers.events as events_router
from app.config import Settings, get_settings
from app.errors import InvalidRequest, NotFound
from app.models import Alert, AlertDelivery, EventEntity, MarketEvent, SourceCursor
from app.services.events_ingest import story_key

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _settings(**kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


def _boom_session_scope():
    raise AssertionError("demo mode must never open a DB session")


def test_list_events_demo_empty_never_touches_db(monkeypatch):
    monkeypatch.setattr(events_router, "session_scope", _boom_session_scope)
    result = asyncio.run(events_router.list_events(settings=_settings(demo_mode=True)))
    assert result == {"items": [], "next_cursor": None}


def test_get_event_demo_not_found_never_touches_db(monkeypatch):
    monkeypatch.setattr(events_router, "session_scope", _boom_session_scope)
    with pytest.raises(NotFound):
        asyncio.run(
            events_router.get_event(event_id=1, settings=_settings(demo_mode=True))
        )


def test_list_event_sources_demo_zeroed_never_touches_db(monkeypatch):
    monkeypatch.setattr(sources_router, "session_scope", _boom_session_scope)
    result = asyncio.run(
        sources_router.list_event_sources(settings=_settings(demo_mode=True))
    )
    assert result["items"] == []
    assert result["summary"] == {
        "duplicate_rate": None,
        "alerts_fired_7d": 0,
        "alerts_acked_7d": 0,
        "ack_rate_7d": None,
        "alerts_suppressed_7d": 0,
        "suppress_reasons_7d": {},
        "alerts_dispositioned_7d": 0,
        "usefulness_rate_7d": None,
    }


def test_invalid_cursor_and_since_until():
    settings = _settings()

    def tok(payload) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    aware, naive = NOW.isoformat(), NOW.replace(tzinfo=None).isoformat()
    for cursor in (
        "not-a-cursor",
        tok([]),
        tok({}),
        tok([naive, 1]),
        tok([aware, 2**63]),
        tok([aware, 0]),
        tok([aware, -1]),
        tok([aware, True]),
        tok([aware, "1"]),
        tok([aware, 1, 2]),
    ):
        with pytest.raises(InvalidRequest):
            asyncio.run(
                events_router.list_events(cursor=cursor, limit=1, settings=settings)
            )
    with pytest.raises(InvalidRequest):
        asyncio.run(
            events_router.list_events(
                since=NOW, until=NOW - timedelta(days=1), limit=1, settings=settings
            )
        )

    class _Row:
        published_at = NOW
        id = 42

    token = events_router._encode(_Row())
    assert events_router._cursor(token) == (NOW, 42)


def _database() -> tuple[str, sa.URL, str]:
    current_url = get_settings().database_url
    name = f"event_s9a_{uuid4().hex}"
    return current_url, make_url(current_url).set(database=name), name


def _alembic(database_name: str) -> None:
    env = os.environ.copy()
    env["POSTGRES_DB"] = database_name
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0015"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)


def _with_db(monkeypatch, body):
    admin_url, temporary_url, database_name = _database()

    async def run():
        admin = create_async_engine(
            admin_url, poolclass=NullPool, connect_args={"timeout": 5}
        )
        temporary = create_async_engine(
            temporary_url, poolclass=NullPool, connect_args={"timeout": 5}
        )
        sessions = async_sessionmaker(temporary, expire_on_commit=False)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def scope():
            async with sessions() as session:
                yield session

        monkeypatch.setattr(events_router, "session_scope", scope)
        monkeypatch.setattr(sources_router, "session_scope", scope)
        try:
            async with admin.connect() as conn:
                autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, ProgrammingError (denied), …
            await admin.dispose()
            await temporary.dispose()
            pytest.skip(f"Postgres unreachable or CREATE DATABASE denied ({exc.__class__.__name__}): {exc}")
        try:
            _alembic(database_name)
            await body(sessions)
        finally:
            await temporary.dispose()
            async with admin.connect() as conn:
                autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
            await admin.dispose()

    asyncio.run(run())


def _event_row(**overrides) -> MarketEvent:
    title = overrides.get("title", "Alpha Bravo Charlie filing")
    values = dict(
        source="rss",
        feed="unit",
        source_key="k1",
        revision=1,
        content_hash="a" * 64,
        event_type="news",
        title=title,
        url="https://example.test/item",
        published_at=NOW,
        first_seen_at=NOW,
        available_at=NOW,
        story_key=story_key(title),
        raw={},
        entity_hints=[],
    )
    values.update(overrides)
    return MarketEvent(**values)


def test_contracts_revisions_pagination_filters_story_and_rates(monkeypatch):
    settings = _settings()

    async def body(sessions):
        title = "Alpha Bravo Charlie filing"
        async with sessions() as session:
            async with session.begin():
                rev1 = _event_row(
                    content_hash="1" * 64, title="Old Alpha Bravo Charlie"
                )
                session.add(rev1)
                await session.flush()
                rev2 = _event_row(
                    revision=2,
                    supersedes_id=rev1.id,
                    content_hash="2" * 64,
                    title=title,
                    published_at=NOW + timedelta(minutes=1),
                    first_seen_at=NOW + timedelta(minutes=1),
                    available_at=NOW + timedelta(minutes=1),
                )
                other = _event_row(
                    source_key="k2",
                    content_hash="3" * 64,
                    title="Delta Echo Foxtrot item",
                    event_type="filing",
                    published_at=NOW - timedelta(minutes=1),
                    first_seen_at=NOW - timedelta(minutes=1),
                    available_at=NOW - timedelta(minutes=1),
                )
                session.add_all([rev2, other])
                await session.flush()
                session.add(
                    EventEntity(
                        event_id=rev2.id,
                        kind="symbol",
                        value="AAPL",
                        symbol="AAPL",
                        method="title",
                        confidence=0.9,
                        relevance=[{"holding": True}],
                    )
                )
                session.add(
                    EventEntity(
                        event_id=other.id,
                        kind="symbol",
                        value="MSFT",
                        symbol="MSFT",
                        method="title",
                        confidence=0.4,
                        relevance=[],
                    )
                )
                session.add(
                    SourceCursor(
                        source="rss",
                        feed="unit",
                        events_seen=10,
                        events_new=6,
                        events_duplicate=4,
                    )
                )
                alert_u = Alert(
                    event_id=rev2.id,
                    rule="r1",
                    score=1,
                    components={},
                    why={},
                    cooldown_key="c1",
                    mode="live",
                    disposition="useful",
                    acked_at=NOW,
                    fired_at=NOW,
                )
                alert_f = Alert(
                    event_id=other.id,
                    rule="r2",
                    score=1,
                    components={},
                    why={},
                    cooldown_key="c2",
                    mode="live",
                    disposition="false_positive",
                    fired_at=NOW,
                )
                session.add_all([alert_u, alert_f])
                await session.flush()
                session.add_all(
                    [
                        AlertDelivery(
                            event_id=rev2.id,
                            rule="r1",
                            channel="audit",
                            action="suppress",
                            attempted_at=NOW,
                            ok=True,
                            detail={"reason": "cooldown"},
                        ),
                        AlertDelivery(
                            event_id=other.id,
                            rule="r2",
                            channel="audit",
                            action="suppress",
                            attempted_at=NOW,
                            ok=True,
                            detail={"reason": "dup"},
                        ),
                        AlertDelivery(
                            event_id=other.id,
                            rule="old",
                            channel="audit",
                            action="suppress",
                            attempted_at=NOW - timedelta(days=8),
                            ok=True,
                            detail={"reason": "old"},
                        ),
                    ]
                )
                rev1_id, rev2_id, other_id = rev1.id, rev2.id, other.id

        latest = await events_router.list_events(limit=50, settings=settings)
        ids = [item["id"] for item in latest["items"]]
        assert ids == [rev2_id, other_id]
        assert rev1_id not in ids
        assert latest["items"][0]["revision"] == 2
        assert latest["items"][0]["story_key"] == story_key(title)
        assert latest["items"][0]["entities"][0]["symbol"] == "AAPL"
        keys = {
            "id",
            "source",
            "feed",
            "event_type",
            "form_type",
            "title",
            "summary",
            "url",
            "published_at",
            "published_at_estimated",
            "first_seen_at",
            "available_at",
            "revision",
            "supersedes_id",
            "story_key",
            "entities",
        }
        assert keys <= set(latest["items"][0])

        all_rev = await events_router.list_events(
            limit=50, all_revisions=True, settings=settings
        )
        assert {item["id"] for item in all_rev["items"]} == {rev1_id, rev2_id, other_id}

        page1 = await events_router.list_events(limit=1, settings=settings)
        assert len(page1["items"]) == 1 and page1["next_cursor"]
        position = events_router._cursor(page1["next_cursor"])
        assert position == (page1["items"][0]["published_at"], page1["items"][0]["id"])
        page2 = await events_router.list_events(
            limit=1, cursor=page1["next_cursor"], settings=settings
        )
        assert page2["items"][0]["id"] != page1["items"][0]["id"]
        assert {page1["items"][0]["id"], page2["items"][0]["id"]} == {rev2_id, other_id}

        filings = await events_router.list_events(
            event_type="filing", limit=50, settings=settings
        )
        assert [item["id"] for item in filings["items"]] == [other_id]
        by_symbol = await events_router.list_events(
            symbol="aapl", limit=50, settings=settings
        )
        assert [item["id"] for item in by_symbol["items"]] == [rev2_id]
        relevant = await events_router.list_events(
            relevant_only=True, limit=50, settings=settings
        )
        assert [item["id"] for item in relevant["items"]] == [rev2_id]

        detail = await events_router.get_event(event_id=rev2_id, settings=settings)
        assert [row["revision"] for row in detail["revisions"]] == [1, 2]
        with pytest.raises(NotFound):
            await events_router.get_event(event_id=999999, settings=settings)

        sources = await sources_router.list_event_sources(settings=settings)
        item = sources["items"][0]
        assert item["duplicate_rate"] == 0.4
        assert sources["summary"]["duplicate_rate"] == 0.4
        assert sources["summary"]["alerts_fired_7d"] == 2
        assert sources["summary"]["alerts_acked_7d"] == 1
        assert sources["summary"]["ack_rate_7d"] == 0.5
        assert sources["summary"]["alerts_dispositioned_7d"] == 2
        assert sources["summary"]["usefulness_rate_7d"] == 0.5
        assert sources["summary"]["alerts_suppressed_7d"] == 2
        assert sources["summary"]["suppress_reasons_7d"] == {"cooldown": 1, "dup": 1}

    _with_db(monkeypatch, body)
