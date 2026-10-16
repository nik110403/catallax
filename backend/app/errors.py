"""Domain exceptions and the standard error envelope.

All non-2xx responses carry the envelope defined in
`skills/fastapi-patterns.md`:

    { "error": { "code": "SYMBOL_NOT_FOUND", "message": "..." } }

Adapters/services raise `CatallaxError` subclasses; handlers never build
envelopes by hand. `register_exception_handlers(app)` wires them up so raw
upstream errors (yfinance tracebacks, Fava HTML/BQL) never leak to clients —
detailed messages are logged server-side; clients get a stable `public_message`
where one is defined.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("catallax.errors")


class CatallaxError(Exception):
    """Base for all domain errors. Carries a stable machine code + HTTP status."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    # When set, the client sees this instead of the (possibly detail-bearing)
    # internal message. The internal message is still logged server-side.
    public_message: str | None = None

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        super().__init__(self.message)

    def envelope(self) -> dict[str, dict[str, str]]:
        return {
            "error": {
                "code": self.code,
                "message": self.public_message or self.message,
            }
        }


class SymbolNotFound(CatallaxError):
    """No data for the requested symbol."""

    code = "SYMBOL_NOT_FOUND"
    status_code = 404


class RateLimitExceeded(CatallaxError):
    """Upstream rate limit exhausted; retry later."""

    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429


class UpstreamError(CatallaxError):
    """An upstream data source failed or returned an unusable response."""

    code = "UPSTREAM_ERROR"
    status_code = 502
    public_message = "Upstream data source error"


class SidecarNotEnabled(CatallaxError):
    """A profile-gated sidecar could not be reached at all (nothing listening).

    Distinct from `UpstreamError`: this fires only on a connect-level failure
    (refused/no route/DNS) — the sidecar container is very likely just not
    running because its compose profile was never started, as opposed to
    the sidecar answering with a bad response (still `UpstreamError`).
    """

    code = "SIDECAR_NOT_ENABLED"
    status_code = 503

    def __init__(self, service: str, profile: str) -> None:
        self.service = service
        self.profile = profile
        super().__init__(
            f"{service} unreachable (connect failure) — sidecar not running?"
        )
        self.public_message = (
            f"{service} is not enabled — start it with "
            f"`docker compose --profile {profile} up -d`"
        )


class LedgerUnavailable(CatallaxError):
    """The Beancount/Fava ledger backend could not be reached."""

    code = "LEDGER_UNAVAILABLE"
    status_code = 503
    public_message = "Ledger backend unavailable"


class AdapterNotFound(CatallaxError):
    """Requested price source adapter is not registered."""

    code = "ADAPTER_NOT_FOUND"
    status_code = 400


class InvalidRequest(CatallaxError):
    """A request parameter failed validation."""

    code = "INVALID_REQUEST"
    status_code = 400


class InvalidEvidenceRef(CatallaxError):
    """A note evidence reference is invalid or cannot be resolved."""

    code = "INVALID_EVIDENCE_REF"
    status_code = 422


class NotFound(CatallaxError):
    """A requested resource does not exist."""

    code = "NOT_FOUND"
    status_code = 404
    # Don't reflect the (caller-supplied) resource key back in the client message.
    public_message = "Resource not found"


class DemoModeReadOnly(CatallaxError):
    """Not available in demo mode."""

    code = "DEMO_MODE_READONLY"
    status_code = 403


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CatallaxError)
    async def _catallax_handler(_: Request, exc: CatallaxError) -> JSONResponse:
        # Log the full internal message; the client may see only public_message.
        if exc.status_code >= 500:
            logger.warning("%s: %s", exc.code, exc.message)
        return JSONResponse(exc.envelope(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Keep structured detail (type/loc/msg) but strip the submitted input
        # values so a malformed request can't reflect its own payload back.
        details = [
            {k: v for k, v in err.items() if k not in ("input", "ctx", "url")}
            for err in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        # Last-resort guard: keep every non-2xx on the envelope, never leak a
        # traceback or upstream text to the client.
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
            status_code=500,
        )
