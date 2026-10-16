"""Session router — public boot info for the frontend: demo-mode flag, owner
greeting name, and auth-gate state. Always public (never behind the share
gate — see Task 2's middleware exemption list) so a locked-out visitor can
still learn whether a passphrase is needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth import is_authenticated
from app.config import Settings, get_settings

router = APIRouter(prefix="/session", tags=["session"])


class SessionInfo(BaseModel):
    demo_mode: bool
    owner_name: str | None
    auth_required: bool
    authenticated: bool


@router.get("")
async def session(
    request: Request, settings: Settings = Depends(get_settings)
) -> SessionInfo:
    auth_required = bool(settings.share_passphrase.get_secret_value())
    authenticated = is_authenticated(request, settings)
    # The greeting name is owner-only data. This route is public by design (a
    # locked-out visitor must be able to learn that a passphrase is needed), so
    # the name is withheld from anyone who has not passed the gate — and from
    # everyone in demo mode. With no passphrase configured, `authenticated` is
    # DERIVED (not proven by a cookie) and always true — so `owner_name` is
    # published to anyone who reaches this port. Intended for a single-user
    # loopback box (see the SHARE_PASSPHRASE comment in .env.example), but
    # undocumented behaviour otherwise: flag before binding this beyond
    # loopback with an empty passphrase.
    # (`/session` must not reveal the owner name pre-login.)
    return SessionInfo(
        demo_mode=settings.demo_mode,
        owner_name=(
            None if settings.demo_mode or not authenticated else settings.owner_name
        ),
        auth_required=auth_required,
        authenticated=authenticated,
    )
