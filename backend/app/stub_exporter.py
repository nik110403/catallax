"""Stub Wealthfolio exporter: serves the demo persona (`services/demo.py`) in the
exporter's wire format, so `WealthfolioBridge` can run unmodified against it.

Run: ``uvicorn app.stub_exporter:app --host 0.0.0.0 --port 8089``.
Decimals are emitted as JSON numbers (FastAPI's encoder), which the bridge parses back
to Decimal."""
from __future__ import annotations

from datetime import date

from fastapi import FastAPI

from app.services.demo import (
    WF_ASSETS, WF_CASH, WF_INVESTMENTS, WF_TOTAL, wf_accounts, wf_networth_series, wf_position_history,
)

app = FastAPI(title="Catallax stub exporter")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/networth")
async def networth(months: int = 12) -> dict:
    return {
        "base": "EUR",
        "points": [
            {"date": d.isoformat(), "total_value": v, "cash": c, "investments": i}
            for d, v, c, i in wf_networth_series(months)
        ],
    }


@app.get("/holdings")
async def holdings() -> dict:
    return {
        "base": "EUR",
        "as_of": date.today().isoformat(),
        "totals": {"investments": WF_INVESTMENTS, "cash": WF_CASH, "total": WF_TOTAL},
        "by_asset": [
            {
                "symbol": a.symbol, "name": a.name, "asset_class": a.asset_class,
                "quantity": a.quantity, "value": a.value, "cost_basis": a.cost_basis,
                "fallback": False,
            }
            for a in WF_ASSETS
        ],
        "by_account": wf_accounts(),
    }


@app.get("/positions-history")
async def positions_history(symbol: str) -> list[dict]:
    rows = wf_position_history(symbol, months=36)
    if rows is None:
        return []
    return [
        {"date": d.isoformat(), "quantity": q, "value": v, "cost_basis": cb, "fallback": False}
        for d, q, v, cb in rows
    ]
