import pytest

from app.adapters.llm.base import Grounding, LLMResponse
from app.services.intel import Enrichment, IntelEngine
from app.services.news import NewsItem


class _FakeAdapter:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, messages, *, facts=None) -> LLMResponse:
        return LLMResponse(
            text=self._text, provider="fake", grounding=Grounding.speculative, warnings=[]
        )


def _item() -> NewsItem:
    return NewsItem(title="t", link="l", source="s", published=None, summary=None)


async def test_enrich_non_dict_json_no_raise():
    engine = IntelEngine()
    out = await engine.enrich_item(_FakeAdapter("[1,2,3]"), _item())
    assert isinstance(out, Enrichment)
    assert out.grounding == "unverifiable"


async def test_enrich_non_list_topics_no_raise():
    engine = IntelEngine()
    out = await engine.enrich_item(
        _FakeAdapter('{"text":"hi","sentiment":"positive","topics":42}'), _item()
    )
    assert isinstance(out, Enrichment)
    assert out.topics == []


async def test_enrich_out_of_range_coords_clamped():
    engine = IntelEngine()
    out = await engine.enrich_item(_FakeAdapter('{"text":"x","lat":9999,"lon":-9999}'), _item())
    assert out.lat == 90
    assert out.lon == -180


async def test_enrich_uses_structured_data():
    class _FakeStructuredAdapter:
        async def complete(self, messages, *, facts=None) -> LLMResponse:
            return LLMResponse(
                text="summary",
                provider="fake",
                grounding=Grounding.speculative,
                warnings=[],
                data={
                    "text": "summary",
                    "sentiment": "positive",
                    "sentiment_score": 0.3,
                    "topics": ["fed", "inflation"],
                    "country": "United States",
                    "lat": 38.89,
                    "lon": -77.03,
                },
            )

    engine = IntelEngine()
    out = await engine.enrich_item(_FakeStructuredAdapter(), _item())
    assert out.sentiment == "positive"
    assert out.topics == ["fed", "inflation"]
    assert out.country == "United States"
    assert out.lat == pytest.approx(38.89)
    assert out.lon == pytest.approx(-77.03)
    assert out.grounding == "speculative"
