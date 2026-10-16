"""Phase 20 ingest tests against isolated throwaway PostgreSQL databases."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.events import factory as factory_mod
from app.adapters.events.base import PollResult, RawEvent, SourceAdapter, Universe
from app.adapters.events.factory import build_source_adapters
from app.config import Settings, get_settings
from app.models import MarketEvent, SourceCursor
from app.services import events_ingest as ingest_mod
from app.services.events_ingest import ingest, story_key

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class FakeAdapter(SourceAdapter):
    source = "rss"
    feed = "unit"
    rate_limit_per_min = 10

    def __init__(self, events=None, error=None, delay=0.0, source="rss", feed="unit"):
        self.source, self.feed = source, feed
        self.events, self.error, self.delay = events or [], error, delay

    async def poll(self, cursor, etag, last_modified) -> PollResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return PollResult(
            events=self.events, cursor={"n": 1}, etag="etag", last_modified="lm"
        )


def _event(**overrides) -> RawEvent:
    base = dict(
        source="rss",
        feed="unit",
        source_key="k1",
        event_type="news",
        form_type=None,
        title="Alpha Bravo Charlie filing",
        summary="body",
        url="https://example.test/item",
        published_at=NOW,
        first_seen_at=NOW,
        raw={"n": 1},
        entity_hints=[],
    )
    base.update(overrides)
    return RawEvent(**base)


def _database() -> tuple[str, sa.URL, str]:
    current_url = get_settings().database_url
    name = f"event_s2_{uuid4().hex}"
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


def _with_db(body):
    admin_url, temporary_url, database_name = _database()

    async def run():
        admin = create_async_engine(
            admin_url, poolclass=NullPool, connect_args={"timeout": 5}
        )
        temporary = create_async_engine(
            temporary_url, poolclass=NullPool, connect_args={"timeout": 5}
        )
        sessions = async_sessionmaker(temporary, expire_on_commit=False)
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


def _disabled_event_settings(**overrides):
    values = dict(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        news_feeds="",
        events_cb_feeds="",
        events_ir_feeds="",
        sec_edgar_user_agent="",
    )
    values.update(overrides)
    return Settings(**values)


def test_factory_empty_feeds_and_sec_ua_build_no_adapters(monkeypatch):
    def no_http(*_args, **_kwargs):
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", no_http)
    assert build_source_adapters(_disabled_event_settings(), None, Universe()) == []
    assert (
        build_source_adapters(
            _disabled_event_settings(), None, Universe(symbols=frozenset({"AAPL"}))
        )
        == []
    )


def test_factory_skips_missing_source_modules(monkeypatch):
    def missing(name, package=None):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(factory_mod, "import_module", missing)
    assert (
        build_source_adapters(
            _disabled_event_settings(news_feeds="https://feeds.test/markets.xml"),
            None,
            Universe(),
        )
        == []
    )

    def nested(_name, package=None):
        raise ModuleNotFoundError("No module named 'feedparser'", name="feedparser")

    monkeypatch.setattr(factory_mod, "import_module", nested)
    with pytest.raises(ModuleNotFoundError) as caught:
        build_source_adapters(_disabled_event_settings(), None, Universe())
    assert caught.value.name == "feedparser"


def test_story_key_permutation_and_short_title():
    a = story_key("Charlie the Alpha Bravo")
    b = story_key("Bravo Alpha Charlie")
    assert a and a == b and len(a) == 40
    assert story_key("Alpha Bravo") is None
    assert story_key("to be") is None


def test_repeat_duplicate_and_aba_revisions():
    async def body(sessions):
        async with sessions() as session:
            first = await ingest(session, FakeAdapter([_event()]), NOW)
            assert (first.seen, first.new, first.duplicate) == (1, 1, 0)
        async with sessions() as session:
            again = await ingest(
                session, FakeAdapter([_event()]), NOW + timedelta(seconds=1)
            )
            assert (again.new, again.duplicate, again.revised) == (0, 1, 0)
        async with sessions() as session:
            revised = await ingest(
                session,
                FakeAdapter([_event(title="Alpha Bravo Charlie amended")]),
                NOW + timedelta(seconds=2),
            )
            assert (revised.revised, revised.new) == (1, 0)
        async with sessions() as session:
            aba = await ingest(
                session, FakeAdapter([_event()]), NOW + timedelta(seconds=3)
            )
            assert aba.revised == 1
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(MarketEvent).order_by(MarketEvent.revision)
                    )
                )
                .scalars()
                .all()
            )
            assert [row.revision for row in rows] == [1, 2, 3]
            assert rows[2].supersedes_id == rows[1].id
            assert rows[0].content_hash == rows[2].content_hash != rows[1].content_hash

    _with_db(body)


def test_long_link_hashed_key_url_retained():
    url = "https://example.test/" + "a" * (1000 - len("https://example.test/"))
    assert len(url) == 1000
    key = "k" * 300

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(
                session, FakeAdapter([_event(source_key=key, url=url)]), NOW
            )
            assert stats.new == 1
        async with sessions() as session:
            row = (await session.execute(sa.select(MarketEvent))).scalar_one()
            assert row.url == url
            assert (
                row.source_key == "sha256:" + hashlib.sha256(key.encode()).hexdigest()
            )

    _with_db(body)


def test_poison_among_good_rejects_one_and_commits_cursor():
    bad = _event(source_key="poison", event_type="x" * 40)
    good_a = _event(source_key="ok-a", title="Good Alpha Bravo Charlie")
    good_b = _event(source_key="ok-b", title="Other Alpha Bravo Charlie")

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(session, FakeAdapter([good_a, bad, good_b]), NOW)
            assert (stats.new, stats.rejected, stats.seen) == (2, 1, 3)
        async with sessions() as session:
            keys = set(
                (await session.execute(sa.select(MarketEvent.source_key))).scalars()
            )
            cursor = await session.get(SourceCursor, {"source": "rss", "feed": "unit"})
            assert keys == {"ok-a", "ok-b"}
            assert cursor.last_success_at == NOW
            assert cursor.events_rejected == 1
            assert cursor.consecutive_failures == 0

    _with_db(body)


def test_future_published_and_first_seen_after_now():
    future = NOW + timedelta(hours=2)

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(
                session,
                FakeAdapter(
                    [
                        _event(
                            source_key="future",
                            published_at=future,
                            first_seen_at=NOW,
                            raw={"orig": True},
                        ),
                        _event(
                            source_key="late-seen",
                            published_at=NOW,
                            first_seen_at=future,
                        ),
                        _event(source_key="missing-pub", published_at=None),
                    ]
                ),
                NOW,
            )
            assert stats.new == 3
        async with sessions() as session:
            rows = {
                row.source_key: row
                for row in (await session.execute(sa.select(MarketEvent))).scalars()
            }
            future_row, late, missing = (
                rows["future"],
                rows["late-seen"],
                rows["missing-pub"],
            )
            assert future_row.published_at == NOW
            assert future_row.published_at_estimated is True
            assert future_row.raw["published_upstream"] == future.isoformat()
            assert late.first_seen_at == future
            assert late.available_at >= late.first_seen_at
            assert missing.published_at == NOW
            assert missing.published_at_estimated is True

    _with_db(body)


def test_hints_persisted():
    hints = [("symbol", "AAPL", "title")]

    async def body(sessions):
        async with sessions() as session:
            await ingest(session, FakeAdapter([_event(entity_hints=hints)]), NOW)
        async with sessions() as session:
            row = (await session.execute(sa.select(MarketEvent))).scalar_one()
            assert row.entity_hints == [["symbol", "AAPL", "title"]]
            assert row.story_key == story_key("Alpha Bravo Charlie filing")

    _with_db(body)


def test_timeout_error_backoff_dead_letter_unrelated_adapter(monkeypatch):
    live = get_settings()
    monkeypatch.setattr(
        ingest_mod,
        "get_settings",
        lambda: live.model_copy(
            update={
                "events_adapter_timeout_seconds": 1,
                "events_dead_letter_after": 2,
            }
        ),
    )

    async def body(sessions):
        failing = FakeAdapter(delay=3.0, source="rss", feed="slow")
        other = FakeAdapter(
            [_event(source="sec", feed="filings")], source="sec", feed="filings"
        )
        now = NOW
        async with sessions() as session:
            await ingest(session, failing, now)
        async with sessions() as session:
            await ingest(session, other, now)
            slow = await session.get(SourceCursor, {"source": "rss", "feed": "slow"})
            ok = await session.get(SourceCursor, {"source": "sec", "feed": "filings"})
            assert slow.consecutive_failures == 1
            assert slow.next_attempt_at > now
            assert slow.last_error
            assert ok.consecutive_failures == 0
            assert ok.events_new == 1
            later = slow.next_attempt_at
        async with sessions() as session:
            skipped = await ingest(session, failing, now + timedelta(seconds=1))
            assert skipped.seen == 0
        async with sessions() as session:
            await ingest(
                session,
                FakeAdapter(error=RuntimeError("boom"), source="rss", feed="slow"),
                later,
            )
            dead = await session.get(SourceCursor, {"source": "rss", "feed": "slow"})
            assert dead.consecutive_failures == 2
            assert dead.dead_letter_count == 1
            dead_next = dead.next_attempt_at
        async with sessions() as session:
            await ingest(
                session,
                FakeAdapter(error=RuntimeError("boom"), source="rss", feed="slow"),
                dead_next,
            )
            again = await session.get(SourceCursor, {"source": "rss", "feed": "slow"})
            assert again.dead_letter_count == 1
            assert again.consecutive_failures == 3

    _with_db(body)


def test_surrogate_key_poison_rejects_and_commits_good(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.events_ingest")
    good = _event(source_key="ok-good", title="Good Alpha Bravo Charlie")
    poison = _event(source_key="x" * 256 + "\ud800", title="Poison Alpha Bravo Charlie")

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(session, FakeAdapter([good, poison]), NOW)
            assert (stats.new, stats.rejected, stats.seen) == (1, 1, 1)
        async with sessions() as session:
            rows = (await session.execute(sa.select(MarketEvent))).scalars().all()
            cursor = await session.get(SourceCursor, {"source": "rss", "feed": "unit"})
            assert [row.source_key for row in rows] == ["ok-good"]
            assert cursor.last_success_at == NOW
            assert cursor.consecutive_failures == 0
            assert cursor.events_rejected == 1
            assert cursor.events_new == 1
        for record in caplog.records:
            record.getMessage().encode("utf-8")
        assert "\ud800" not in caplog.text

    _with_db(body)


def test_first_failure_dead_letter_threshold_one(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.services.events_ingest")
    live = get_settings()
    monkeypatch.setattr(
        ingest_mod,
        "get_settings",
        lambda: live.model_copy(update={"events_dead_letter_after": 1}),
    )

    async def body(sessions):
        failing = FakeAdapter(error=RuntimeError("boom"), source="rss", feed="once")
        async with sessions() as session:
            await ingest(session, failing, NOW)
        async with sessions() as session:
            row = await session.get(SourceCursor, {"source": "rss", "feed": "once"})
            assert row.consecutive_failures == 1
            assert row.dead_letter_count == 1
            assert row.last_failure_at == NOW
            assert row.last_error == "RuntimeError:poll"
            later = row.next_attempt_at
        letters = [r for r in caplog.records if "dead-letter" in r.getMessage()]
        assert len(letters) == 1
        caplog.clear()
        async with sessions() as session:
            await ingest(session, failing, later)
        async with sessions() as session:
            again = await session.get(SourceCursor, {"source": "rss", "feed": "once"})
            assert again.dead_letter_count == 1
            assert again.consecutive_failures == 2
        assert not any("dead-letter" in r.getMessage() for r in caplog.records)

    _with_db(body)


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": None},
        {"url": None},
        {"summary": 1},
        {"event_type": None},
        {"form_type": 1},
        {"source_key": None},
        {"first_seen_at": datetime(2026, 10, 5, 12, 0)},
        {"published_at": datetime(2026, 10, 5, 12, 0)},
    ],
    ids=[
        "titleNone",
        "urlNone",
        "summaryInt",
        "typeNone",
        "formInt",
        "keyNone",
        "seenNaive",
        "pubNaive",
    ],
)
def test_malformed_field_rejects_poison_and_persists_good(overrides):
    good = _event(source_key="ok-good", title="Good Alpha Bravo Charlie")
    poison = _event(**{"source_key": "poison", **overrides})

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(session, FakeAdapter([good, poison]), NOW)
            assert (stats.new, stats.rejected, stats.seen) == (1, 1, 1)
        async with sessions() as session:
            rows = (await session.execute(sa.select(MarketEvent))).scalars().all()
            cursor = await session.get(SourceCursor, {"source": "rss", "feed": "unit"})
            assert [row.source_key for row in rows] == ["ok-good"]
            assert cursor.last_success_at == NOW
            assert cursor.last_error is None
            assert cursor.consecutive_failures == 0
            assert cursor.events_new == 1
            assert cursor.events_rejected == 1

    _with_db(body)


def test_nullable_summary_form_published_persist():
    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(
                session,
                FakeAdapter(
                    [
                        _event(source_key="ok-good", title="Good Alpha Bravo Charlie"),
                        _event(
                            source_key="nullable",
                            summary=None,
                            form_type=None,
                            published_at=None,
                        ),
                    ]
                ),
                NOW,
            )
            assert (stats.new, stats.rejected) == (2, 0)
        async with sessions() as session:
            rows = {
                row.source_key: row
                for row in (await session.execute(sa.select(MarketEvent))).scalars()
            }
            assert rows["ok-good"].summary == "body"
            assert rows["nullable"].summary is None
            assert rows["nullable"].form_type is None
            assert rows["nullable"].published_at == NOW
            assert rows["nullable"].published_at_estimated is True

    _with_db(body)


def test_sanitized_errors_omit_secrets(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.events_ingest")
    secret = "https://user:hunter2@example.test/item?token=s3cret"

    async def body(sessions):
        async with sessions() as session:
            stats = await ingest(
                session,
                FakeAdapter(
                    [
                        _event(
                            source_key="poison",
                            event_type="x" * 40,
                            url=secret,
                            title=secret,
                        )
                    ]
                ),
                NOW,
            )
            assert stats.rejected == 1
        async with sessions() as session:
            await ingest(
                session,
                FakeAdapter(error=RuntimeError(secret), source="rss", feed="leaky"),
                NOW,
            )
            cursor = await session.get(SourceCursor, {"source": "rss", "feed": "leaky"})
            assert cursor.last_error == "RuntimeError:poll"
            assert "hunter2" not in cursor.last_error
            assert "s3cret" not in cursor.last_error
            assert secret not in cursor.last_error
        text = caplog.text
        assert "hunter2" not in text
        assert "s3cret" not in text
        assert "token=" not in text
        text.encode("utf-8")

    _with_db(body)
