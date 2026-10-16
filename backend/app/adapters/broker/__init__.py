"""Broker adapter seam (Phase 13, paper-trading algo hub).

`BrokerAdapter` (an ABC mirroring `PriceSourceAdapter` / `BacktestEngineAdapter`)
+ a `factory`, with `paper` (`PaperBroker`) the only enabled engine — it
fill-simulates against the existing F2 price chain rather than talking to a
real venue. `nautilus` and `alpaca-paper` are REGISTERED but not enabled, to
prove the seam is a framework and document the drop-in path for a real
paper/live venue integration.

Order/position/fill-simulation logic (the actual paper book) lives in
`services/algo.py` (Task 3); this seam covers only what a real venue would
replace: mark-to-market quotes and a liveness check.
"""
