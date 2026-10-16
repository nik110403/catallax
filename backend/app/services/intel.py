"""News intelligence engine — RSS → grounded enrich → cluster → rows.

Pure orchestration: it pulls the existing RSS feed, narrates each article through
the F3 seam (numbers/facts computed here, model only tags + summarises), then
clusters deterministically by topic overlap. NO Redis/DB here — the IntelService
owns the job lifecycle + persistence. `enrich_item` never raises: a failed claude
call yields an `unverifiable` Enrichment so the article is still recorded.
"""
from __future__ import annotations

import hashlib
import json
import logging

from pydantic import BaseModel, Field

from app.adapters.llm.base import Grounding, LLMMessage, LLMProviderAdapter, Role
from app.services.news import NewsItem

logger = logging.getLogger("catallax.intel")

_TASK = (
    "Classify this news article. Reply ONLY as JSON "
    '{"text": <1-sentence summary>, "grounding": "grounded"|"speculative"|"unverifiable", '
    '"sentiment": "positive"|"neutral"|"negative", "sentiment_score": <-1..1>, '
    '"topics": [<=4 short lowercase tags], "country": <name or null>, '
    '"lat": <number or null>, "lon": <number or null>}. '
    "Summary + sentiment are your inference → grounding \"speculative\". "
    "Only the title/source/date are facts."
)


class Enrichment(BaseModel):
    sentiment: str | None = None
    sentiment_score: float | None = None
    topics: list[str] = Field(default_factory=list)
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    summary: str | None = None
    grounding: str = Grounding.unverifiable.value


class IntelEngine:
    async def enrich_item(self, adapter: LLMProviderAdapter, item: NewsItem) -> Enrichment:
        facts = {
            "title": item.title,
            "source": item.source,
            "published": item.published.isoformat() if item.published else None,
            "summary": item.summary,
        }
        resp = await adapter.complete(
            [LLMMessage(role=Role.user, content=_TASK)], facts=facts
        )
        if resp.warnings or not resp.text:
            return Enrichment(grounding=Grounding.unverifiable.value)
        data = resp.data if isinstance(resp.data, dict) else None
        if data is None:
            try:
                data = json.loads(resp.text)
            except (ValueError, TypeError):
                # model answered in prose — keep it as the summary, mark unverifiable
                return Enrichment(summary=resp.text[:400], grounding=Grounding.unverifiable.value)
            if not isinstance(data, dict):
                # valid JSON but not an object (list/null/number/string) — untrusted model output
                return Enrichment(summary=resp.text[:400], grounding=Grounding.unverifiable.value)
        topics_raw = data.get("topics")
        return Enrichment(
            sentiment=_clean_sentiment(data.get("sentiment")),
            sentiment_score=_clamp(data.get("sentiment_score")),
            topics=[str(t).lower()[:32] for t in (topics_raw if isinstance(topics_raw, list) else [])][:4],
            country=(str(data["country"])[:64] if data.get("country") else None),
            lat=_coord(_num(data.get("lat")), -90.0, 90.0),
            lon=_coord(_num(data.get("lon")), -180.0, 180.0),
            summary=(str(data.get("text"))[:400] if data.get("text") else None),
            grounding=(resp.grounding.value if hasattr(resp.grounding, "value") else str(resp.grounding)),
        )


def _clean_sentiment(v: object) -> str | None:
    return v if v in ("positive", "neutral", "negative") else None


def _clamp(v: object) -> float | None:
    try:
        return max(-1.0, min(1.0, float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _num(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coord(v: float | None, lo: float, hi: float) -> float | None:
    return None if v is None else max(lo, min(hi, v))


def cluster(rows: list[dict]) -> list[dict]:
    """Assign a stable `cluster_id` per row by topic-overlap (union-find, deterministic).

    Two articles sharing ≥1 topic tag join the same cluster. Topic-less articles are
    singletons. cluster_id = sha1(smallest member link)[:16] — stable, no RNG, fits
    the String(36) DB column."""
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    topic_first: dict[str, int] = {}
    for i, r in enumerate(rows):
        for t in (r.get("topics") or []):
            if t in topic_first:
                union(i, topic_first[t])
            else:
                topic_first[t] = i

    out: list[dict] = []
    for i, r in enumerate(rows):
        root = find(i)
        key = rows[root].get("link") or root
        cid = hashlib.sha1(str(key).encode()).hexdigest()[:16]
        r = {**r, "cluster_id": cid}
        out.append(r)
    return out
