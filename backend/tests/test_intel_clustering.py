from app.services.intel import cluster


def test_cluster_groups_by_topic_overlap():
    rows = [
        {"link": "a", "topics": ["fed", "rates"]},
        {"link": "b", "topics": ["rates", "inflation"]},
        {"link": "c", "topics": ["oil", "opec"]},
    ]
    out = cluster(rows)
    by_link = {r["link"]: r["cluster_id"] for r in out}
    # a and b share "rates" → same cluster; c is alone
    assert by_link["a"] == by_link["b"]
    assert by_link["c"] != by_link["a"]
    assert all(r["cluster_id"] for r in out)


def test_cluster_no_topics_each_alone():
    rows = [{"link": "a", "topics": []}, {"link": "b", "topics": []}]
    out = cluster(rows)
    assert out[0]["cluster_id"] != out[1]["cluster_id"]


def test_cluster_is_deterministic():
    rows = [{"link": "a", "topics": ["x"]}, {"link": "b", "topics": ["x"]}]
    assert [r["cluster_id"] for r in cluster(rows)] == [r["cluster_id"] for r in cluster(rows)]


def test_cluster_id_fits_db_column():
    rows = [
        {"link": "https://example.com/2026/07/09/some-long-article-slug-here", "topics": ["fed", "rates"]},
        {"link": "https://example.com/2026/07/09/another-very-long-article-slug", "topics": ["rates", "inflation"]},
        {"link": "https://example.com/2026/07/09/yet-another-extremely-long-slug-for-oil-news", "topics": ["oil", "opec"]},
    ]
    out = cluster(rows)
    assert all(len(r["cluster_id"]) <= 36 for r in out)
    by_link = {r["link"]: r["cluster_id"] for r in out}
    assert by_link[rows[0]["link"]] == by_link[rows[1]["link"]]
