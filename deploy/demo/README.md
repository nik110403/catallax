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
| `preflight-demo.sh` | Fails closed on external networks, outside bind mounts, privileged, docker socket, all-interface ports, missing limits or `read_only`, unlisted `cap_add`, any `security_opt` other than exactly `no-new-privileges:true` (so `unconfined`, `label:disable` and the like), a missing, malformed or broad nginx `DEMO_TRUSTED_PROXY` (or a template mount that is not the read-only repo directory holding only the one `${DEMO_TRUSTED_PROXY} 1;` template, or a missing `demo-env` tmpfs), a missing, multi-subnet, outside-/16-/28 or proxy-overlapping `default` network subnet, a wrong demo flag, and any private marker from `DEMO_DENY_MARKERS_FILE`. The values of `DEMO_BIND_ADDR` and `DEMO_TRUSTED_PROXY` (which compose inlines into services that load the stack env file) are exempt by key name only; the same value under any other key still fails |
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
plus the market boards, then exits non-zero only if a core step failed (quotes, or any daily-bar series the risk and signals lenses read: holdings, FX, benchmarks). Optional steps (fundamentals, which ETFs do not have, boards, news, sentiment) log a warning and exit 0, so the nightly unit shows real breakage and not expected gaps. CoinGecko history requests are capped at 365 days (the free API's limit). It warms the ticker-bar feed quotes, the market-research universe bars (sector/factor/cross-asset ETFs, vol indices) and the equity-research screen universe (bars and fundamentals) as optional steps. FRED/macro series are not fetched by the warm (the demo has neither openbb-data nor a FRED key): the warm imports them from a handoff file, see "Macro handoff" below, and without that file the Macro and Rates tabs and the macro-based report components stay empty. The research screen result is recomputed as the last research step, so a degraded result an early visitor cached does not stick for its 6 h TTL. It also backfills up to two years of daily bars into PostgreSQL (`daily_bars`) through `HistoryStore`: the cache-only backend cannot backfill, so history, risk, signals and research read those stored bars. The warm job is the one process started with `UPSTREAM_MODE=live`; the backend's mode comes from `compose.demo.yml` and cannot be changed by a request.

## Deploy

Needs Docker Compose 2.24.4 or newer (`!override` in `compose.demo.yml`). `compose.sh`, and with it the preflight and every unit, checks `docker compose version` and refuses to run on anything older.

```bash
git clone <public repo url> catallax-demo && cd catallax-demo
git checkout <pushed, scanned commit>
cp deploy/demo/demo.env.example .env && chmod 600 .env   # edit passwords, DEMO_BIND_ADDR and DEMO_SUBNET (a /24 no other Docker network uses)
# One regex per line of private host/network markers; keep that file OUTSIDE the clone and
# point DEMO_DENY_MARKERS_FILE (in the stack env file) at it. The preflight fails closed without it.
deploy/demo/preflight-demo.sh
deploy/demo/compose.sh build
deploy/demo/reset-demo.sh
```

`reset-demo.sh` is the whole nightly sequence: `down`, remove the two stateful
volumes (a volume that still exists afterwards fails the reset and the trap takes the stack down), `up` (read-only, default flags, never unlocked), a write probe, then the
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
protection). The template adds an `ipAllowList` middleware on all three demo
routers, restricted to `CLOUDFLARED_SOURCE` (the CIDR of the cloudflared
connector as Traefik sees it, e.g. `CLOUDFLARED_SOURCE=203.0.113.10/32` added to
the `envsubst` call and its variable list), so nothing but the tunnel can reach
the demo through Traefik. It also adds two rate limiters keyed on `Cf-Connecting-Ip`:
`/api/*` at 10 r/s (burst 40) and static assets at 100 r/s (burst 400). The
static limiter is deliberately generous: the SPA preloads many chunks on first
load. 

The demo nginx keys its own limits on `Cf-Connecting-Ip` only when the socket
peer is inside `DEMO_TRUSTED_PROXY` (stack env file; Traefik's address as nginx
sees it, a single IPv4 CIDR no broader than /8, default `127.0.0.1/32` = nobody
useful). Every other peer, such as a LAN host or anything reaching the port
without Traefik, is keyed on its own socket address, so a forged header cannot
buy a fresh rate bucket or extra websocket slots. `compose.demo.yml` passes the
value as an nginx environment variable; the image's envsubst entrypoint renders
`nginx-templates/demo-trusted-proxy.conf.template` into the tmpfs
`/etc/nginx/demo-env/` (a compose `configs` entry cannot be used with `read_only`). The
LAN port must still never be forwarded on your router.

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

## Macro handoff

The Macro and Rates tabs read Redis keys (`macro:yc:*`, `macro:rates:US`, `macro:indicators`, `macro:calendar`, `macro:series:<ID>:toya|level`) that the demo cannot fetch. A private-side export job (kept outside this repository) reads a fixed allowlist of those keys and writes `{"version":1,"generated_at":...,"entries":{key: json}}` to `${DEMO_HANDOFF_DIR:-/srv/catallax-handoff}/macro.json` (mode 0644, daily).

`demo-warm` alone mounts that directory read-only at `/import` (`DEMO_MACRO_IMPORT=/import/macro.json`). Before the bundle recompute it re-checks every key against the same allowlist regex, validates the value with its pydantic model and writes `KEY` (normal TTL) and `KEY:stale` (7 d). The step is optional and never fails the warm: a missing file is skipped (INFO), a file older than 48 h imports with a WARNING, bad keys or values are skipped with a WARNING, and files over 5 MB or 500 entries are rejected. The backend gets no mount, and `preflight-demo.sh` allows exactly this one read-only bind, on `demo-warm` only.

The host directory must exist before `compose up`. The export timer creates it; if it is missing, docker creates it root-owned and empty (harmless: the warm then skips the import, and the root-run export later writes into it). Rollback: delete the mount from `demo-warm` and the warm silently runs without macro data.
