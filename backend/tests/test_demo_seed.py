"""Sample-artifact seed for the hosted demo (DEMO-SPEC §13): pure builder checks always
run; the DB-backed checks (idempotence, read gates, reconciliation) need a throwaway
Postgres with the schema applied and are opt-in via DEMO_SEED_TEST_DB=1 (they WRITE rows),
so the default suite never touches a database."""
from __future__ import annotations

import os
from datetime import date

import httpx
import pytest
from sqlalchemy import func, select

import app.demo_seed as demo_seed
from app.adapters.backtest.base import BacktestReport
from app.adapters.lab.base import FactorReport
from app.config import Settings, get_settings
from app.main import create_app
from app.services.algo import replay_cash


def _settings(**kw: object) -> Settings:
    kw.setdefault("share_passphrase", "")
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


TODAY = date(2026, 10, 16)


# ── pure ────────────────────────────────────────────────────────
def test_demo_empty_reads_flag() -> None:
    assert _settings(demo_mode=True).demo_empty_reads is True
    # DEMO_READONLY is only a write gate: it never opens the database reads.
    assert _settings(demo_mode=True, demo_readonly=True).demo_empty_reads is True
    assert _settings(demo_mode=True, demo_sample_reads=True).demo_empty_reads is False
    assert _settings().demo_empty_reads is False


async def test_readonly_without_sample_reads_still_returns_empty() -> None:
    """No DB touched: the gate short-circuits before any query."""
    from app.services.backtest import BacktestService
    from app.services.algo import AlgoService
    s = _settings(demo_mode=True, demo_readonly=True)
    assert (await BacktestService(object(), s).list()).jobs == []
    assert await AlgoService(object(), s).list_orders() == []


class _Stop(Exception):
    pass


class _KeyRedis:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def get(self, key):
        self.keys.append(key)
        raise _Stop


async def test_demo_grade_ignores_refresh_and_pins_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.signals import SignalEngine
    s = _settings(demo_mode=True, demo_readonly=True, demo_sample_reads=True)
    r = _KeyRedis()
    eng = SignalEngine(r, s)

    async def rows(_sid):
        return [("2026-01-30", "AAPL", "yfinance", 1.0)]

    async def sig(_sid):
        return "sig"
    monkeypatch.setattr(eng, "_load_rows", rows)
    monkeypatch.setattr(eng, "_signature", sig)
    with pytest.raises(_Stop):  # refresh=True would have skipped the cache read entirely
        await eng.grade("xs_mom_252_21", horizon=5, quantiles=3, refresh=True)
    assert r.keys and f":{s.signal_default_horizon_bars}:{s.signal_default_quantiles}:" in r.keys[0]


@pytest.mark.parametrize("prefix", ["/api/v1/journal", "/api/v1/notes", "/api/v1/watchlist", "/api/v1/backtests",
                                    "/api/v1/lab", "/api/v1/signals", "/api/v1/algo"])
