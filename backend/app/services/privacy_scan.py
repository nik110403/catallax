"""Deterministic, conservative privacy scan for shareable research notes.

Offsets refer to the original input. Redaction uses the same non-overlapping
matches, in reverse order, so a second scan can enforce a clean export.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

_NUMBER = r"\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d{4,}(?:[.,]\d{1,2})?|\d{1,3}(?:[.,]\d{1,2})?"
_EUR = re.compile(
    rf"(?<!\w)(?:€\s*(?:{_NUMBER})\s*[kKmM]?|(?:{_NUMBER})\s*[kKmM]?\s*(?:€|EUR\b))",
    re.I,
)
_BARE = re.compile(
    r"(?<![\w./-])\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|(?<![\w./-])\d{4,}(?:[.,]\d{1,2})?",
    re.I,
)
_SHARES = re.compile(
    rf"(?<!\w)(?:{_NUMBER})\s*(?:shares?|units?)\b|\b(?:shares?|units?|quantity|qty)\s*[:=]?\s*(?:{_NUMBER})(?!\w)",
    re.I,
)
_NET = re.compile(
    rf"\bnet\s*worth\s*(?:[:=]|is|of)?\s*(?:€\s*)?(?:{_NUMBER})\s*[kKmM]?\b", re.I
)
_MONEY_TERMS = re.compile(
    r"\b(?:balance|cash|portfolio|nav|value|cost|paid|worth|assets?|holdings?|proceeds?|salary|account|capital|invested|profit|loss)\b",
    re.I,
)
_EXCLUDE = re.compile(
    r"\b(?:brier|ratio|score|percent|probability|date|year|price|target|rate)\b", re.I
)
_LABEL_BOUNDARY = re.compile(r"[;.!?\n]|,(?!\d)")
_ACCOUNT_PATH = re.compile(r"\bAssets(?::[\w-]+){2,}\b", re.I)
_MD_MARKS = re.compile(r"[*_~`]")


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Strip Markdown emphasis/code markers, collapse whitespace runs to a
    single space, and casefold — while keeping an index map from each
    normalized-text position back to the original-text offset it came from,
    so a match found only after normalization can still redact the exact
    (obfuscated) original span. `len(index_map) == len(out) + 1`; each
    output char maps to the input index it was derived from (a casefold that
    expands to multiple chars maps all of them to that same input index)."""
    out: list[str] = []
    index_map: list[int] = []
    in_space_run = False
    for i, ch in enumerate(text):
        if _MD_MARKS.match(ch) or unicodedata.category(ch) == "Cf":
            continue
        if ch.isspace():
            if not in_space_run:
                out.append(" ")
                index_map.append(i)
                in_space_run = True
            continue
        in_space_run = False
        folded = ch.casefold()
        out.extend(folded)
        index_map.extend([i] * len(folded))
    index_map.append(len(text))
    return "".join(out), index_map


def scan(text: str, settings: Any, *, extra_labels: Iterable[str] = ()) -> dict[str, Any]:
    """Return ordered rule/match/offset findings and a clean flag."""
    candidates: list[dict[str, Any]] = []

    def add(rule: str, start: int, end: int) -> None:
        if start < end:
            candidates.append(
                {"rule": rule, "match": text[start:end], "offset": start, "end": end}
            )

    for rule, regex in (
        ("net_worth", _NET),
        ("eur_amount", _EUR),
        ("share_count", _SHARES),
    ):
        for hit in regex.finditer(text):
            add(rule, *hit.span())

    for hit in _BARE.finditer(text):
        digits = re.sub(r"\D", "", hit.group().split(".")[0].split(",")[0])
        # Parse grouped thousands and decimal-free integers, never four-digit years/dates.
        normalized = re.sub(r"[\s.,]", "", hit.group())
        if not normalized.isdigit() or int(normalized) < 1000:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", hit.group()) or (
            hit.start() and text[hit.start() - 1] in "-/"
        ):
            continue
        preceding = text[max(0, hit.start() - 35) : hit.start()]
        label = _LABEL_BOUNDARY.split(preceding)[-1]
        if _MONEY_TERMS.search(label) and not _EXCLUDE.search(label):
            add("bare_money", *hit.span())

    labels = getattr(settings, "advisor_clearing_accounts_list", []) or []
    configured = getattr(settings, "notes_account_names", "") or ""
    labels = [
        *labels,
        *(part.strip() for part in configured.split(",")),
        *extra_labels,
    ]
    for label in labels:
        if isinstance(label, str) and label.strip():
            for hit in re.finditer(
                r"(?<!\w)" + re.escape(label.strip()) + r"(?!\w)", text, re.I
            ):
                add("account_name", *hit.span())
    # Markdown/whitespace obfuscation (e.g. "Trade **Republic**") evades the
    # literal match above but not a normalized one; inserted after the
    # literal loop so an exact-span duplicate always loses the tie to the
    # plain `account_name` finding (candidates are sorted by (offset,
    # -length) with a stable sort, so insertion order breaks ties).
    normalized_labels = [
        label.strip() for label in labels if isinstance(label, str) and label.strip()
    ]
    if normalized_labels:
        normalized_text, index_map = _normalize_with_map(text)
        for label in normalized_labels:
            normalized_label, _ = _normalize_with_map(label)
            if not normalized_label:
                continue
            for hit in re.finditer(
                r"(?<!\w)" + re.escape(normalized_label) + r"(?!\w)", normalized_text
            ):
                add(
                    "account_label_obfuscated",
                    index_map[hit.start()],
                    index_map[hit.end()],
                )
    for hit in re.finditer(
        r"\b(?:WF[:_-]?portfolio_snapshot[:_-]?[\w-]+|portfolio_snapshot[:#]\s*\w[\w-]*)\b",
        text,
        re.I,
    ):
        add("account_name", *hit.span())
    # Beancount-style account paths remain recognizable without explicit config.
    for hit in _ACCOUNT_PATH.finditer(text):
        add("account_name", *hit.span())
    owner = getattr(settings, "owner_name", "")
    if isinstance(owner, str) and owner.strip():
        for hit in re.finditer(
            r"(?<!\w)" + re.escape(owner.strip()) + r"(?!\w)", text, re.I
        ):
            add("owner_name", *hit.span())

    # Longest first on a shared start; priority is the explicit rules above.
    candidates.sort(key=lambda item: (item["offset"], -(item["end"] - item["offset"])))
    findings: list[dict[str, Any]] = []
    last = 0
    for item in candidates:
        if item["offset"] >= last:
            findings.append(item)
            last = item["end"]
    return {
        "rules": sorted({item["rule"] for item in findings}),
        "findings": findings,
        "clean": not findings,
    }


def redact(
    text: str, settings: Any, *, extra_labels: Iterable[str] = ()
) -> tuple[str, dict[str, Any]]:
    result = scan(text, settings, extra_labels=extra_labels)
    for item in reversed(result["findings"]):
        text = text[: item["offset"]] + "[REDACTED]" + text[item["end"] :]
    return text, result
