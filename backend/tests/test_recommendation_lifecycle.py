"""Tests for `services/recommendation_lifecycle.py`.

Pure fixture tests, no live Postgres — mirrors `test_advisor_report.py` /
`test_reports_router.py`'s DB-seam monkeypatch convention (`session_scope`
patched at its own module-level import, a hand-rolled fake standing in for
the SQLAlchemy result).

Covers the brief's acceptance points not already exercised end-to-end
through the router in `test_reports_router.py`:
  (a) every legal edge in `TRANSITIONS` succeeds; every illegal edge raises
      `InvalidRequest` naming both statuses.
  (b) `DemoModeReadOnly` raised before any `session_scope()` call.
  (c) `expire_and_supersede` on a 3-rule fixture (one stale proposed, one
      rule with two proposed weeks, one fresh) inserts exactly 1 `expired`
      + 1 `superseded` row, and 0 on a rerun against the post-tick state.
"""
from __future__ import annotations

import contextlib
import copy
from datetime import date, datetime, timezone

import pytest

import app.services.recommendation_lifecycle as lifecycle
from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import Recommendation


def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _row(**kw: object) -> Recommendation:
    base = dict(
        id=1, rule_id="weight_band_trim", as_of=date(2026, 8, 17), report_week="2026-W34",
        action="sell", sizing=1000.0, input_snapshot={"symbol": "BTC"},
        regime_component_set_id=None, status="proposed",
        tax_cost_arithmetic=None, note="fires", prior_id=None,
        created_at=datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Recommendation(**base)


class _FakeResult:
    def __init__(self, payload) -> None:
        self._payload = payload

    def scalar_one_or_none(self):
        return self._payload

    def scalars(self):
        return self

    def all(self):
        return self._payload


class _FakeTransitionDB:
    """Two-query seam for `transition()`: the prior row's
    `select(...).with_for_update()`, then the child-existence probe
    (`select(Recommendation.id).where(prior_id == ...)`) — distinguished by
    which column the whereclause compares (mirrors `_FakeLifecycleDB` below)."""

    def __init__(self, prior: Recommendation | None, *, existing_child_id: int | None = None) -> None:
        self.prior = prior
        self.existing_child_id = existing_child_id
        self.added: list[Recommendation] = []

    async def execute(self, stmt: object) -> _FakeResult:
        if stmt.whereclause is not None and stmt.whereclause.left.key == "prior_id":
            return _FakeResult(self.existing_child_id)
        return _FakeResult(self.prior)

    def add(self, obj: Recommendation) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: Recommendation) -> None:
        pass


def _wire(monkeypatch, prior: Recommendation | None, *, existing_child_id: int | None = None) -> _FakeTransitionDB:
    db = _FakeTransitionDB(prior, existing_child_id=existing_child_id)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope)
    return db


class _DBTouched(Exception):
    pass


def _boom_session_scope():
    raise _DBTouched()


# ── demo mode: no DB touch ────────────────────────────────────────────────
async def test_transition_rejected_in_demo_mode_before_any_db_read(monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "session_scope", _boom_session_scope)
    with pytest.raises(DemoModeReadOnly):
        await lifecycle.transition(1, "accepted", note="x", extra_snapshot=None, settings=_settings(demo_mode=True))


async def test_expire_and_supersede_rejected_in_demo_mode_before_any_db_read(monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "session_scope", _boom_session_scope)
    with pytest.raises(DemoModeReadOnly):
        await lifecycle.expire_and_supersede(_settings(demo_mode=True), now=datetime(2026, 8, 31, tzinfo=timezone.utc))


# ── legal edges succeed ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "from_status,to_status,extra",
    [
        ("proposed", "accepted", None),
        ("proposed", "modified", {"sizing_override": 42.0}),
        ("proposed", "overridden", {"human_action": "override"}),
        (
            "accepted", "executed",
            {"executed_at": "2026-08-25T00:00:00+00:00", "fill_price": 1.0, "quantity": 1.0, "venue": "ibkr"},
        ),
        (
            "modified", "executed",
            {"executed_at": "2026-08-25T00:00:00+00:00", "fill_price": 1.0, "quantity": 1.0, "venue": "ibkr"},
        ),
    ],
)
async def test_legal_edges_succeed(monkeypatch, from_status, to_status, extra) -> None:
    prior = _row(status=from_status)
    db = _wire(monkeypatch, prior)
    result = await lifecycle.transition(1, to_status, note="ok", extra_snapshot=extra, settings=_settings())
    assert db.added[0] is result["new"]
    assert result["new"].status == to_status
    assert result["new"].prior_id == prior.id


