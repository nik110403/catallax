"""Research note PDF using the weekly pack's Jinja and Chromium print engine."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.services.report_pdf import _environment, _FOOTER_TEMPLATE, resolve_pdf_path


def render_note_html(note: dict[str, Any], redacted: bool) -> str:
    """Render escaped note content; no HTML from user fields is trusted."""
    return _environment().get_template("note.html").render(note=note, redacted=redacted)


async def render_note_pdf(note: dict[str, Any], redacted: bool, out_dir: str) -> str:
    """Write a print-ready PDF beneath out_dir and return its contained path."""
    from playwright.async_api import (
        async_playwright,
    )  # lazy: API imports without Chromium

    slug = note.get("slug", "")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("invalid note slug for PDF filename")
    base = Path(out_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    # Slugs derive from original titles; a redacted filename must not expose one.
    identifier = hashlib.sha256(slug.encode()).hexdigest()[:24] if redacted else slug
    filename = f"catallax-note-{identifier}{'-redacted' if redacted else ''}.pdf"
    path = resolve_pdf_path(str(base / filename), str(base))
    html = render_note_html(note, redacted)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            await page.emulate_media(media="print")
            await page.pdf(
                path=path,
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=_FOOTER_TEMPLATE,
                margin={
                    "top": "24mm",
                    "bottom": "20mm",
                    "left": "15mm",
                    "right": "15mm",
                },
            )
        finally:
            await browser.close()
    return resolve_pdf_path(path, str(base))
