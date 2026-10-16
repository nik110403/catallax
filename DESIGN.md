---
name: Catallax
description: Self-hosted financial terminal — dark, dense, Bloomberg-shaped dashboard for one person's net worth and the markets around it.
colors:
  # Transcribed 1:1 from frontend/src/index.css :root (repo root cascade — sole
  # declaration site; frontend/src/theme.ts mirrors these values in JS for canvas/chart
  # libs and is drift-guarded against index.css by scripts/check-tokens.mjs, not a second
  # source). No redeclarations found anywhere else under frontend/src (grep across all
  # .css/.tsx, 2026-09-07).
  bg: "#0d1117"
  panel: "#11161d"
  panel-header: "#161b22"
  border: "#21262d"
  grid: "#1c2128"
  text: "#e6edf3"
  muted: "#838b96" # raised 2026-08-15 from #7d8590 for 4.5:1 contrast (index.css comment)
  accent: "#f0883e"
  up: "#3fb950"
  down: "#f85149"
  warn: "#d29922"
  chart-1: "#58a6ff"
  chart-2: "#f0883e"
  chart-3: "#3fb950"
  chart-4: "#db61a2"
  chart-5: "#a371f7"
  chart-6: "#e3b341"
  chart-7: "#56d4dd"
  chart-8: "#f85149"
  # The 19 above are every named `--*` colour custom property; the 4 below
  # are literal colour values that exist in `index.css` outside any custom property
  # (box-shadow / overlay-scrim backgrounds) — real, distinct values in code, not
  # invented, marked "undocumented — from <file>" (same
  # technique already used for `rounded:` below). Confirmed exhaustive via
  # `grep -n "rgba(" frontend/src/index.css`; no other literal colour values exist.
  shadow-card: "rgba(0, 0, 0, 0.5)" # undocumented — from index.css box-shadow, 5 sites (242,692,798,899,1046), the "0 18px 48px" card-elevation recipe
  shadow-card-alt: "rgba(0, 0, 0, 0.55)" # undocumented — from index.css, box-shadow line 439 ("0 12px 32px") + orientation-overlay background line 1026
  overlay-scrim: "rgba(0, 0, 0, 0.45)" # undocumented — from index.css, palette/catalog fullscreen-overlay backgrounds, lines 680 & 783
  panel-translucent: "rgba(13, 17, 23, 0.82)" # undocumented — from index.css line 542 (geo-toggle background); a translucent variant of `bg` (#0d1117), not itself a named property
typography:
  # index.css declares 2 font-family custom properties (--font-mono, --font-serif) and 5
  # font-size properties (--fs-micro/sm/base/lg/xl) but no named weight/line-height/
  # letter-spacing tokens — those values below are transcribed from the specific rules
  # that consume the size/family variables (body, .app-title/.login-wordmark, the
  # reading layer), not invented.
  body:
    fontFamily: "var(--font-mono)" # \"SF Mono\", \"Fira Code\", \"JetBrains Mono\", Consolas, monospace
    fontSize: "var(--fs-base)" # 13px
    fontWeight: 400 # not set on body{} — browser default, transcribed as such
    lineHeight: normal # not set on body{} — browser default
    letterSpacing: normal # not set on body{} — browser default
  heading:
    # from .app-title (header) and .login-wordmark (login card); both share weight +
    # letter-spacing, sizes differ — recorded, not reconciled (see Do's and Don'ts).
    fontFamily: "var(--font-mono)" # inherited from body, no override in either rule
    fontSize: "var(--fs-xl)" # 20px on .login-wordmark; .app-title uses --fs-lg (15px) instead
    fontWeight: 700
    lineHeight: normal
    letterSpacing: 0.32em
  reading:
    # frontend/src/theme.ts `reading` block + --font-serif; used only by
    # NoteView's reading surface and JournalTimeline's full-view/thesis prose — every
    # dense list/table row keeps `body` above.
    fontFamily: "var(--font-serif)" # Georgia, "Times New Roman", serif
    fontSize: "15px" # theme.ts reading.fontSize (matches --fs-lg's 15px, no shared var)
    fontWeight: 400
    lineHeight: 1.6 # theme.ts reading.lineHeight
    letterSpacing: normal
rounded:
  # No named --radius custom property exists anywhere in the system (confirmed by grep —
  # index.css uses only literal border-radius px values). These are NOT tokens the code
  # references by name; they are the distinct literal values actually in use, transcribed
  # verbatim (# undocumented — from frontend/src/index.css) so the recording reflects what
  # ships rather than inventing a scale that doesn't exist. See Do's and Don'ts.
  xs: 3px  # 1 site
  sm: 4px  # 14 sites — the de facto default
  sm-alt: 5px  # 3 sites
  md: 6px  # 3 sites
  lg: 8px  # 5 sites
