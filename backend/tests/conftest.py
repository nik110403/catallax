"""Env defaults so the suite runs without a `.env` (Settings reads process env only)."""
import os

for _k, _v in {
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test",
    "REDIS_PASSWORD": "test",
    "DEMO_MODE": "false",
    "LLM_PROVIDER": "noop",
    "OWNER_NAME": "Owner",
}.items():
    os.environ.setdefault(_k, _v)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
