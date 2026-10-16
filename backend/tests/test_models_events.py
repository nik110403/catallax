"""Phase 20/config tests using a disposable PostgreSQL database."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.models import Alert, AlertDelivery, EventEntity, MarketEvent, SourceCursor


def _settings(**overrides) -> Settings:
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **overrides,
    )


def test_event_settings_defaults_and_sec_rate_limit():
    settings = _settings()
    assert settings.events_enabled is False
    assert settings.events_tick_interval_seconds == 900
    assert settings.sec_edgar_rate_limit_per_sec == 5
    assert settings.alerts_ntfy_token.get_secret_value() == ""
    with pytest.raises(ValidationError):
        _settings(sec_edgar_rate_limit_per_sec=11)


def test_event_model_shape():
    assert MarketEvent.__table__.c.content_hash.unique is None
    assert {"ck_market_events_revision_positive", "ck_market_events_pit_order"} <= {
        c.name for c in MarketEvent.__table__.constraints
    }
    assert "ck_event_entities_confidence_range" in {
        c.name for c in EventEntity.__table__.constraints
    }
    assert "ck_alert_deliveries_alert_or_suppress" in {
        c.name for c in AlertDelivery.__table__.constraints
    }
    assert list(SourceCursor.__table__.primary_key.columns.keys()) == ["source", "feed"]


def _database_urls_or_skip() -> tuple[str, sa.URL, str]:
    try:
        current_url = get_settings().database_url
    except ValidationError as exc:
        pytest.skip(f"Postgres settings unavailable: {exc.errors()[0]['loc'][0]}")
    temporary_name = f"event_s1_{uuid4().hex}"
    temporary_url = make_url(current_url).set(database=temporary_name)
    return current_url, temporary_url, temporary_name


def _alembic(database_name: str, revision: str) -> None:
    env = os.environ.copy()
    env["POSTGRES_DB"] = database_name
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *revision.split()],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"alembic {' '.join(revision.split())} failed")


def test_0015_alembic_roundtrip_and_constraints():
    admin_url, temporary_url, database_name = _database_urls_or_skip()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    async def run():
        admin = create_async_engine(
            admin_url, poolclass=NullPool, connect_args={"timeout": 3}
        )
        temporary = create_async_engine(
            temporary_url, poolclass=NullPool, connect_args={"timeout": 3}
        )
        try:
            async with admin.connect() as conn:
                autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, ProgrammingError (denied), …
            await admin.dispose()
            await temporary.dispose()
            pytest.skip(f"Postgres unreachable or CREATE DATABASE denied ({exc.__class__.__name__}): {exc}")
        try:
            _alembic(database_name, "upgrade 0015")
            _alembic(database_name, "downgrade 0014")
            _alembic(database_name, "upgrade 0015")

            base = dict(
                source="test",
                feed="feed",
                source_key="key",
                content_hash="a" * 64,
                event_type="filing",
                title="test",
                url="https://example.test",
                published_at=now,
                first_seen_at=now,
                available_at=now,
                raw={},
            )
            async with temporary.begin() as conn:
                event_id = (
                    await conn.execute(
                        sa.insert(MarketEvent.__table__)
                        .values(**base)
                        .returning(MarketEvent.id)
                    )
                ).scalar_one()
                await conn.execute(
                    sa.insert(MarketEvent.__table__).values({**base, "revision": 2})
                )
                alert_id = (
                    await conn.execute(
                        sa.insert(Alert.__table__)
                        .values(
                            event_id=event_id,
                            rule="r",
                            score=1,
                            components={},
                            why={},
                            cooldown_key="key",
                            mode="live",
                            fired_at=now,
                        )
                        .returning(Alert.id)
                    )
                ).scalar_one()
                await conn.execute(
                    sa.insert(AlertDelivery.__table__).values(
                        event_id=event_id,
                        rule="r",
                        channel="audit",
                        action="suppress",
                        ok=True,
                    )
                )
                await conn.execute(
                    sa.insert(AlertDelivery.__table__).values(
                        alert_id=alert_id,
                        event_id=event_id,
                        rule="r",
                        channel="ntfy",
                        action="send",
                        ok=True,
                    )
                )

            async def rejected(statement, constraint: str):
                async with temporary.begin() as conn:
                    with pytest.raises(IntegrityError) as caught:
                        await conn.execute(statement)
                assert constraint in str(caught.value.orig)

            await rejected(
                sa.insert(MarketEvent.__table__).values(
                    {
                        **base,
                        "source_key": "pit",
                        "available_at": now - timedelta(seconds=1),
                    }
                ),
                "ck_market_events_pit_order",
            )
            await rejected(
                sa.insert(MarketEvent.__table__).values(
                    {**base, "source_key": "revision", "revision": 0}
                ),
                "ck_market_events_revision_positive",
            )
            await rejected(
                sa.insert(EventEntity.__table__).values(
                    event_id=event_id,
                    kind="symbol",
                    value="T",
                    method="test",
                    confidence=1.1,
                ),
                "ck_event_entities_confidence_range",
            )
            await rejected(
                sa.insert(Alert.__table__).values(
                    event_id=event_id,
                    rule="bad-disposition",
                    score=1,
                    components={},
                    why={},
                    cooldown_key="bad",
                    mode="live",
                    disposition="bad",
                    fired_at=now,
                ),
                "ck_alerts_disposition",
            )
            await rejected(
                sa.insert(AlertDelivery.__table__).values(
                    event_id=event_id, rule="r", channel="ntfy", action="send", ok=False
                ),
                "ck_alert_deliveries_alert_or_suppress",
            )
        finally:
            await temporary.dispose()
            async with admin.connect() as conn:
                autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(
                    sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
            await admin.dispose()

    asyncio.run(run())