---

## Overview

Catallax is a self-hosted financial terminal for one person's money and the markets
around it: net worth, holdings and ledger, plus macro, cross-asset research, equity
screening, a quant lab and a paper-trading desk, all in one keyboard-driven dashboard
(README.md). It is deliberately dark, dense and Bloomberg-shaped — "numbers per square
inch, not whitespace" — across five permanent sections (Home, Portfolio, Markets, Quant,
Live). This file is a **brownfield recording** of the design system `frontend/src/index.css`
and `frontend/src/theme.ts` already declare, not a redesign: no new tokens, no font or
colour changes were made in producing it.

## Colors

19 named colours live as CSS custom properties in `frontend/src/index.css`'s single
`:root` block, mirrored 1:1 in `frontend/src/theme.ts` (the canonical JS source for chart
libraries that can't read CSS variables) and kept in sync by
`frontend/scripts/check-tokens.mjs`. 11 are surface/semantic (`bg`, `panel`,
`panel-header`, `border`, `grid`, `text`, `muted`, `accent`, `up`/`down` for
gain/loss, `warn`), and 8 form one ordered categorical palette (`chart-1..8`) every
multi-series chart draws from by index. Four more literal colour values exist in
`index.css` outside any custom property — `shadow-card`/`shadow-card-alt` (box-shadow
alphas behind the card-elevation recipe), `overlay-scrim` (the palette/catalog
fullscreen-overlay backdrop) and `panel-translucent` (a translucent `bg` used once, on
the geo-toggle) — added to the frontmatter as `# undocumented` entries (Fix round 2) so
the count reflects every colour actually in the file, not only the named ones. One dark
theme only for beta; no light theme, no runtime switcher yet (theme.ts notes Phase 15
will add one). Border radius has no
named custom property anywhere in the system — every `border-radius` in `index.css` is a
literal px value; the `rounded:` scale above transcribes the 5 distinct values actually in
use (3/4/5/6/8px) so the detector's radius rule has something real to check against, not
a documented design decision — see Do's and Don'ts.

## Typography

Two font families (`--font-mono` for all dense chrome and numerals, `--font-serif` for
the one prose reading layer added in Phase 19) and five sizes (`--fs-micro` 11px
through `--fs-xl` 20px, with 11px held as a hard legibility floor). `body` above is the
default for effectively the entire UI — dense grid rows, tables, labels. `heading` covers
the two bold/letter-spaced wordmark-style rules (header title, login wordmark), which
share weight and tracking but not size. `reading` is deliberately narrow in scope: only
`NoteView`'s reading surface and a journal entry's full-view/thesis prose switch to it
(66ch measure, 1.6 line-height, 15px) — every dense list/table row stays on `body`.

## Elevation

No named elevation/shadow tokens exist in the system. `index.css` uses ad hoc
`box-shadow` declarations at a handful of sites (e.g. the login card's
`0 18px 48px rgba(0, 0, 0, 0.5)`); panels are otherwise distinguished by the `panel` /
`panel-header` background steps against `bg`, plus a 1px `border` — not by shadow or
elevation depth. This is a flat, dense terminal aesthetic, not a layered/elevated one.

## Components

No component-level token layer exists yet — styling is per-selector CSS in `index.css`
(class names like `.app-header`, `.app-actions button`, `.login-wordmark`) plus inline
`style={{ fontSize: theme.fontSize.xl, ... }}` reads from `theme.ts` in individual widget
`.tsx` files (`ScoreCard.tsx`, `PanelHead.tsx`, `NoteView.tsx` among them). There is no
consolidated button/card/badge token set to transcribe here; that consolidation is a
later documentation task, not this recording.

## Do's and Don'ts

- Do read `--*` custom properties from `index.css` as the single source for CSS chrome,
  and `theme.ts`'s matching JS object for anything a canvas/chart library renders — they
  are drift-guarded (`scripts/check-tokens.mjs`) and must never be edited to disagree.
- Don't add a third source of these values; extend both files together or neither.
- Don't assume `heading` has one canonical size — `.app-title` (15px) and
  `.login-wordmark` (20px) diverge and neither has been reconciled to the other; flagged
  here as a finding for a future item, not resolved by this recording.
- Don't read the `rounded:` scale above as a considered design decision — it names the
  literal `border-radius` values already in `index.css` (no named custom property backs
  any of them) purely so the detector's radius rule can run; consolidating them into an
  actual `--radius-*` token set is a later design task, not done here.
- Don't add named elevation tokens either — none exist today, and inventing one here
  would have violated the transcribe-only scope of this recording
  (rule: "transcribe, don't design").
