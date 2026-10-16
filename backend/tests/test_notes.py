"""Tests for the notes / thesis service + router.

Validation that never touches the DB (kind/title/one_sentence blank checks,
PATCH's unknown-field guard) runs first inside `NotesService.create`/
`update`, before `session_scope()` is ever opened — mirrors
`test_journal.py`'s split. Everything that needs a real row (slug
collisions, evidence-ref resolution/hydration, the signed-note 409) needs a
REAL Postgres with migration 0014 applied, using the same "connect or skip"
`_with_real_db` convention `test_journal.py` established (single
`asyncio.run()` owns the probe, the session use and the dispose, to sidestep
the asyncpg/SQLAlchemy event-loop-affinity bug documented there). Router
tests exercise the DEMO_MODE seam directly (async handlers, no DB/service
construction under demo), mirroring `test_forecasts_router.py`/
`test_journal.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routers.notes as notes_router
import app.services.notes as notes_module
from app.config import Settings
from app.errors import (
    DemoModeReadOnly,
    InvalidEvidenceRef,
    InvalidRequest,
    NotFound,
    register_exception_handlers,
)
from app.models import JournalEntry, Note, ReportPack
from app.services.notes import NotesService


def _settings(**kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


def _payload(**kw):
    values = dict(
        kind="research_note", title="Test note title", one_sentence="This is the claim."
    )
    values.update(kw)
    return values


# ── validation — no DB required ─────────────────────────────────────────
def test_kind_must_be_research_note_or_thesis():
    svc = NotesService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(kind="bogus"))

    asyncio.run(_run())


def test_blank_title_rejected():
    svc = NotesService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(title="   "))

    asyncio.run(_run())


def test_blank_one_sentence_rejected():
    svc = NotesService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(one_sentence="   "))

    asyncio.run(_run())


def test_update_rejects_unknown_fields():
    svc = NotesService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.update("some-slug", {"status": "signed"})

    asyncio.run(_run())


# ── slug/evidence/sign — real Postgres, skip if unreachable ────────────
def _with_real_db(monkeypatch, body):
    """See `test_journal.py`'s `_with_real_db` for the full rationale on why
    the probe, the session use and the engine dispose all run on ONE
    `asyncio.run()`."""
    settings = _settings()

    async def _runner():
        engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, …
            await engine.dispose()
            pytest.skip(
                f"Postgres unreachable from host ({exc.__class__.__name__}): {exc}"
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        @contextlib.asynccontextmanager
        async def _fake_session_scope():
            async with session_factory() as session:
                yield session

        monkeypatch.setattr(notes_module, "session_scope", _fake_session_scope)
        try:
            await body(settings)
        finally:
            await engine.dispose()

    asyncio.run(_runner())


def test_slug_collision_suffixes(monkeypatch):
    stamp = int(time.time() * 1000)
    title = f"Collision Title {stamp}"

    async def _body(settings):
        svc = NotesService(redis=None, settings=settings)
        first = await svc.create(_payload(title=title))
        second = await svc.create(_payload(title=title))
        third = await svc.create(_payload(title=title))
        base = first["note"]["slug"]
        assert second["note"]["slug"] == f"{base}-2"
        assert third["note"]["slug"] == f"{base}-3"

    _with_real_db(monkeypatch, _body)


def test_unresolvable_evidence_ref_rejected_naming_it(monkeypatch):
    stamp = int(time.time() * 1000)

    async def _body(settings):
        svc = NotesService(redis=None, settings=settings)
        with pytest.raises(InvalidEvidenceRef) as exc_info:
            await svc.create(
                _payload(
                    title=f"Bad ref {stamp}",
                    evidence_refs=[{"kind": "forecast", "id": 999_999_999}],
                )
            )
        assert "forecast" in str(exc_info.value)
        assert "999999999" in str(exc_info.value)

    _with_real_db(monkeypatch, _body)


@pytest.mark.parametrize(
    "method,path,body",
    [
        (
            "post",
            "/api/v1/notes/",
            _payload(evidence_refs=[{"kind": "forecast", "id": 999999}]),
        ),
        (
            "patch",
            "/api/v1/notes/existing",
            {"evidence_refs": [{"kind": "forecast", "id": 999999}]},
        ),
    ],
)
def test_evidence_ref_api_returns_422_envelope_without_db(
    monkeypatch, method, path, body
):
    @contextlib.asynccontextmanager
    async def fake_session_scope():
        class Session:
            calls = 0

            async def execute(self, statement):
                self.calls += 1
                # PATCH loads the existing note before validating the ref.
                row = (
                    Note(slug="existing", status="draft")
                    if method == "patch" and self.calls == 1
                    else None
                )

                class Result:
                    def scalar_one_or_none(self):
                        return row

                return Result()

            async def commit(self):
                pytest.fail("invalid evidence ref must not commit")

        yield Session()

    monkeypatch.setattr(notes_module, "session_scope", fake_session_scope)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(notes_router.router, prefix="/api/v1")
    from app.config import get_settings
    from app.redis import get_redis

    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_redis] = lambda: None

    with TestClient(app) as client:
        response = getattr(client, method)(path, json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "INVALID_EVIDENCE_REF",
            "message": "evidence ref forecast:999999 does not resolve to an existing row",
        }
    }


def test_unrelated_invalid_request_api_stays_400():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(notes_router.router, prefix="/api/v1")
    from app.config import get_settings
    from app.redis import get_redis

    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_redis] = lambda: None
    with TestClient(app) as client:
        response = client.post("/api/v1/notes/", json=_payload(title="   "))
    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "INVALID_REQUEST", "message": "title must not be empty"}
    }


def test_journal_entry_evidence_ref_hydrates_with_title(monkeypatch):
    stamp = int(time.time() * 1000)

    async def _body(settings):
        async with notes_module.session_scope() as session:
            entry = JournalEntry(
                entry_date=date(2026, 9, 1),
                title=f"Evidence entry {stamp}",
                view="v",
                probability=0.5,
                review_date=date(2026, 9, 10),
                horizon_days=9,
                subject=f"EVID_{stamp}",
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            entry_id = entry.id

        svc = NotesService(redis=None, settings=settings)
        created = await svc.create(
            _payload(
                title=f"Note citing entry {stamp}",
                evidence_refs=[{"kind": "journal_entry", "id": entry_id}],
            )
        )
        result = await svc.get(created["note"]["slug"])
        assert result["evidence"][0]["resolved"] is True
        assert result["evidence"][0]["label"] == f"Evidence entry {stamp}"

    _with_real_db(monkeypatch, _body)


def test_report_pack_evidence_ref_resolves_by_report_week(monkeypatch):
    stamp = int(time.time() * 1000)
    week = f"W{str(stamp)[-6:]}"

    async def _body(settings):
        async with notes_module.session_scope() as session:
            session.add(ReportPack(report_week=week))
            await session.commit()

        svc = NotesService(redis=None, settings=settings)
        created = await svc.create(
            _payload(
                title=f"Note citing pack {stamp}",
                evidence_refs=[{"kind": "report_pack", "id": week}],
            )
        )
        result = await svc.get(created["note"]["slug"])
        assert result["evidence"][0]["resolved"] is True
        assert result["evidence"][0]["label"] == f"Report pack {week}"

    _with_real_db(monkeypatch, _body)


def test_url_evidence_ref_accepted_as_is(monkeypatch):
    stamp = int(time.time() * 1000)

    async def _body(settings):
        svc = NotesService(redis=None, settings=settings)
        created = await svc.create(
            _payload(
                title=f"Note with url ref {stamp}",
                evidence_refs=[
                    {
                        "kind": "url",
                        "id": "https://example.com/x",
                        "label": "External source",
                    }
                ],
            )
        )
        result = await svc.get(created["note"]["slug"])
        assert result["evidence"][0] == {
            "kind": "url",
            "id": "https://example.com/x",
            "label": "External source",
            "resolved": True,
        }

    _with_real_db(monkeypatch, _body)


def test_signed_note_patch_returns_409(monkeypatch):
    stamp = int(time.time() * 1000)

    async def _body(settings):
        svc = NotesService(redis=None, settings=settings)
        created = await svc.create(_payload(title=f"Signed note {stamp}"))
        slug = created["note"]["slug"]
        signed = await svc.sign(slug)
        assert signed["note"]["status"] == "signed"
        assert signed["note"]["signed_at"] is not None
        with pytest.raises(HTTPException) as exc_info:
            await svc.update(slug, {"body": "new body"})
        assert exc_info.value.status_code == 409

    _with_real_db(monkeypatch, _body)


# ── router — DEMO_MODE seam, no DB/service construction ────────────────
def _create_body(**kw) -> notes_router.NoteCreateRequest:
    values = dict(kind="research_note", title="t", one_sentence="s")
    values.update(kw)
    return notes_router.NoteCreateRequest(**values)


async def test_router_create_demo_mode_raises_before_any_db_touch():
    settings = _settings(demo_mode=True)
    with pytest.raises(DemoModeReadOnly):
        await notes_router.create_note(
            _create_body(), settings=settings, redis=object()
        )


async def test_router_list_notes_demo_mode_returns_empty():
    settings = _settings(demo_mode=True)
    result = await notes_router.list_notes(settings=settings, redis=object())
    assert result == {"notes": [], "count": 0}


async def test_router_get_note_demo_mode_404s():
    settings = _settings(demo_mode=True)
    with pytest.raises(NotFound):
        await notes_router.get_note(slug="x", settings=settings, redis=object())


async def test_router_update_note_demo_mode_raises():
    settings = _settings(demo_mode=True)
    body = notes_router.NoteUpdateRequest(body="x")
    with pytest.raises(DemoModeReadOnly):
        await notes_router.update_note(
            body, slug="x", settings=settings, redis=object()
        )


async def test_router_sign_note_demo_mode_raises():
    settings = _settings(demo_mode=True)
    with pytest.raises(DemoModeReadOnly):
        await notes_router.sign_note(slug="x", settings=settings, redis=object())


def test_create_request_model_rejects_invalid_kind():
    with pytest.raises(ValidationError):
        _create_body(kind="bogus")


def test_update_request_model_forbids_server_managed_fields():
    with pytest.raises(ValidationError):
        notes_router.NoteUpdateRequest(kind="thesis")