# ── illegal edges raise, naming both statuses ──────────────────────────────
@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("accepted", "accepted"),
        ("executed", "accepted"),
        ("executed", "executed"),
        ("overridden", "executed"),
        ("evaluated", "accepted"),
        ("proposed", "executed"),  # must go through accepted/modified first
    ],
)
async def test_illegal_edges_raise_naming_both_statuses(monkeypatch, from_status, to_status) -> None:
    prior = _row(status=from_status)
    _wire(monkeypatch, prior)
    with pytest.raises(InvalidRequest) as exc:
        await lifecycle.transition(1, to_status, note="x", extra_snapshot=None, settings=_settings())
    message = str(exc.value)
    assert from_status in message
    assert to_status in message


async def test_transition_on_row_with_existing_child_raises(monkeypatch) -> None:
    """A `proposed` row already superseded by a child (e.g. accepted once,
    then accepted again) is not a leaf — acting on it again must not fork
    the chain."""
    prior = _row(id=1, status="proposed")
    _wire(monkeypatch, prior, existing_child_id=90)
    with pytest.raises(InvalidRequest) as exc:
        await lifecycle.transition(1, "accepted", note="x", extra_snapshot=None, settings=_settings())
    message = str(exc.value)
    assert "not a leaf" in message
    assert "90" in message


async def test_transition_not_found_raises(monkeypatch) -> None:
    _wire(monkeypatch, None)
    with pytest.raises(NotFound):
        await lifecycle.transition(999, "accepted", note="x", extra_snapshot=None, settings=_settings())


async def test_modify_without_sizing_override_raises(monkeypatch) -> None:
    prior = _row(status="proposed")
    _wire(monkeypatch, prior)
    with pytest.raises(InvalidRequest):
        await lifecycle.transition(1, "modified", note="x", extra_snapshot=None, settings=_settings())


async def test_execute_missing_a_required_field_raises(monkeypatch) -> None:
    prior = _row(status="accepted")
    _wire(monkeypatch, prior)
    with pytest.raises(InvalidRequest):
        await lifecycle.transition(
            1, "executed", note="x",
            extra_snapshot={"executed_at": "2026-08-25T00:00:00+00:00", "fill_price": 1.0},  # missing quantity/venue
            settings=_settings(),
        )


async def test_prior_row_untouched_and_note_carried_forward(monkeypatch) -> None:
    prior = _row(status="proposed", note="cash short of the target by 500")
    db = _wire(monkeypatch, prior)
    result = await lifecycle.transition(1, "accepted", note="approved", extra_snapshot=None, settings=_settings())
    assert result["prior"] is prior
    assert prior.status == "proposed"  # never mutated
    assert result["new"].note == "cash short of the target by 500"  # rule arithmetic carried forward
    assert result["new"].input_snapshot["human_note"] == "approved"


