"""Note privacy, print, and export boundary tests (no live DB required)."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import get_settings
from app.errors import register_exception_handlers
from app.redis import get_redis
from app.routers import notes as routes
from app.routers.networth import get_wealthfolio
from app.services.note_pdf import render_note_html, render_note_pdf
from app.services.privacy_scan import redact, scan


OWNER = "Avery"  # placeholder persona token for owner-name tests


def settings(**kw):
    from app.config import Settings

    kw.setdefault("share_passphrase", "")
    kw.setdefault("demo_mode", False)
    kw.setdefault("owner_name", OWNER)

    return Settings(
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        redis_password="r",
        **kw,
    )


@pytest.mark.parametrize(
    "text,rule",
    [
        ("€1,200.50", "eur_amount"),
        ("1.2k EUR", "eur_amount"),
        ("1500€", "eur_amount"),
        ("cash balance 1,200", "bare_money"),
        ("qty: 142", "share_count"),
        ("142 shares", "share_count"),
        ("Assets:Broker:Clearing", "account_name"),
        ("WF_portfolio_snapshot_42", "account_name"),
        (OWNER, "owner_name"),
        ("net worth 55000", "net_worth"),
    ],
)
def test_rules(text, rule):
    found = scan(text, settings(advisor_clearing_accounts="Assets:Broker:Clearing"))
    assert rule in found["rules"]
    assert found["findings"][0]["offset"] >= 0
    cleaned, _ = redact(
        text, settings(advisor_clearing_accounts="Assets:Broker:Clearing")
    )
    assert scan(cleaned, settings(advisor_clearing_accounts="Assets:Broker:Clearing"))[
        "clean"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Brier 0.18 and ratio 1.4",
        "65% probability",
        "2026-09-25",
        "AAPL up 42%",
        "target price 1200",
    ],
)
def test_research_figures_survive(text):
    assert scan(text, settings())["clean"]
    assert redact(text, settings())[0] == text


def test_brier_after_cash_does_not_hide_amount():
    text = "Cash balance 1,200; Brier score 0.18"
    cleaned, result = redact(text, settings())
    assert result["rules"] == ["bare_money"]
    assert cleaned == "Cash balance [REDACTED]; Brier score 0.18"
    assert scan(cleaned, settings())["clean"]


def test_configured_and_recognizable_accounts_redacted():
    config = settings(notes_account_names="Brokerage Alpha,Second Bank")
    text = "Brokerage Alpha / Assets:Bank:Example; ratio 1.4"
    cleaned, result = redact(text, config)
    assert result["rules"] == ["account_name"]
    assert cleaned == "[REDACTED] / [REDACTED]; ratio 1.4"
    assert scan(cleaned, config)["clean"]


def test_order_offsets_and_retention():
    original = {
        "note": {
            "slug": "safe-note",
            "kind": "thesis",
            "status": "draft",
            "title": "Research",
            "one_sentence": f"{OWNER} paid €1200",
            "body": "Brier 0.18",
        },
        "evidence": [{"kind": "url", "id": "https://example.org", "label": OWNER}],
    }
    clean, summary = routes._export_note(original, settings(), True)
    assert clean["one_sentence"] == "[REDACTED] paid [REDACTED]"
    assert clean["evidence"][0]["label"] == "[REDACTED]"
    assert original["note"]["one_sentence"] == f"{OWNER} paid €1200"
    assert summary["rules"] == {"eur_amount": 1, "owner_name": 2}
    assert OWNER not in render_note_html(clean, True)
    assert "Brier 0.18" in render_note_html(clean, True)


def test_residual_scan_fails_closed_when_strict(monkeypatch):
    monkeypatch.setattr(
        routes,
        "redact",
        lambda value, config, extra_labels=(): (value, scan(value, config)),
    )
    with pytest.raises(HTTPException) as error:
        routes._export_note({"note": {"title": "€1200"}}, settings(), True)
    assert error.value.status_code == 409
    assert error.value.detail["rule"] == "eur_amount"
    assert error.value.detail["offset"] == 0


def test_residual_scan_can_be_disabled_for_controlled_export(monkeypatch):
    monkeypatch.setattr(
        routes,
        "redact",
        lambda value, config, extra_labels=(): (value, scan(value, config)),
    )
    note, summary = routes._export_note(
        {"note": {"title": "€1200"}},
        settings(notes_privacy_scan_strict=False),
        True,
    )
    assert note["title"] == "€1200"
    assert summary["rules"] == {"eur_amount": 1}


def test_html_order_and_escaping():
    note = {
        "title": "<private>",
        "kind": "thesis",
        "status": "draft",
        "one_sentence": "First",
        "why_it_matters": "Second",
        "action": "Third",
        "risk_invalidation": "Fourth",
        "body": "Fifth",
        "evidence": [{"kind": "url", "id": "x", "label": "<script>"}],
        "scan_counts": {"owner_name": 1},
    }
    html = render_note_html(note, True)
    assert (
        html.index("First")
        < html.index("Second")
        < html.index("Third")
        < html.index("Fourth")
        < html.index("Fifth")
        < html.index("Evidence")
    )
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "REDACTED EXPORT" in html and "owner_name: 1" in html


def test_html_evidence_is_a_table():
    class EvidenceTable(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = False
            self.cell = None
            self.rows = []

        def handle_starttag(self, tag, attrs):
            if tag == "table" and ("class", "evidence") in attrs:
                self.in_table = True
            elif self.in_table and tag == "tr":
                self.rows.append([])
            elif self.in_table and tag in ("th", "td"):
                self.cell = [tag, ""]

        def handle_data(self, data):
            if self.cell is not None:
                self.cell[1] += data

        def handle_endtag(self, tag):
            if self.cell is not None and tag == self.cell[0]:
                self.rows[-1].append(tuple(self.cell))
                self.cell = None
            elif tag == "table":
                self.in_table = False

    html = render_note_html(
        {
            "title": "Research",
            "kind": "thesis",
            "status": "draft",
            "evidence": [{"kind": "url", "id": "long-identifier", "label": "<source>"}],
        },
        True,
    )
    table = EvidenceTable()
    table.feed(html)
    assert table.rows == [
        [("th", "Kind"), ("th", "ID"), ("th", "Label")],
        [("td", "url"), ("td", "long-identifier"), ("td", "<source>")],
    ]
    assert "&lt;source&gt;" in html and "<source>" not in html


@pytest.mark.asyncio
async def test_pdf_path_contained_and_traversal_rejected(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    class Page:
        async def set_content(self, html, **kw):
            self.html = html

        async def emulate_media(self, **kw):
            pass

        async def pdf(self, *, path, **kw):
            Path(path).write_bytes(b"%PDF-1.4\n")

    class Browser:
        async def new_page(self):
            return Page()

        async def close(self):
            pass

    class Chromium:
        async def launch(self, **kw):
            return Browser()

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *args):
            pass

    module = ModuleType("playwright.async_api")
    module.async_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    payload = {
        "slug": "safe-note",
        "title": "Test",
        "kind": "thesis",
        "status": "draft",
        "one_sentence": "Safe",
        "evidence": [],
    }
    result = await render_note_pdf(payload, True, str(tmp_path))
    assert Path(result).parent == tmp_path and Path(result).read_bytes().startswith(
        b"%PDF"
    )
    with pytest.raises(ValueError):
        await render_note_pdf({**payload, "slug": "../escape"}, True, str(tmp_path))
    import hashlib

    ident = hashlib.sha256(b"linked").hexdigest()[:24]
    (tmp_path / f"catallax-note-{ident}-redacted.pdf").symlink_to(
        tmp_path.parent / "outside.pdf"
    )
    with pytest.raises(ValueError):
        await render_note_pdf({**payload, "slug": "linked"}, True, str(tmp_path))


class _PoisonBridge:
    """A WealthfolioBridge stand-in that fails the test if ever touched."""

    async def holdings(self):
        raise AssertionError("Wealthfolio called before the gate")


def test_demo_export_routes_gate_before_data_access(monkeypatch):
    class FailingService:
        def __init__(self, *args):
            raise AssertionError("DB read before demo gate")

    def fail_pdf(*args, **kwargs):
        raise AssertionError("PDF generated before demo gate")

    monkeypatch.setattr(routes, "NotesService", FailingService)
    monkeypatch.setattr(routes, "render_note_pdf", fail_pdf)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_settings] = lambda: settings(demo_mode=True)
    app.dependency_overrides[get_redis] = lambda: None
    app.dependency_overrides[get_wealthfolio] = _PoisonBridge

    with TestClient(app) as client:
        post = client.post("/api/v1/notes/test/export")
        get = client.get("/api/v1/notes/test/export.pdf")
    assert post.status_code == 403
    assert post.json()["error"]["code"] == "DEMO_MODE_READONLY"
    assert get.status_code == 404
    assert get.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_export_gate_and_demo_do_not_read_note(monkeypatch):
    class FailingService:
        def __init__(self, *args):
            raise AssertionError("DB read before gate")

    monkeypatch.setattr(routes, "NotesService", FailingService)
    request = SimpleNamespace()
    with pytest.raises(HTTPException) as denied:
        await routes._export("x", False, request, settings(), None, _PoisonBridge())
    assert denied.value.status_code == 403
    with pytest.raises(Exception) as demo:
        await routes._export(
            "x", True, request, settings(demo_mode=True), None, _PoisonBridge()
        )
    assert demo.value.__class__.__name__ == "NotFound"


@pytest.mark.asyncio
async def test_export_redacted_default_and_unredacted_requires_authenticated_gate(
    monkeypatch, tmp_path
):
    payload = {
        "note": {
            "slug": "owner-note",
            "title": f"{OWNER} note",
            "kind": "thesis",
            "status": "draft",
            "one_sentence": "€1200",
        },
        "evidence": [],
    }

    class Service:
        def __init__(self, *args):
            pass

        async def get(self, slug):
            return payload

    observed = []

    async def render(note, redacted, out_dir):
        observed.append((note, redacted))
        path = tmp_path / "safe.pdf"
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    class DummyBridge:
        async def holdings(self):
            return SimpleNamespace(by_account=[SimpleNamespace(account="Dummy Bank")])

    monkeypatch.setattr(routes, "NotesService", Service)
    monkeypatch.setattr(routes, "render_note_pdf", render)
    config = settings(notes_export_dir=str(tmp_path))
    request = SimpleNamespace(cookies={})
    result = await routes.export_note(
        request,
        slug="owner-note",
        settings=config,
        redis=None,
        wealthfolio=DummyBridge(),
    )
    assert result["redacted"] is True and result["scan"]["rules"] == {
        "eur_amount": 1,
        "owner_name": 1,
    }
    assert observed[0][0]["title"] == "[REDACTED] note"
    assert payload["note"]["title"] == f"{OWNER} note"
    with pytest.raises(HTTPException) as denied:
        await routes._export(
            "owner-note",
            False,
            request,
            settings(
                share_passphrase="long-safe-passphrase", notes_export_dir=str(tmp_path)
            ),
            None,
            DummyBridge(),
        )
    assert denied.value.status_code == 403
    monkeypatch.setattr(routes, "is_authenticated", lambda req, config: True)
    allowed = await routes._export(
        "owner-note",
        False,
        request,
        settings(
            share_passphrase="long-safe-passphrase", notes_export_dir=str(tmp_path)
        ),
        None,
        DummyBridge(),
    )
    assert allowed["redacted"] is False and observed[-1][0]["title"] == f"{OWNER} note"
    download = await routes.download_note_pdf(
        request,
        slug="owner-note",
        settings=config,
        redis=None,
        wealthfolio=DummyBridge(),
    )
    assert download.media_type == "application/pdf"


def test_extra_labels_redacted_like_configured_accounts():
    text = "my Acme Broker account"
    found = scan(text, settings(), extra_labels=["Acme Broker"])
    assert "account_name" in found["rules"]
    cleaned, _ = redact(text, settings(), extra_labels=["Acme Broker"])
    assert "Acme Broker" not in cleaned


def test_markdown_obfuscated_account_label_is_still_caught():
    text = "Moved into Acme **Broker** today"
    found = scan(text, settings(), extra_labels=["Acme Broker"])
    assert "account_label_obfuscated" in found["rules"]
    cleaned, _ = redact(text, settings(), extra_labels=["Acme Broker"])
    assert "Acme" not in cleaned and "Broker" not in cleaned
    assert scan(cleaned, settings(), extra_labels=["Acme Broker"])["clean"]
    zw = "Moved into Acme\u200b Bro\u200dker today"
    assert (
        "account_label_obfuscated"
        in scan(zw, settings(), extra_labels=["Acme Broker"])["rules"]
    )
    # figures survive normalization too — this isn't just a literal-match bypass.
    assert scan("Brier 0.18 and ratio 1.4", settings())["clean"]


def test_obfuscated_account_label_does_not_reach_redacted_export():
    original = {
        "note": {
            "slug": "safe-note",
            "kind": "thesis",
            "status": "draft",
            "title": "Research",
            "one_sentence": "ok",
            "body": "Moved into Acme **Broker** today",
        },
        "evidence": [],
    }
    clean, summary = routes._export_note(
        original, settings(), True, ("Acme Broker",)
    )
    assert "Acme" not in clean["body"] and "Broker" not in clean["body"]
    assert "account_label_obfuscated" in summary["rules"]
    html = render_note_html(clean, True)
    assert "Broker" not in html


@pytest.mark.asyncio
async def test_export_threads_real_account_labels_from_wealthfolio(
    monkeypatch, tmp_path
):
    payload = {
        "note": {
            "slug": "wf-note",
            "title": "WF note",
            "kind": "thesis",
            "status": "draft",
            "one_sentence": "ok",
            "body": (
                "Moved €12,500 from Acme Broker to ExampleBank; cash balance 48.300"
            ),
        },
        "evidence": [],
    }

    class Service:
        def __init__(self, *args):
            pass

        async def get(self, slug):
            return payload

    class Bridge:
        async def holdings(self):
            return SimpleNamespace(
                by_account=[
                    SimpleNamespace(account="Acme Broker"),
                    SimpleNamespace(account="ExampleBank"),
                ]
            )

    captured = []

    async def render(note, redacted, out_dir):
        captured.append(note)
        path = tmp_path / "safe.pdf"
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    monkeypatch.setattr(routes, "NotesService", Service)
    monkeypatch.setattr(routes, "render_note_pdf", render)
    config = settings(notes_export_dir=str(tmp_path))
    request = SimpleNamespace(cookies={})
    result = await routes._export("wf-note", True, request, config, None, Bridge())
    note = captured[0]
    for needle in ("Acme Broker", "ExampleBank", "12,500", "48.300"):
        assert needle not in note["body"]
    assert "account_name" in result["scan"]["rules"]
    assert {"eur_amount", "bare_money"} & set(result["scan"]["rules"])


@pytest.mark.asyncio
async def test_redacted_export_fails_closed_when_labels_unavailable(
    monkeypatch, tmp_path
):
    class Service:
        def __init__(self, *args):
            raise AssertionError("note should not be read before the label fetch fails")

    class BrokenBridge:
        async def holdings(self):
            raise RuntimeError("upstream down")

    rendered = []
    monkeypatch.setattr(routes, "NotesService", Service)
    monkeypatch.setattr(routes, "render_note_pdf", lambda *a, **kw: rendered.append(1))
    config = settings(notes_export_dir=str(tmp_path))
    request = SimpleNamespace(cookies={})
    with pytest.raises(HTTPException) as error:
        await routes._export("wf-note", True, request, config, None, BrokenBridge())
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "PRIVACY_LABELS_UNAVAILABLE"}
    assert not rendered


@pytest.mark.asyncio
async def test_redacted_export_fails_closed_when_labels_are_empty(
    monkeypatch, tmp_path
):
    class Service:
        def __init__(self, *args):
            raise AssertionError("note should not be read when the label set is empty")

    class EmptyBridge:
        async def holdings(self):
            return SimpleNamespace(by_account=[])

    rendered = []
    monkeypatch.setattr(routes, "NotesService", Service)
    monkeypatch.setattr(routes, "render_note_pdf", lambda *a, **kw: rendered.append(1))
    config = settings(notes_export_dir=str(tmp_path))
    request = SimpleNamespace(cookies={})
    with pytest.raises(HTTPException) as error:
        await routes._export("wf-note", True, request, config, None, EmptyBridge())
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "PRIVACY_LABELS_UNAVAILABLE"}
    assert not rendered


@pytest.mark.asyncio
async def test_unredacted_export_proceeds_when_labels_unavailable(
    monkeypatch, tmp_path
):
    payload = {
        "note": {
            "slug": "wf-note",
            "title": "WF note",
            "kind": "thesis",
            "status": "draft",
            "one_sentence": "ok",
        },
        "evidence": [],
    }

    class Service:
        def __init__(self, *args):
            pass

        async def get(self, slug):
            return payload

    class BrokenBridge:
        async def holdings(self):
            raise RuntimeError("upstream down")

    async def render(note, redacted, out_dir):
        path = tmp_path / "safe.pdf"
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    monkeypatch.setattr(routes, "NotesService", Service)
    monkeypatch.setattr(routes, "render_note_pdf", render)
    monkeypatch.setattr(routes, "is_authenticated", lambda req, config: True)
    config = settings(
        notes_export_dir=str(tmp_path), share_passphrase="long-safe-passphrase"
    )
    request = SimpleNamespace(cookies={})
    result = await routes._export(
        "wf-note", False, request, config, None, BrokenBridge()
    )
    assert result["redacted"] is False


def test_list_bounds():
    with pytest.raises(ValidationError):
        routes.NoteCreateRequest(
            kind="thesis", title="x", one_sentence="y", evidence_refs=[{}] * 101
        )
    with pytest.raises(ValidationError):
        routes.NoteUpdateRequest(subjects=["x"] * 101)
