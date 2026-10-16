from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.adapters.events.base import PollResult, RawEvent, SourceAdapter, Universe

logger = logging.getLogger(__name__)
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKERS_KEY = "events:edgar:tickers"
_TICKERS_TTL = 86400
_VAL_PREFIX = "events:edgar:val:"
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_INDEX = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}-index.html"
)
_DATE_FORMS = frozenset({"8-K", "6-K"})


class _TokenBucket:
    def __init__(self, rate: float) -> None:
        self.rate = max(float(rate), 1e-9)
        self.capacity = 1.0
        self.tokens = 1.0
        self.stamp = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(
                self.capacity, self.tokens + (now - self.stamp) * self.rate
            )
            self.stamp = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) / self.rate)
                self.tokens = 0
                self.stamp = time.monotonic()
            else:
                self.tokens -= 1


def _pad_cik(value: object) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits.zfill(10) if digits else ""


def _parse_acceptance(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _base_form(form: str) -> tuple[str, str]:
    original = (form or "").strip()
    mapped = original
    upper = original.upper()
    if upper.startswith("SCHEDULE "):
        rest = original.split(None, 1)[1]
        mapped = "SC " + rest
        upper = mapped.upper()
    base = mapped[:-2].rstrip() if upper.endswith("/A") else mapped
    return original, base


def _val_key(url: str) -> str:
    return _VAL_PREFIX + hashlib.sha256(url.encode()).hexdigest()


def _ticker_map(cached: Any) -> dict[str, str]:
    if not isinstance(cached, dict):
        return {}
    source = cached["body"] if "body" in cached else cached
    if not isinstance(source, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in source.items()
        if k not in {"body", "etag", "last_modified"}
    }


def _bundle_validators(bundle: dict[str, Any] | None) -> dict[str, str]:
    if not bundle or bundle.get("body") is None:
        return {}
    out: dict[str, str] = {}
    if bundle.get("etag"):
        out["etag"] = str(bundle["etag"])
    if bundle.get("last_modified"):
        out["last_modified"] = str(bundle["last_modified"])
    return out


class EdgarSourceAdapter(SourceAdapter):
    source = "sec_edgar"
    feed = "edgar"

    def __init__(self, settings: Any, redis: Any, universe: Universe) -> None:
        self.rate_limit_per_min = settings.sec_edgar_rate_limit_per_sec * 60
        self._settings = settings
        self._redis = redis
        self._universe = universe
        self._bucket = _TokenBucket(settings.sec_edgar_rate_limit_per_sec)
        self._forms = {
            item.strip() for item in settings.sec_edgar_forms.split(",") if item.strip()
        }
        self._missing: set[str] = set()
        self._ua = settings.sec_edgar_user_agent
        self._timeout = settings.events_http_timeout_seconds
        self._max_items = settings.events_max_items_per_feed
        self._mem: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._ua, "Accept-Encoding": "gzip"}

    async def _cache_get(self, key: str) -> Any | None:
        if self._redis is not None:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        return self._mem.get(key)

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        if self._redis is not None:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        else:
            self._mem[key] = value

    async def _load_bundle(self, key: str) -> dict[str, Any] | None:
        data = await self._cache_get(key)
        if isinstance(data, dict) and "body" in data:
            return data
        return None

    async def _save_bundle(self, key: str, body: Any, response: httpx.Response) -> None:
        bundle: dict[str, Any] = {"body": body}
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if etag:
            bundle["etag"] = etag
        if last_modified:
            bundle["last_modified"] = last_modified
        await self._cache_set(key, bundle, _TICKERS_TTL)

    def _conditional(self, validators: dict[str, str]) -> dict[str, str]:
        extra: dict[str, str] = {}
        if validators.get("etag"):
            extra["If-None-Match"] = validators["etag"]
        if validators.get("last_modified"):
            extra["If-Modified-Since"] = validators["last_modified"]
        return extra

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        deadline: float,
        extra: dict[str, str],
    ) -> httpx.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()

        async def op() -> httpx.Response:
            await self._bucket.take()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            response = await client.get(
                url, headers=extra, timeout=min(float(self._timeout), left)
            )
            if response.status_code == 304:
                return response
            response.raise_for_status()
            return response

        return await asyncio.wait_for(op(), timeout=remaining)

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        deadline: float,
        key: str,
        *,
        require_body: bool = False,
    ) -> tuple[httpx.Response, Any | None]:
        bundle = await self._load_bundle(key)
        body = None if bundle is None else bundle.get("body")
        usable = body is not None and (not require_body or bool(body))
        extra = self._conditional(_bundle_validators(bundle) if usable else {})
        response = await self._request(client, url, deadline, extra)
        if response.status_code != 304:
            return response, None
        if usable:
            return response, body
        return await self._request(client, url, deadline, {}), None

    async def _tickers(
        self, client: httpx.AsyncClient, deadline: float
    ) -> dict[str, str]:
        cached = await self._cache_get(_TICKERS_KEY)
        mapping = _ticker_map(cached)
        try:
            response, cached_body = await self._get(
                client, _TICKERS_URL, deadline, _TICKERS_KEY, require_body=True
            )
        except (TimeoutError, httpx.TimeoutException):
            if mapping:
                return mapping
            raise TimeoutError()
        if response.status_code == 304:
            replayed = _ticker_map(
                {"body": cached_body} if cached_body is not None else {}
            )
            if replayed:
                return replayed
            if mapping:
                return mapping
            raise TimeoutError()
        payload = response.json()
        mapping = {}
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper()
            cik = _pad_cik(row.get("cik_str"))
            if ticker and cik:
                mapping[ticker] = cik
        await self._save_bundle(_TICKERS_KEY, mapping, response)
        return mapping

    def _resolve_ciks(self, tickers: dict[str, str]) -> list[tuple[str, str]]:
        found: dict[str, str] = {}
        for symbol in sorted(self._universe.symbols):
            token = symbol.upper()
            cik = _pad_cik(self._universe.cik_map.get(token) or tickers.get(token))
            if not cik:
                if token not in self._missing:
                    logger.info("edgar skip symbol=%s no CIK", token)
                    self._missing.add(token)
                continue
            found.setdefault(cik, token)
        return [(symbol, cik) for cik, symbol in found.items()]

    def _order(
        self, pairs: list[tuple[str, str]], cursor: dict[str, Any]
    ) -> list[tuple[str, str]]:
        def key(item: tuple[str, str]) -> tuple[int, str]:
            prev = (cursor or {}).get(item[1])
            if not prev:
                return (0, item[1])
            return (1, str(prev))

        return sorted(pairs, key=key)

    def _events_for(
        self, payload: dict[str, Any], symbol: str, cik: str, cursor: dict[str, Any]
    ) -> tuple[list[RawEvent], str | None]:
        seen = datetime.now(timezone.utc)
        name = str(payload.get("name") or symbol)
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        acceptances = recent.get("acceptanceDateTime") or []
        reports = recent.get("reportDate") or []
        filings = recent.get("filingDate") or []
        n = min(len(forms), len(accessions), len(acceptances))
        matched: list[tuple[datetime, RawEvent]] = []
        for i in range(n):
            original, base = _base_form(str(forms[i]))
            if base not in self._forms:
                continue
            accepted = _parse_acceptance(
                acceptances[i] if i < len(acceptances) else None
            )
            if accepted is None:
                continue
            accession = str(accessions[i])
            report = str(reports[i] if i < len(reports) else "") or str(
                filings[i] if i < len(filings) else ""
            )
            if base in _DATE_FORMS:
                source_key = f"{cik}:{base}:{report}"
            else:
                source_key = f"{cik}:{base}:{accession}"
            url = _INDEX.format(
                cik=int(cik), acc_nodash=accession.replace("-", ""), accession=accession
            )
            matched.append(
                (
                    accepted,
                    RawEvent(
                        source=self.source,
                        feed=self.feed,
                        source_key=source_key,
                        event_type="filing",
                        form_type=original,
                        title=f"{original} — {name}",
                        summary=None,
                        url=url,
                        published_at=accepted,
                        first_seen_at=seen,
                        raw={"accession": accession},
                        entity_hints=[
                            ("cik", cik, "cik_map"),
                            ("symbol", symbol, "cik_map"),
                        ],
                    ),
                )
            )
        matched.sort(key=lambda item: item[0], reverse=True)
        prev = (cursor or {}).get(cik)
        if prev:
            floor = _parse_acceptance(str(prev))
            if floor is not None:
                floor = floor - timedelta(days=2)
                matched = [item for item in matched if item[0] >= floor]
            cap = self._max_items
        else:
            cap = min(10, self._max_items)
        matched = matched[:cap]
        newest = matched[0][0].isoformat() if matched else None
        events = [item[1] for item in matched]
        for event in events:
            event.first_seen_at = seen
        return events, newest

    async def poll(
        self, cursor: dict[str, Any], etag: str | None, last_modified: str | None
    ) -> PollResult:
        start = time.monotonic()
        deadline = start + max(self._settings.events_adapter_timeout_seconds - 10, 0)
        out_cursor = dict(cursor or {})
        events: list[RawEvent] = []
        failures = 0
        unmodified = True
        got_response = False
        async with httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers(), follow_redirects=True
        ) as client:
            try:
                tickers = await self._tickers(client, deadline)
            except TimeoutError:
                return PollResult(
                    events=[],
                    cursor=out_cursor,
                    etag=etag,
                    last_modified=last_modified,
                )
            pairs = self._order(self._resolve_ciks(tickers), out_cursor)
            for symbol, cik in pairs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                url = _SUBMISSIONS.format(cik=cik)
                key = _val_key(url)
                try:
                    response, cached_body = await self._get(client, url, deadline, key)
                except TimeoutError:
                    break
                except (httpx.TimeoutException, httpx.ConnectError):
                    failures += 1
                    if failures >= 3:
                        raise
                    continue
                failures = 0
                got_response = True
                if response.status_code == 304:
                    payload = cached_body
                    if not isinstance(payload, dict):
                        continue
                else:
                    unmodified = False
                    payload = response.json()
                cik_events, newest = self._events_for(payload, symbol, cik, out_cursor)
                if response.status_code != 304:
                    await self._save_bundle(key, payload, response)
                events.extend(cik_events)
                if newest:
                    out_cursor[cik] = newest
        return PollResult(
            events=events,
            cursor=out_cursor,
            etag=etag,
            last_modified=last_modified,
            not_modified=got_response and unmodified,
        )


def build(settings: Any, redis: Any, universe: Universe | None) -> list[SourceAdapter]:
    if not (settings.sec_edgar_user_agent or "").strip() or universe is None:
        return []
    return [EdgarSourceAdapter(settings, redis, universe)]