# ── expire_and_supersede: 3-rule fixture ───────────────────────────────────
class _FakeAllResult:
    def __init__(self, rows: list[Recommendation]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeLifecycleDB:
    """`execute()` serves four distinct queries (fix 3 replaced the old
    no-`WHERE` full-table scan with two bounded ones): the `status ==
    "proposed"` leaf-candidate scan, the `prior_id IN (candidate ids)`
    leaf-ness resolution, the per-leaf `id == ... .with_for_update()`
    re-select `_lock_leaf` issues right before each insert, and
    `_lock_leaf`'s own child-existence probe (`prior_id == ...`) —
    distinguished by `stmt.whereclause`, which column it compares, and (for
    the two `prior_id`-keyed shapes) whether the right-hand side is a list
    (`IN`) or a scalar (`==`) (mirrors `test_forecasts_router.py`'s fake).
    `child_ids` maps a leaf id to an existing child id, for the "already
    forked" test; absent entries mean no child (the default for the base
    fixture)."""

    def __init__(self, rows: list[Recommendation], *, child_ids: dict[int, int] | None = None) -> None:
        self._rows = rows  # the full fixture — only read via bounded queries below
        # independent objects: mutating one to simulate a race must NOT
        # retroactively change what the initial scan above already "saw"
        self._by_id = {r.id: copy.copy(r) for r in rows}
        self._child_ids = child_ids or {}
        self.added: list[Recommendation] = []
        self.lock_calls: list[int] = []

    async def execute(self, stmt: object):
        wc = stmt.whereclause
        if wc.left.key == "status":
            return _FakeAllResult([r for r in self._rows if r.status == wc.right.value])
        if wc.left.key == "prior_id":
            right_value = wc.right.value
            if isinstance(right_value, (list, tuple, set)):
                ids = set(right_value)
                return _FakeAllResult([r.prior_id for r in self._rows if r.prior_id in ids])
            return _FakeResult(self._child_ids.get(right_value))
        leaf_id = wc.right.value
        self.lock_calls.append(leaf_id)
        return _FakeResult(self._by_id.get(leaf_id))

    def add(self, obj: Recommendation) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


def _fixture_rows() -> list[Recommendation]:
    return [
        # rule A: one stale proposed row (as_of well past expiry_weeks=4)
        _row(
            id=1, rule_id="cash_floor_band", as_of=date(2026, 7, 1), report_week="2026-W27",
            input_snapshot={"shortfall_eur": 500.0}, created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        # rule B: two proposed weeks, same symbol scope, both still fresh
        _row(
            id=2, rule_id="weight_band_trim", as_of=date(2026, 8, 10), report_week="2026-W33",
            input_snapshot={"symbol": "BTC"}, created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        ),
        _row(
            id=3, rule_id="weight_band_trim", as_of=date(2026, 8, 17), report_week="2026-W34",
            input_snapshot={"symbol": "BTC"}, created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        ),
        # rule C: one fresh proposed row, alone in its scope
        _row(
            id=4, rule_id="dca_modulation", as_of=date(2026, 8, 24), report_week="2026-W35",
            input_snapshot={}, created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        ),
    ]


async def test_expire_and_supersede_fixture_exact_counts(monkeypatch) -> None:
    rows = _fixture_rows()
    db = _FakeLifecycleDB(rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope)
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    result = await lifecycle.expire_and_supersede(_settings(recommendation_expiry_weeks=4), now=now)

    assert result == {"expired": 1, "superseded": 1}
    assert len(db.added) == 2
    expired_rows = [r for r in db.added if r.status == "expired"]
    superseded_rows = [r for r in db.added if r.status == "superseded"]
    assert len(expired_rows) == 1 and expired_rows[0].prior_id == 1  # the stale cash_floor_band row
    assert len(superseded_rows) == 1 and superseded_rows[0].prior_id == 2  # the OLDER of the two BTC rows
    assert "expired by lifecycle service" in expired_rows[0].input_snapshot["human_note"]
    assert "superseded by a newer" in superseded_rows[0].input_snapshot["human_note"]

    # ── idempotent rerun: simulate post-commit state (the two new rows now
    # persisted, each pointing prior_id at the leaf it replaced) ──
    for i, new_row in enumerate(db.added, start=100):
        new_row.id = i
    post_tick_rows = rows + db.added
    db2 = _FakeLifecycleDB(post_tick_rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope_2():
        yield db2

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope_2)
    rerun = await lifecycle.expire_and_supersede(_settings(recommendation_expiry_weeks=4), now=now)
    assert rerun == {"expired": 0, "superseded": 0}
    assert db2.added == []


def _naive_leaves(rows: list[Recommendation]) -> set[int]:
    """The OLD (pre-) full-table-scan algorithm's leaf set — the reference
    a mixed fixture's bounded-query result is checked against below."""
    prior_ids = {r.prior_id for r in rows if r.prior_id is not None}
    return {r.id for r in rows if r.status == "proposed" and r.id not in prior_ids}


async def test_expire_and_supersede_bounded_query_matches_unbounded_leaf_set(monkeypatch) -> None:
    """fix 3: `expire_and_supersede` now resolves leaves with two bounded
    queries (`status == "proposed"` then `prior_id IN (candidate ids)`)
    instead of a full-table scan. On a mixed fixture — the base 4 rows plus
    a terminal noise row unrelated to any chain, and a `proposed` row
    already superseded by a NON-`accepted` status (`overridden`, exercising
    that leaf-ness is a status-agnostic child-existence check, not an
    `accepted`-only one) — the bounded result must equal the naive
    full-scan reference."""
    rows = _fixture_rows() + [
        _row(  # noise: terminal, unrelated to any chain
            id=5, rule_id="weight_band_trim", as_of=date(2026, 8, 20), report_week="2026-W34",
            input_snapshot={"symbol": "ETH"}, status="accepted", prior_id=None,
            created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ),
        _row(  # a proposed row already overridden — must be excluded as a non-leaf
            id=6, rule_id="weight_band_trim", as_of=date(2026, 8, 28), report_week="2026-W35",
            input_snapshot={"symbol": "SOL"}, status="proposed", prior_id=None,
            created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        ),
        _row(
            id=7, rule_id="weight_band_trim", as_of=date(2026, 8, 28), report_week="2026-W35",
            input_snapshot={"symbol": "SOL", "human_note": "rejected"}, status="overridden", prior_id=6,
            created_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        ),
    ]
    reference_leaves = _naive_leaves(rows)
    assert reference_leaves == {1, 2, 3, 4}  # 6 excluded: already overridden by row 7; 5 isn't proposed

    db = _FakeLifecycleDB(rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope)
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    result = await lifecycle.expire_and_supersede(_settings(recommendation_expiry_weeks=4), now=now)

    assert result == {"expired": 1, "superseded": 1}  # identical to the plain 4-row fixture — rows 5-7 are inert
    acted_on = {r.prior_id for r in db.added}
    assert acted_on == {1, 2}  # the stale cash_floor_band leaf + the older BTC leaf, both within reference_leaves
    assert acted_on <= reference_leaves


async def test_expire_and_supersede_skips_leaf_transitioned_concurrently(monkeypatch) -> None:
    """HIGH fix: a concurrent human `transition()` moves the stale leaf off
    `proposed` between the initial scan and the per-leaf lock re-select —
    `_lock_leaf` must see the fresh status and skip it, not fork the chain."""
    rows = _fixture_rows()
    db = _FakeLifecycleDB(rows)
    # Simulate the race: by the time `_lock_leaf` re-selects row 1 (the
    # stale cash_floor_band leaf) under FOR UPDATE, it's already been
    # accepted by a human — the fake's by-id table reflects the post-race
    # state while the initial full-table scan still saw the old snapshot.
    db._by_id[1].status = "accepted"

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope)
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    result = await lifecycle.expire_and_supersede(_settings(recommendation_expiry_weeks=4), now=now)

    assert result == {"expired": 0, "superseded": 1}  # the BTC supersede pair is untouched by the race
    assert 1 in db.lock_calls  # the lock re-select was attempted
    assert all(r.prior_id != 1 for r in db.added)  # no child row forked off the transitioned leaf


async def test_expire_and_supersede_skips_leaf_already_forked(monkeypatch) -> None:
    """Same race as above, but the concurrent `transition()` already
    inserted a child rather than (impossibly) flipping `status` — append-
    only rows never do the latter, so `_lock_leaf`'s child-existence probe
    is the only thing that can catch this."""
    rows = _fixture_rows()
    db = _FakeLifecycleDB(rows, child_ids={1: 99})

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(lifecycle, "session_scope", _fake_session_scope)
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    result = await lifecycle.expire_and_supersede(_settings(recommendation_expiry_weeks=4), now=now)

    assert result == {"expired": 0, "superseded": 1}  # the BTC supersede pair is untouched by the race
    assert 1 in db.lock_calls  # the lock re-select was attempted
    assert all(r.prior_id != 1 for r in db.added)  # no child row forked off the already-forked leaf
