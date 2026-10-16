# News Feeds (Catallax)

Market news aggregator. `backend/app/services/news.py` +
`backend/app/routers/news.py`. Endpoint: `GET /api/v1/news`.

- Source list: `NEWS_FEEDS` (comma-separated RSS URLs). Default: WSJ Markets,
  Investing.com, Yahoo Finance.
- Feeds are fetched concurrently over httpx; each feed is parsed with feedparser
  in a worker thread (`asyncio.to_thread` — feedparser is blocking).
- **Per-feed failures are swallowed** — a dead/blocking feed never sinks the
  response; the endpoint returns whatever feeds responded.
- Items normalise to `NewsItem{title, link, source, published, summary}`, sorted
  newest-first, capped at `NEWS_MAX_ITEMS`.
- The merged result is cached in Redis (`news:aggregate`,
  `NEWS_CACHE_TTL_SECONDS`), so parsing only runs on cache miss/refresh.

## Add or change a feed
1. Edit `NEWS_FEEDS` in `.env` (and document in `.env.example`). No code change.
2. If a feed needs special headers/auth, extend `_fetch_feed` in
   `services/news.py` and note it here.
