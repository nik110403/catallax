# Hosted read-only demo: deployment

How to run the public Catallax demo from a clone of this repository. The demo
shows the synthetic persona only. It is read-only at three layers and never
calls an upstream data source on a visitor's request.

## What is in here

| File | Purpose |
|---|---|
| `compose.demo.yml` | Overlay on `docker-compose.yml`: own project `catallax-demo`, memory/CPU limits, read-only root filesystems, `no-new-privileges`, `cap_drop: ALL`, heavy services and fava switched off (profile `never`), the warm job |
| `nginx.demo.conf` | Demo nginx: 20 r/s (burst 40) limit on `/api/`, at most 2 price websockets per client, 405 for non-read methods outside `/api/`, 404 for `/fava/`, upstreams re-resolved per request (a recreated container never leaves a stale IP) |
| `compose.sh` | Compose wrapper with the project name and file order pinned |
| `preflight-demo.sh` | Fails closed on external networks, outside bind mounts, privileged, docker socket, all-interface ports, missing limits or `read_only`, unlisted `cap_add`, `unconfined` profiles, a wrong demo flag, and any private marker from `DEMO_DENY_MARKERS_FILE` |
| `reset-demo.sh` + `catallax-demo-reset.{service,timer}` | Nightly 03:30 reset (read-only throughout, never seeded) |
| `catallax-demo-warm.{service,timer}` | Cache warm job schedule |
| `traefik-catallax-demo.yml.tmpl` | Edge proxy routers, method allowlist, rate limiters |
| `demo.env.example` | Environment template (demo values only) |

## Layers

1. Edge (Traefik): only `GET`, `HEAD` and `OPTIONS` reach the app; everything else is answered 405.
2. App (`DEMO_READONLY=true`): any other method on `/api/v1/*` returns `403 {"error":"read_only_demo"}`.
3. Data: the sample ledger is mounted read-only, and the nightly reset wipes PostgreSQL and Redis. The demo is not seeded and never unlocked.

Upstream calls: the backend runs `UPSTREAM_MODE=cache-only`. A cache miss returns
the stale cached value or an empty result, and never fetches. The warm job
(`python -m app.demo_warm`, run through the `demo-warm` compose service) is the
only caller; it fetches the persona's symbols (`DEMO_WARM_SYMBOLS` overrides)
plus the market boards, then exits non-zero if any step failed. It also backfills two years of daily bars into PostgreSQL (`daily_bars`) through `HistoryStore`: the cache-only backend cannot backfill, so history, risk, signals and research read those stored bars. The warm job is the one process started with `UPSTREAM_MODE=live`; the backend's mode comes from `compose.demo.yml` and cannot be changed by a request.

## Deploy

Needs Docker Compose 2.24 or newer.

```bash
git clone <public repo url> catallax-demo && cd catallax-demo
git checkout <pushed, scanned commit>
cp deploy/demo/demo.env.example .env && chmod 600 .env   # edit passwords and DEMO_BIND_ADDR
# One regex per line of private host/network markers; keep that file OUTSIDE the clone and
# point DEMO_DENY_MARKERS_FILE (in the stack env file) at it. The preflight fails closed without it.
deploy/demo/preflight-demo.sh
deploy/demo/compose.sh build
deploy/demo/reset-demo.sh
```

`reset-demo.sh` is the whole nightly sequence: `down`, remove the two stateful
volumes, `up` (read-only, default flags, never unlocked), a write probe, then the
warm job. The probe POSTs to `/api/v1/watchlist` from inside the backend
container (`compose.sh exec fastapi-backend`, its own port 8000, so no extra
port is published) and must get 403. If anything fails before the probe passes,
the trap runs `down`; a stack that is not provably read-only does not serve.
Config comes from the stack env file, not the caller's shell. A `flock` on
`.demo-lock` keeps reset and the warm unit from overlapping.

### What the demo lacks without seeding

Spec E5 assumed `bin/seed-demo.sh` would pre-fill the lab, backtest, report and
news-intel tabs. That script refuses `DEMO_MODE=true` (everything is read-only),
and the demo has no unlocked window, so those tabs start empty: lab runs,
backtests, reports, journal, notes, user-created signals, the watchlist and
news-intel results. Tabs fed by the stub exporter, the warm job (quotes,
boards, news, fundamentals, daily bars) and the persona code stay populated.
Building demo-safe seed data is a separate task.

Install the timers (replace `@STACK_DIR@` with the clone path):

```bash
for u in reset warm; do
  for t in service timer; do
    sed "s|@STACK_DIR@|$PWD|g" deploy/demo/catallax-demo-$u.$t | sudo tee /etc/systemd/system/catallax-demo-$u.$t >/dev/null
  done
done
sudo systemctl daemon-reload
sudo systemctl enable --now catallax-demo-reset.timer catallax-demo-warm.timer
```

Warm schedule: every 15 minutes from 13:00 to 21:45 UTC on weekdays (covers
the US session in both EST and EDT), hourly otherwise. Quote and board caches
keep a two-day stale copy, so a missed run shows older prices, not an empty page.

## Edge proxy

```bash
HOSTNAME=demo.example.org BACKEND_URL=http://<lan-address>:8190 EDGE_CHAIN=<your-chain>@file \
  envsubst '${HOSTNAME} ${BACKEND_URL} ${EDGE_CHAIN}' \
  < deploy/demo/traefik-catallax-demo.yml.tmpl > catallax-demo.yml
```

`EDGE_CHAIN` is your existing middleware chain (security headers, HSTS, bot
protection). The template adds two rate limiters keyed on `Cf-Connecting-Ip`; the demo nginx keys its own limits on the same header (falling back to the socket address when it is empty):
`/api/*` at 10 r/s (burst 40) and static assets at 100 r/s (burst 400). The
static limiter is deliberately generous: the SPA preloads many chunks on first
load. `Cf-Connecting-Ip` is trustworthy only because the demo port is reachable solely through the tunnel/edge path, so the LAN port must never be forwarded on your router.

## Memory budget

nginx 32M, frontend 64M, backend 512M, postgres 256M, redis 64M, stub-exporter
128M, notes-mount-init 16M (one-shot), plus the warm job (`demo-warm`) at 256M
while it runs: about 1.1 GB steady, 1.4 GB peak. Fava (128M) is not started.

## Roll back

`deploy/demo/compose.sh down -v` in the clone removes the demo stack and its volumes and touches nothing else.

## Not verified yet

Written without starting containers. The capability sets (`cap_add` on nginx,
postgres, redis) and `read_only` root filesystems follow each image's documented
needs but were not exercised; if a container fails to start on first `up`, relax
only that service's line and note it here.
