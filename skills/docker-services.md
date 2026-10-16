# Docker Services (Catallax)

Reference for every service in `docker-compose.yml`. Inter-service calls use
the service name as hostname on the default compose network.

## Default stack

| Service | Image/Build | Internal Port | Host Port | Purpose | Healthcheck | Volumes |
|---|---|---|---|---|---|---|
| nginx | nginx:1.27-alpine | 80 | `${NGINX_HOST_PORT}` (8090, bound to `${NGINX_BIND_ADDR}`, default loopback) | Reverse proxy, single entry point | `wget --spider /api/v1/health` | `./nginx/nginx.conf` (ro) |
| fastapi-backend | `./backend` | 8000 | none | API; the only gateway to Fava and the exporter | python urllib on `/api/v1/health` | sample ledger (ro), `report_pdfs` (ro), `note_pdfs` (rw) |
| react-frontend | `./frontend` | 80 | none | nginx-served production build of the SPA | `wget --spider /` | none |
| postgres | postgres:16-alpine | 5432 | none | App database | `pg_isready` | `postgres-data` |
| redis | redis:7-alpine | 6379 | none | Cache, rate limiting, pub/sub (password from `REDIS_PASSWORD`) | `redis-cli ping` | `redis-data` |
| fava | yegle/fava | 5001 | none | Beancount UI/API, serves `${BEANCOUNT_FILE}` under `--prefix /fava` | none | `./data/sample.beancount` (ro) |
| report-worker | `./backend` + `Dockerfile.report` | none | none | Weekly report-pack worker (`python -m app.report_worker`) | heartbeat-file age check | `report_pdfs` (rw) |
| stub-exporter | `./backend` (`app.stub_exporter`) | 8089 | none | Serves the demo persona's net worth and holdings in the wire format the portfolio bridge expects | python urllib on `/health` | none |

Two helper pieces: `notes-mount-init` is a one-shot busybox container that creates the
`/data/reports/notes` mountpoint inside the `report_pdfs` volume (Docker cannot create a
nested mountpoint under a read-only parent), and `macro-net` is a second network shared by
the backend and report-worker.

## Data sources in the public stack

- **Ledger:** `data/sample.beancount` is mounted read-only into the backend and Fava. The
  backend stats the file's mtime for cache invalidation; queries go through Fava's API.
- **Portfolio:** the backend talks to a portfolio exporter over HTTP. In this repository
  that is the `stub-exporter` service, which serves a fixed demo persona. Pointing the
  backend at a real exporter only needs the exporter URL setting changed.

## Opt-in profiles

These services are off by default and start only with their profile:

| Service | Profile | Purpose |
|---|---|---|
| openbb-data | `macro` | OpenBB REST sidecar for macro/economic series, on `macro-net` only |
| backtester | `backtest` | Off-API backtest worker (`python -m app.worker`); jobs via Redis, results in the `backtests` table |
| qlib-lab | `lab` | Heavy Quant/ML worker (`Dockerfile.lab`); jobs via Redis, results in `lab_runs` |
| trading-engine | `algo` | Paper-trading tick loop (`python -m app.algo_worker`); single worker, do not scale replicas |

Enable one with e.g. `docker compose --profile backtest up -d`. Workers that share the
`./backend` image need a rebuild together with `fastapi-backend` after a backend code change.
See `skills/backtest-engine.md`, `skills/quant-lab.md`, `skills/algo-trading.md`.

## Notes on the report-worker

- It starts with the default stack; no profile needed.
- The weekly pack builds its factor study, intel enrichment and PDF in-process, with no
  dependency on the `lab` profile.
- `LLM_PROVIDER` is pinned to `noop` here, so packs render without narration and are
  visibly marked as un-narrated.
- PDFs go to the `report_pdfs` volume; `fastapi-backend` mounts the same volume read-only
  to serve them.
- It overrides the image CMD (no `alembic upgrade`), so it waits for `fastapi-backend` to be
  healthy before starting.
- It has its own heavy image (Playwright/Chromium), so the daily backend image carries no
  browser. Its healthcheck is a heartbeat-file staleness test, since it has no HTTP surface.
- Any service that imports `MacroAdapter` (directly or via `services/market_research.py` or
  `services/report_pack.py`) must join `macro-net`. The network is declared at top level,
  so it exists even with the `macro` profile off, and the adapter then degrades cleanly.
- After recreating `react-frontend` or `fastapi-backend`, restart `nginx` too: a long-running
  nginx keeps the old upstream address and returns 502.

## Inter-service hostnames

From inside any container: `http://fastapi-backend:8000`, `http://fava:5001`,
`postgres:5432`, `redis:6379`. The backend reaches Fava and the exporter through
configuration settings (`FAVA_BASE_URL` and the exporter URL), never literals.

## Healthcheck pattern

- Every long-running service defines a healthcheck.
- Dependents use `depends_on: {service: {condition: service_healthy}}` so startup order is
  enforced (the backend waits for postgres, redis and the stub exporter; nginx waits for the backend).
- HTTP services: `wget -q --spider <local health URL>` (alpine/busybox) or a python
  `urllib` one-liner where wget isn't in the image.

## How to add a new service

1. Add a compose entry: image/build, an env file, healthcheck, `depends_on` with
   health conditions, named volume if stateful.
2. Ports: pick the next free internal port; expose to the host only if humans need direct
   access (everything else goes through nginx).
3. New environment variables go into the example env file, grouped and commented
   (`SERVICE_PURPOSE` names).
4. If browser-reachable: add one `upstream` and one `location` block in `nginx/nginx.conf`
   (never rewrite existing blocks).
5. Update the tables in this file and in `README.md`.
