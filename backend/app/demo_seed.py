"""Sample-artifact seed for the hosted public demo (DEMO-SPEC §13): `python -m app.demo_seed`.

One-shot job (compose service `demo-seed`, after `alembic upgrade head`, before the backend
serves). It writes ORM rows straight into Postgres so every product tab has one synthetic
sample: 2 backtests, 1 lab factor study, 3 journal entries, 3 notes, 5 watchlist symbols,
user signals, one filled paper order with its fill/position/snapshots, a few news-intel rows.

Safety: it REFUSES to run unless DEMO_MODE, DEMO_READONLY and DEMO_SAMPLE_READS are all true
(only the hosted demo sets them) and it refuses when a seeded table holds rows that are not
its own sample rows, so it can never write into (or on top of) a private database. Everything is synthetic: prices,
curves and text are generated here or fixed sample text; links point at example.com.

Idempotent: each product is written only when its marker row is absent
(`ON CONFLICT DO NOTHING` on the natural keys), so a second run changes nothing. Dates are
relative to today. JSON result blobs are validated through the same pydantic models the
read paths use (`BacktestReport`, `FactorReport`).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.backtest.base import BacktestConfig, BacktestReport
from app.adapters.lab.base import FactorConfig, FactorReport
from app.adapters.yahoo_symbols import to_yahoo_symbol
from app.config import Settings, get_settings
from app.db import dispose_engine, init_engine, session_scope
from app.models import (
    AlgoAccountSnapshot, AlgoFill, AlgoOrder, AlgoPosition, Backtest, JournalEntry,
    LabRun, NewsIntelRow, Note, SignalRow, WatchlistItem,
)

logger = logging.getLogger("catallax.demo_seed")

_NS = uuid.UUID("5a7e0d0e-0000-4000-8000-0000000c7d0e")  # fixed namespace -> stable job ids
SAMPLE_NOTE = "Sample data: synthetic, generated for the public demo."

# Persona symbols the warm job stores daily bars for (services/demo.WF_ASSETS): the signal
# grader reads these from `daily_bars`, so they must be a subset of the warmed keys.
SIGNAL_EQUITIES = ("AAPL", "MSFT", "NVDA", "IWDA.AS", "VWCE.DE")
SIGNAL_CRYPTO = ("BTC", "ETH")
SIGNAL_IDS = ("xs_mom_252_21", "xs_lowvol_63")
ALGO_SYMBOL = "AAPL"  # a persona holding the warm job quotes


def _job_id(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def _at(d: date, hour: int = 9) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


def _bdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _month_ends(start: date, end: date) -> list[date]:
    """Last business day of each month inside [start, end]."""
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if start <= d <= end:
            out.append(d)
        y, m = nxt.year, nxt.month
    return out


def _mean_std(xs: list[float]) -> tuple[float, float]:
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
    return mu, sd


# ── backtests ───────────────────────────────────────────────────
def _curve(rng: random.Random, days: list[date], drift: float, vol: float) -> list[float]:
    v, out = 1.0, []
    for _ in days:
        v *= math.exp(rng.gauss(drift, vol))
        out.append(v)
    return out


def build_backtest(name: str, signal_id: str, today: date, seed: int, long_short: bool,
                   quantile: int, drift: float, vol: float) -> dict[str, Any]:
    start, end = today - timedelta(days=400), today - timedelta(days=6)
    cfg = BacktestConfig(name=name, signal_id=signal_id, start=start, end=end, rebalance="M",
                         quantile=quantile, long_short=long_short, costs_bps=10.0)
    rng = random.Random(seed)
    days = _bdays(start, end)
    eq = _curve(rng, days, drift, vol)
    bench = _curve(random.Random(seed + 1), days, 0.0003, 0.009)
    peak, dd = 0.0, []
    for v in eq:
        peak = max(peak, v)
        dd.append(v / peak - 1.0)
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    mu, sd = _mean_std(rets)
    n = len(days)
    cum = eq[-1] - 1.0
    ann = (eq[-1] ** (252.0 / n) - 1.0) if n else 0.0
    ann_vol = sd * math.sqrt(252.0)
    rebs = _month_ends(start, end)
    ics = [round(rng.gauss(0.04, 0.12), 4) for _ in rebs]
    def _entry(d: date) -> str | None:
        nxt = _bdays(d + timedelta(days=1), d + timedelta(days=5))
        return nxt[0].isoformat() if nxt else None

    rebalances = [
        {"date": d.isoformat(), "entry_date": _entry(d),
         "n_long": 2 if quantile >= 3 else 3, "n_short": 2 if long_short else 0,
         "turnover": round(rng.uniform(0.15, 0.55), 3), "ic": ics[i],
         "status": "pending" if i == len(rebs) - 1 else "matured"}
        for i, d in enumerate(rebs)
    ]
    matured = [r for r in rebalances if r["status"] == "matured"]
    report = BacktestReport.model_validate({
        "signal_id": signal_id, "engine": "vectorized", "name": name,
        "window_start": days[0].isoformat(), "window_end": days[-1].isoformat(),
        "equity": [{"date": d.isoformat(), "value": round(v, 5)} for d, v in zip(days, eq)],
        "drawdown": [{"date": d.isoformat(), "value": round(v, 5)} for d, v in zip(days, dd)],
        "benchmark_equity": [{"date": d.isoformat(), "value": round(v, 5)} for d, v in zip(days, bench)],
        "rebalances": rebalances,
        "metrics": {
            "cum_return_pct": round(cum * 100, 2), "ann_return_pct": round(ann * 100, 2),
            "ann_vol_pct": round(ann_vol * 100, 2),
            "sharpe": round((mu * 252.0) / ann_vol, 2) if ann_vol else None,
            "info_ratio": round(rng.uniform(0.1, 0.8), 2),
            "max_drawdown_pct": round(min(dd) * 100, 2),
            "turnover_annualised": round(sum(r["turnover"] for r in matured) / len(matured) * 12, 2),
            "mean_ic": round(sum(r["ic"] for r in matured) / len(matured), 4),
            "n_rebalances": len(rebalances), "n_matured_periods": len(matured),
            "n_pending_periods": len(rebalances) - len(matured),
            "n_names_avg": 7.0, "n_days": n, "benchmark": "synthetic equal-weight",
        },
        "warnings": [SAMPLE_NOTE],
    })
    return {
        "job_id": _job_id(name), "name": name, "engine": "vectorized", "signal_id": signal_id,
        "config": cfg.model_dump(mode="json"), "status": "done", "progress": 1.0, "phase": "done",
        "result": report.model_dump(mode="json"), "warnings": [SAMPLE_NOTE],
        "created_at": _at(today - timedelta(days=3)), "updated_at": _at(today - timedelta(days=3), 10),
    }


# ── lab factor study ────────────────────────────────────────────
def build_factor_run(today: date) -> dict[str, Any]:
    name = "demo: momentum factor study, 21d horizon"
    start, end = today - timedelta(days=400), today - timedelta(days=30)
    cfg = FactorConfig(name=name, factor="momentum", start=start, end=end, horizon_days=21, quantiles=5)
    rng = random.Random(77)
    dates = _month_ends(start, end)
    ics = [round(rng.gauss(0.045, 0.11), 4) for _ in dates]
    mu, sd = _mean_std(ics)
    t = mu / (sd / math.sqrt(len(ics))) if sd else None
    qret = [-0.006, -0.002, 0.003, 0.007, 0.013]
    report = FactorReport.model_validate({
        "name": name, "factor": "momentum", "horizon_days": 21,
        "window_start": dates[0].isoformat(), "window_end": dates[-1].isoformat(),
        "n_periods": len(ics), "n_symbols": 7, "n_obs": len(ics) * 7,
        "ic_mean": round(mu, 4), "ic_std": round(sd, 4), "ic_ir": round(mu / sd, 3) if sd else None,
        "ic_tstat": round(t, 2) if t is not None else None,
        "ic_hit_rate": round(sum(1 for x in ics if x > 0) / len(ics), 3),
        "ic_series": [{"date": d.isoformat(), "ic": v} for d, v in zip(dates, ics)],
        "quantile_returns": [{"quantile": i + 1, "mean_fwd_ret": r, "n_avg": 1.4} for i, r in enumerate(qret)],
        "long_short_spread": round(qret[-1] - qret[0], 4),
        "warnings": [SAMPLE_NOTE],
    })
    return {
        "job_id": _job_id(name), "kind": "factor", "name": name, "engine": "factor-study",
        "config": cfg.model_dump(mode="json"), "status": "done", "progress": 1.0, "phase": "done",
        "result": report.model_dump(mode="json"), "warnings": [SAMPLE_NOTE],
        "created_at": _at(today - timedelta(days=2)), "updated_at": _at(today - timedelta(days=2), 10),
    }


# ── journal / notes / watchlist ─────────────────────────────────
_JOURNAL = (
    ("Demo persona: keep the broad index fund as the core", "VWCE", 0.70, 150, -30,
     "Broad global equity stays the core holding; no reason to rotate on a short-term view.",
     "Low cost, wide diversification, and the plan is a 10+ year horizon. Single-stock risk is better taken in small satellite positions.",
     "A sustained rise in fund costs, or a change in the plan's time horizon.",
     "Core stays the largest position; satellites capped at a small share of the portfolio."),
    ("Demo persona: mega-cap tech concentration check", "MSFT", 0.55, 90, -20,
     "Large-cap tech weight in the satellite sleeve is acceptable but should not grow unchecked.",
     "AAPL and MSFT already dominate the US sleeve; adding more would raise concentration without adding diversification.",
     "The two names exceed a third of the equity portfolio at a quarterly review.",
     "Hold current size; rebalance toward the core fund if the cap is breached."),
    ("Demo persona: crypto stays a small, fixed slice", "BTC", 0.50, 120, -10,
     "Keep the crypto allocation small and rebalance on a schedule rather than on price moves.",
     "High volatility argues for a hard cap and mechanical rebalancing instead of conviction-based sizing.",
     "Allocation drifts above its cap for two consecutive quarter-ends.",
     "Fixed small percentage of net worth; trim back to target each quarter."),
)


def build_journal(today: date) -> list[dict[str, Any]]:
    out = []
    for title, subj, p, review_in, entry_off, view, thesis, inval, sizing in _JOURNAL:
        entry, review = today + timedelta(days=entry_off), today + timedelta(days=review_in)
        out.append({
            "entry_date": entry, "title": title, "view": view, "probability": p,
            "review_date": review, "horizon_days": (review - entry).days, "subject": subj,
            "thesis": thesis, "invalidation": inval, "sizing_note": sizing,
            "confidence_note": SAMPLE_NOTE, "status": "open", "source": "human",
            "created_at": _at(entry), "updated_at": _at(entry),
        })
    return out


_NOTES = (
    ("demo-thesis-boring-core-small-satellites", "thesis", "Demo thesis: a boring core, small satellites",
     "A low-cost global index fund as the core with small, capped satellite positions beats frequent rotation for a long-horizon saver.",
     "Costs and behaviour dominate outcomes over long horizons; the structure limits both.",
     "Review allocation once a quarter and rebalance only when a band is breached.",
     "A structural change in fees, taxes or the investment horizon.", ["VWCE"]),
    ("demo-note-signal-scoreboard", "research_note", "Demo note: how to read the signal scoreboard",
     "The scoreboard ranks logged signals by rank information coefficient; a high IC on a short window is weak evidence.",
     "It keeps the Quant tab honest: signals earn trust through out-of-sample grading, not backtest curves.",
     "Check the horizon and sample size before trusting any single score.",
     "Overlapping windows inflate apparent significance.", ["AAPL", "MSFT"]),
    ("demo-note-eur-currency-exposure", "research_note", "Demo note: currency exposure of a EUR-based portfolio",
     "A EUR-based investor holding USD assets carries FX risk that the Portfolio risk lens makes visible.",
     "Currency moves can outweigh a small equity move over a quarter.",
     "Compare the FX drag panel with the allocation view before adding USD exposure.",
     "A sharp change in the EUR/USD regime.", ["EURUSD=X"]),
)


def build_notes(today: date) -> list[dict[str, Any]]:
    return [
        {"slug": slug, "kind": kind, "title": title, "status": "draft", "one_sentence": one,
         "why_it_matters": why, "action": act, "risk_invalidation": risk,
         "body": f"{SAMPLE_NOTE}\n\n{one}", "subjects": subj,
         "created_at": _at(today - timedelta(days=15 - 4 * i)), "updated_at": _at(today - timedelta(days=15 - 4 * i))}
        for i, (slug, kind, title, one, why, act, risk, subj) in enumerate(_NOTES)
    ]


WATCHLIST = ("AAPL", "MSFT", "VWCE.DE", "BTC-USD", "^GSPC")


def build_watchlist() -> list[dict[str, Any]]:
    return [{"symbol": s, "label": None, "source": None, "sort_order": i} for i, s in enumerate(WATCHLIST)]


# ── user signals ────────────────────────────────────────────────
def signal_symbols(settings: Settings) -> list[str]:
    """Yahoo price symbols, resolved like the risk lens does (crypto -> BTC-USD)."""
    return list(SIGNAL_EQUITIES) + [to_yahoo_symbol(s, settings, crypto=True) for s in SIGNAL_CRYPTO]


def build_signals(today: date, settings: Settings) -> list[dict[str, Any]]:
    """Month-end cross-sections ending >= 45 days ago, so grades have matured by read time."""
    syms = signal_symbols(settings)
    dates = _month_ends(today - timedelta(days=45 + 31 * 11), today - timedelta(days=45))
    rows = []
    for sid in SIGNAL_IDS:
        rng = random.Random(f"demo-signal-{sid}")
        for d in dates:
            for sym in syms:
                rows.append({"signal_id": sid, "as_of_date": d, "symbol": sym, "source": "yfinance",
                             "value": round(rng.gauss(0.0, 1.0), 4),
                             "meta": {"sample": True}})
    return rows


# ── algo / paper book ───────────────────────────────────────────
def build_algo(today: date, starting_cash: float) -> dict[str, Any]:
    qty, price, commission = 40.0, 285.0, 1.0
    filled = _at(today - timedelta(days=12), 14)
    cash = starting_cash - qty * price - commission
    snaps = []
    for i, off in enumerate(range(16, 0, -1)):
        d = today - timedelta(days=off)
        if d.weekday() >= 5:
            continue
        held = d >= filled.date()
        mark = price * (1.0 + 0.004 * math.sin(i)) if held else 0.0
        pv = qty * mark
        c = cash if held else starting_cash
        snaps.append({"snapshot_date": d, "cash": c, "positions_value": pv, "equity": c + pv,
                      "created_at": _at(d, 21)})
    return {
        "order": {"symbol": ALGO_SYMBOL, "side": "buy", "type": "market", "qty": qty, "limit_price": None,
                  "status": "filled", "reason": None, "created_at": filled, "updated_at": filled},
        "fill": {"symbol": ALGO_SYMBOL, "side": "buy", "price": price, "qty": qty,
                 "commission": commission, "slippage_bps": 5.0, "filled_at": filled},
        "position": {"symbol": ALGO_SYMBOL, "qty": qty, "avg_cost": price, "updated_at": filled},
        "snapshots": snaps,
    }


# ── news intel ──────────────────────────────────────────────────
_NEWS = (
    ("Sample: central bank holds rates, signals patience", "Sample Wire", "positive", 0.2, ["rates", "macro"], "Germany", 50.11, 8.68),
    ("Sample: chipmaker guides above expectations on data-centre demand", "Sample Wire", "positive", 0.6, ["tech", "earnings"], "United States", 37.39, -122.08),
    ("Sample: oil slips as inventories build", "Sample Markets", "negative", -0.4, ["energy", "commodities"], "Saudi Arabia", 24.71, 46.68),
    ("Sample: yen weakens after policy meeting", "Sample Markets", "negative", -0.3, ["fx", "rates"], "Japan", 35.68, 139.69),
    ("Sample: euro-area inflation edges lower", "Sample Wire", "neutral", 0.0, ["inflation", "macro"], "Belgium", 50.85, 4.35),
    ("Sample: crypto exchange volumes recover", "Sample Crypto Desk", "positive", 0.3, ["crypto"], "Singapore", 1.35, 103.82),
)


def build_news(today: date) -> list[dict[str, Any]]:
    cluster = str(uuid.uuid5(_NS, "news-cluster"))
    out = []
    for i, (title, src, sent, score, topics, country, lat, lon) in enumerate(_NEWS):
        out.append({
            "link": f"https://example.com/demo-sample/{i + 1}", "title": title, "source": src,
            "published": datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3 + 5 * i),
            "sentiment": sent, "sentiment_score": score, "topics": topics, "country": country,
            "lat": lat, "lon": lon, "summary": f"{SAMPLE_NOTE} Not a real article.",
            "cluster_id": cluster if i < 2 else str(uuid.uuid5(_NS, f"news-cluster-{i}")),
            "grounding": "speculative",
        })
    return out


# ── apply ───────────────────────────────────────────────────────
async def _absent(db: AsyncSession, model: Any, *where: Any) -> bool:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one() == 0


async def _insert(db: AsyncSession, model: Any, rows: list[dict[str, Any]]) -> None:
    if rows:
        await db.execute(pg_insert(model).values(rows).on_conflict_do_nothing())


class ForeignRowsError(RuntimeError):
    """A seeded table holds rows the seed did not write: this is not a demo database."""


async def check_demo_database(db: AsyncSession, markers: list[tuple[Any, Any]]) -> None:
    """Every table must hold exactly its sample rows (or none): any other row means the
    database is not a fresh/seeded demo one."""
    for model, marker in markers:
        total = (await db.execute(select(func.count()).select_from(model))).scalar_one()
        own = (await db.execute(select(func.count()).select_from(model).where(marker))).scalar_one()
        if total != own:
            raise ForeignRowsError(f"{model.__tablename__} has {total - own} non-sample row(s)")


async def seed(db: AsyncSession, settings: Settings, today: date | None = None) -> dict[str, int]:
    """Write every missing product; returns rows inserted per product (all 0 on a rerun)."""
    today = today or datetime.now(timezone.utc).date()
    done: dict[str, int] = {}
    bts = [
        build_backtest("demo: momentum, monthly, top quintile", "xs_mom_252_21", today, 11, False, 5, 0.0003, 0.010),
        build_backtest("demo: low-vol, monthly, long-short", "xs_lowvol_63", today, 23, True, 3, 0.0002, 0.007),
    ]
    lab = build_factor_run(today)
    journal, notes, news = build_journal(today), build_notes(today), build_news(today)
    book = build_algo(today, float(settings.algo_starting_cash))["fill"]
    await check_demo_database(db, [
        (Backtest, Backtest.job_id.in_([b["job_id"] for b in bts])),
        (LabRun, LabRun.job_id == lab["job_id"]),
        (JournalEntry, JournalEntry.title.in_([j["title"] for j in journal])),
        (Note, Note.slug.in_([n["slug"] for n in notes])),
        (WatchlistItem, WatchlistItem.symbol.in_(WATCHLIST)),
        (SignalRow, SignalRow.signal_id.in_(SIGNAL_IDS)),
        (NewsIntelRow, NewsIntelRow.link.in_([n["link"] for n in news])),
        (AlgoOrder, AlgoOrder.symbol == book["symbol"]),
        (AlgoFill, AlgoFill.symbol == book["symbol"]),
    ])

    async def product(label: str, model: Any, marker: Any, rows: list[dict[str, Any]]) -> None:
        if await _absent(db, model, marker):
            await _insert(db, model, rows)
            done[label] = len(rows)
        else:
            done[label] = 0

    await product("backtests", Backtest, Backtest.job_id.in_([b["job_id"] for b in bts]), bts)
    await product("lab_runs", LabRun, LabRun.job_id == lab["job_id"], [lab])
    await product("journal", JournalEntry, JournalEntry.title.in_([j["title"] for j in journal]), journal)
    await product("notes", Note, Note.slug.in_([n["slug"] for n in notes]), notes)
    wl = build_watchlist()
    await product("watchlist", WatchlistItem, WatchlistItem.symbol.in_(WATCHLIST), wl)
    await product("signals", SignalRow, SignalRow.signal_id.in_(SIGNAL_IDS), build_signals(today, settings))
    await product("news_intel", NewsIntelRow, NewsIntelRow.link.in_([n["link"] for n in news]), news)

    # Paper book: a fill's order_id needs the inserted order id, so these go in one step.
    if await _absent(db, AlgoOrder, AlgoOrder.id > 0):  # any order = a book exists; leave it
        algo = build_algo(today, float(settings.algo_starting_cash))
        order = AlgoOrder(**algo["order"])
        db.add(order)
        await db.flush()
        db.add(AlgoFill(order_id=order.id, **algo["fill"]))
        await _insert(db, AlgoPosition, [algo["position"]])
        await _insert(db, AlgoAccountSnapshot, algo["snapshots"])
        done["algo"] = 1
    else:
        done["algo"] = 0
    await db.commit()
    return done


async def _main() -> int:
    settings = get_settings()
    if not (settings.demo_mode and settings.demo_readonly and settings.demo_sample_reads):
        # The seed must never reach a private database: only the hosted demo sets all three.
        logger.error("demo_seed refused: DEMO_MODE, DEMO_READONLY and DEMO_SAMPLE_READS must all be true")
        return 2
    init_engine()
    try:
        async with session_scope() as db:
            done = await seed(db, settings)
    except ForeignRowsError as exc:
        logger.error("demo_seed refused: %s (not a demo database)", exc)
        return 3
    finally:
        await dispose_engine()
    logger.info("demo seed done: %s", done)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main()))