async def test_writes_still_403_with_sample_reads(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    import app.auth as auth_module
    from tests.test_demo_readonly import _Rec, _receive, _scope, _write_routes
    monkeypatch.setattr(auth_module, "get_settings", lambda: _settings(
        demo_mode=True, demo_readonly=True, demo_sample_reads=True))
    app = create_app()
    routes = [(m, p) for m, p in _write_routes() if p.startswith(prefix)]
    assert routes, prefix
    for method, path in routes:
        rec = _Rec()
        await app(_scope(method, path), _receive, rec)
        assert rec.status == 403, (method, path)


def test_blobs_validate_and_are_labelled_synthetic() -> None:
    for name, sid, ls in (("a", "xs_mom_252_21", False), ("b", "xs_lowvol_63", True)):
        row = demo_seed.build_backtest(name, sid, TODAY, 1, ls, 5, 0.0003, 0.01)
        rep = BacktestReport.model_validate(row["result"])
        assert rep.metrics.n_matured_periods >= 6 and row["status"] == "done"
        assert demo_seed.SAMPLE_NOTE in rep.warnings
    fr = FactorReport.model_validate(demo_seed.build_factor_run(TODAY)["result"])
    assert len(fr.quantile_returns) == 5 and demo_seed.SAMPLE_NOTE in fr.warnings


def test_journal_probabilities_and_horizons() -> None:
    for j in demo_seed.build_journal(TODAY):
        assert 0 < j["probability"] < 1
        assert j["review_date"] > j["entry_date"]
        assert j["horizon_days"] == (j["review_date"] - j["entry_date"]).days


def test_signals_use_persona_symbols_only() -> None:
    from app.services.demo import WF_ASSETS
    s = _settings()
    persona = {a.symbol for a in WF_ASSETS}
    syms = {r["symbol"] for r in demo_seed.build_signals(TODAY, s)}
    assert len({r["as_of_date"] for r in demo_seed.build_signals(TODAY, s)}) >= 8
    assert {x.replace("-USD", "") for x in syms} <= persona
    assert demo_seed.ALGO_SYMBOL in persona


def test_algo_cash_reconciles_by_replay() -> None:
    a = demo_seed.build_algo(TODAY, 100000.0)

    class F:  # replay_cash reads side/qty/price/commission
        def __init__(self, d): self.__dict__.update(d)
    cash = replay_cash([F(a["fill"])], 100000.0)
    assert a["snapshots"][-1]["cash"] == pytest.approx(cash)
    assert a["position"]["qty"] == a["fill"]["qty"]


def test_no_private_hosts_in_seed_text() -> None:
    for n in demo_seed.build_news(TODAY):
        assert n["link"].startswith("https://example.com/") and n["lat"] is not None


@pytest.mark.parametrize("flags", [
    {}, {"demo_readonly": True}, {"demo_mode": True, "demo_readonly": True},
    {"demo_mode": True, "demo_sample_reads": True}, {"demo_readonly": True, "demo_sample_reads": True},
])
async def test_refuses_unless_all_three_flags(monkeypatch: pytest.MonkeyPatch, flags: dict) -> None:
    monkeypatch.setattr(demo_seed, "get_settings", lambda: _settings(**flags))
    monkeypatch.setattr(demo_seed, "init_engine", lambda: pytest.fail("touched the DB"))
    assert await demo_seed._main() == 2


# ── DB-backed (opt-in) ──────────────────────────────────────────
needs_db = pytest.mark.skipif(os.environ.get("DEMO_SEED_TEST_DB") != "1", reason="DEMO_SEED_TEST_DB=1 (throwaway Postgres) not set")

@needs_db
async def test_seed_refuses_a_database_with_foreign_rows(db) -> None:
    from app.db import session_scope
    from app.models import WatchlistItem
    async with session_scope() as s:
        s.add(WatchlistItem(symbol="TSLA", sort_order=0))
        await s.commit()
    async with session_scope() as s:
        with pytest.raises(demo_seed.ForeignRowsError):
            await demo_seed.seed(s, get_settings())
    assert (await _counts())["Backtest"] == 0  # nothing was written


class _NoRedis:
    async def get(self, *a, **k): return None
    async def set(self, *a, **k): return True
    async def hget(self, *a, **k): return None


_TABLES = ("Backtest", "LabRun", "JournalEntry", "Note", "WatchlistItem", "SignalRow",
           "NewsIntelRow", "AlgoOrder", "AlgoFill", "AlgoPosition", "AlgoAccountSnapshot")


async def _counts() -> dict[str, int]:
    import app.models as m
    from app.db import session_scope
    async with session_scope() as db:
        return {t: (await db.execute(select(func.count()).select_from(getattr(m, t)))).scalar_one() for t in _TABLES}


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    import app.models as m
    from app.db import dispose_engine, init_engine, session_scope
    from sqlalchemy import delete
    for k, v in (("DEMO_MODE", "true"), ("DEMO_READONLY", "true"), ("DEMO_SAMPLE_READS", "true")):
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr("app.redis._redis", _NoRedis())  # PaperBroker.mark -> failed quotes -> avg_cost
    init_engine()
    async with session_scope() as s:
        for t in _TABLES:
            await s.execute(delete(getattr(m, t)))
        await s.commit()
    yield
    await dispose_engine()
    get_settings.cache_clear()


@needs_db
async def test_seed_is_idempotent(db) -> None:
    from app.db import session_scope
    async with session_scope() as s:
        first = await demo_seed.seed(s, get_settings())
    c1 = await _counts()
    async with session_scope() as s:
        second = await demo_seed.seed(s, get_settings())
    assert await _counts() == c1
    assert all(v > 0 for v in first.values()) and all(v == 0 for v in second.values())
    assert c1["Backtest"] == 2 and c1["JournalEntry"] == 3 and c1["Note"] == 3 and c1["WatchlistItem"] == 5


async def _client(app) -> httpx.AsyncClient:
    from app.redis import get_redis
    app.dependency_overrides[get_redis] = lambda: _NoRedis()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _first_list(j) -> int:
    return len(j if isinstance(j, list) else next(v for v in j.values() if isinstance(v, list)))


_READS = {  # path -> how to count rows in the JSON body
    "/api/v1/backtests": lambda j: len(j["jobs"]),
    "/api/v1/lab/runs": lambda j: len(j["jobs"]),
    "/api/v1/journal/entries": lambda j: j["count"],
    "/api/v1/notes/": lambda j: j["count"],
    "/api/v1/watchlist": lambda j: len(j),
    "/api/v1/signals": lambda j: len(j["data"]["signals"]),
    "/api/v1/algo/orders": _first_list,
    "/api/v1/algo/fills": _first_list,
    "/api/v1/algo/positions": _first_list,
    "/api/v1/intel/news": _first_list,
    "/api/v1/intel/events": _first_list,
}


@needs_db
async def test_reads_serve_seed_under_readonly_and_empty_under_demo_alone(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import session_scope
    async with session_scope() as s:
        await demo_seed.seed(s, get_settings())

    async def hit(path: str):
        async with await _client(create_app()) as c:
            r = await c.get(path)
            assert r.status_code == 200, (path, r.text[:200])
            return r.json()

    for path, count in _READS.items():
        assert count(await hit(path)) > 0, path
    # detail + report of a seeded job
    jobs = (await hit("/api/v1/backtests"))["jobs"]
    rep = await hit(f"/api/v1/backtests/{jobs[0]['job_id']}/report")
    assert rep["data"]["equity"]
    lab = (await hit("/api/v1/lab/runs"))["jobs"][0]["job_id"]
    assert (await hit(f"/api/v1/lab/runs/{lab}/report"))["data"]["ic_series"]

    # DEMO_MODE alone (local demo seam): the DB is NOT served, same rows still present.
    for env in ("DEMO_SAMPLE_READS", "DEMO_READONLY"):  # sample reads off: empty, readonly or not
        monkeypatch.setenv("DEMO_SAMPLE_READS", "false")
        monkeypatch.setenv("DEMO_READONLY", "true" if env == "DEMO_SAMPLE_READS" else "false")
        get_settings.cache_clear()
        for path, count in _READS.items():
            if path.startswith("/api/v1/intel"):
                continue  # news-intel is not gated
            assert count(await hit(path)) == 0, (env, path)


@needs_db
async def test_seeded_algo_book_reconciles_clean(db) -> None:
    from app.db import session_scope
    async with session_scope() as s:
        await demo_seed.seed(s, get_settings())
    async with await _client(create_app()) as c:
        r = await c.get("/api/v1/algo/reconciliation")
        acct = (await c.get("/api/v1/algo/account")).json()
    j = r.json()
    assert r.status_code == 200 and j["clean"] is True
    assert j["cash"]["match"] is True and j["positions"] and all(p["match"] for p in j["positions"])
    assert acct["snapshots"] and acct["equity"] > 0
