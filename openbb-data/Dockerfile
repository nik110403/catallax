# OpenBB Platform REST API — Catallax macro-data sidecar.
# Runs OpenBB's REST API as an isolated service (internal network only); the
# Catallax backend reaches it via MacroAdapter over HTTP. Same sidecar pattern
# as Fava / the Wealthfolio exporter — keeps AGPL at arm's length (network
# boundary, no vendoring) and the backend image lean.
#
# Free providers only. Paid-key providers are intentionally omitted; FRED uses a
# free key supplied at runtime via OPENBB_FRED_API_KEY (env), federal_reserve /
# ecb / econdb / yfinance need no key.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Router extensions (economy + fixedincome) + free providers only. Pinned to the
# tested OpenBB 1.6.x matrix for reproducible builds (bump deliberately).
RUN pip install \
        "openbb-core==1.6.13" \
        "openbb-economy==1.6.2" \
        "openbb-fixedincome==1.6.2" \
        "openbb-federal-reserve==1.6.2" \
        "openbb-fred==1.6.2" \
        "openbb-ecb==1.6.1" \
        "openbb-econdb==1.5.1" \
        "openbb-yfinance==1.6.3" \
        "uvicorn[standard]==0.40.0"

# Assemble + smoke-check the REST app at build time so a broken extension set
# fails the build, not the first request.
RUN python -c "from openbb_core.api.rest_api import app; print('OpenBB REST routes:', len(app.routes))"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV OPENBB_API_PORT=6900
EXPOSE 6900
ENTRYPOINT ["/entrypoint.sh"]
