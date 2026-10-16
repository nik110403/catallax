# Hosted read-only demo: deployment

How to run the public Catallax demo from a clone of this repository. The demo
shows the synthetic persona only. It is read-only at three layers and never
calls an upstream data source on a visitor's request.

## What is in here

| File | Purpose |
|---|---|
| `compose.demo.yml` | Overlay on `docker-compose.yml`: own project `catallax-demo`, memory/CPU limits, read-only root filesystems, `no-new-privileges`, `cap_drop: ALL`, heavy services switched off, the warm job |
| `nginx.demo.conf` | Demo nginx: 20 r/s (burst 40) limit on `/api/`, 405 for non-read methods outside `/api/` |
| `compose.sh` | Compose wrapper with the project name and file order pinned |
| `preflight-demo.sh` | Fails closed on external networks, outside bind mounts, privileged, docker socket, all-interface ports, missing limits, a wrong demo flag, and any private marker |
| `reset-demo.sh` + `catallax-demo-reset.{service,timer}` | Nightly 03:30 reset |
| `catallax-demo-warm.{service,timer}` | Cache warm job schedule |
| `traefik-catallax-demo.yml.tmpl` | Edge proxy routers, method allowlist, rate limiters |
| `demo.env.example` | Environment template (demo values only) |

## Layers

1. Edge (Traefik): only `GET`, `HEAD` and `OPTIONS` reach the app; everything else is answered 405.
2. App (`DEMO_READONLY=true`): any other method on `/api/v1/*` returns `403 {"error":"read_only_demo"}`.
3. Data: the sample ledger is mounted read-only, and the nightly reset rebuilds PostgreSQL and Redis from the seed.

Upstream calls: the backend runs `UPSTREAM_MODE=cache-only`. A cache miss returns
the stale cached value or an empty result, and never fetches. The warm job
(`python -m app.demo_warm`, run through the `demo-warm` compose service) is the
only caller; it fetches the persona's symbols (`DEMO_WARM_SYMBOLS` overrides)
plus the market boards, then exits.

## Deploy

Needs Docker Compose 2.24 or newer.

```bash
git clone <public repo url> catallax-demo && cd catallax-demo
git checkout <pushed, scanned commit>
cp deploy/demo/demo.env.example .env && chmod 600 .env   # edit passwords and DEMO_BIND_ADDR
deploy/demo/preflight-demo.sh
deploy/demo/compose.sh build
deploy/demo/reset-demo.sh
```

`reset-demo.sh` brings the stack up and seeds it (seeding needs writes, so it
starts the backend with `DEMO_READONLY=false`, then recreates it read-only), probes that a POST now
returns 403, and runs the warm job. The same script is the nightly reset.

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
protection). The template adds two rate limiters keyed on `Cf-Connecting-Ip`:
`/api/*` at 10 r/s (burst 40) and static assets at 100 r/s (burst 400). The
static limiter is deliberately generous: the SPA preloads many chunks on first
load. The LAN port must never be forwarded on your router.

## Roll back

`deploy/demo/compose.sh down -v` in the clone removes the demo stack and its volumes and touches nothing else.

## Not verified yet

Written without starting containers. The capability sets (`cap_add` on nginx,
postgres, redis) and `read_only` root filesystems follow each image's documented
needs but were not exercised; if a container fails to start on first `up`, relax
only that service's line and note it here.
