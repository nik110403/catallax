# Case study: Catallax

Catallax started as a way to see my net worth, my holdings and my Beancount ledger in one
place, next to the market data I was checking anyway. Between June and October 2026 it grew
into the terminal in this repository: a FastAPI backend, a React frontend, Postgres and Redis,
a report worker that renders a weekly PDF, and opt-in containers for macro data, backtesting,
a factor and model lab, and paper trading. It runs on my home network for one user. This page
describes the design choices that held up, how the public copy was cut out of the private
one, and how the work was split between me and AI coding agents.

## The problem

Personal finance software tends to fall into two camps. Budgeting apps know your accounts
and nothing about markets, and market terminals know markets and nothing about your accounts.
I wanted both on one screen, with the portfolio as the starting point: what I hold, how
concentrated it is, what a bad week in 2008 or 2020 would do to it, and what the rate and
macro picture says about the next few months. I also wanted it to stay mine. The data lives
on my own hardware, the app speaks only to the LAN, and nothing in it can place a real order.

## Design choices

**The bridge seam.** The backend never reads my portfolio directly. A separate exporter,
running next to the portfolio tracker, answers three read-only endpoints (`/networth`,
`/holdings`, `/positions-history`), and one class in `services/wealthfolio.py` is the only
code that talks to it. Every analytics module (risk, the advisor, signals, the report pack)
asks that bridge. Before publishing I checked that claim with a full-repository search: the
exporter URL is read in exactly one place. That single seam is what made the public copy
possible. `backend/app/stub_exporter.py` speaks the same wire format from a synthetic
persona, and a parity test points the real bridge at the stub and compares its output field
by field with the in-process demo fixtures.

**The demo seam.** `DEMO_MODE` swaps the portfolio and ledger bridges for fixtures and turns
every write into a 403. It was built so I could show the app to someone without showing my
money. For this repository it turned out to be the wrong tool, because demo mode also blanks
the analytics that need a database: no backtests, no journal, no signal history. The public
build therefore runs in normal mode against the stub exporter, and `bin/seed-demo.sh` fills
the database through the public API.

**The report pipeline.** A separate worker builds a weekly report pack in seven chapters from
the advisor's snapshot, runs a factor study in-process, and renders the
result to PDF with Playwright. In the private deployment a language model writes the
narrative, but only from an allowlist of facts the worker assembles first, so a number in the
prose has to exist in the data. Every failure marks the pack as failed with a reason, and the
next tick retries up to a limit. In this copy the narration step is off and the packs render
without prose.

**The privacy scan.** Research notes can be exported and shared, so `services/privacy_scan.py`
redacts euro amounts, share counts, net-worth statements and account paths before export,
then scans the result a second time and refuses an export that still matches. It is
deterministic and errs on the side of redacting too much, since a false positive costs a
reworded note and a false negative leaks a balance.

**Paper trading only.** The trading desk has no broker credentials, no order routing and no
wallet signing. I decided early that no bug in a personal project should be able to move real
money, and the easiest way to guarantee that was to leave the code path out entirely.

## What was removed, and why

The public repository is a copy, built from an allowlist of 339 paths exported from one
recorded commit of the private repository. Nothing was copied from a working tree, so local
secrets, the ledger and untracked files could not slip in.

Three kinds of material came out. The first was infrastructure: host names, IP addresses,
the internal domain, reverse-proxy and VPN details, and the sync scripts that move data
between machines. The second was the AI features. The news enrichment, the research cockpit
and the report narration all call Claude through credentials mounted from the host. I removed
the credential mounts from the compose file itself, because a disabled flag still leaves the
credentials inside the container. The adapter code ships, switched off.

The third kind took the most care: my own financial biography, written into comments and
defaults. A risk band was tuned to my actual crypto share, the tax default matched the
capital-gains rate where I live, the quiet hours for alerts sat in my time zone, and several comments
explained a setting by describing my income or what my ledger holds. The rule for these was
to keep the comment that explains the mechanism and drop the sentence that explains why it
suits me. Defaults became neutral values marked as configurable. The same rule covered the
symbol-alias examples, which had mirrored real holdings and now use common tickers.

The gate before publishing was a term scan built from the private sources at run time (names,
ledger accounts, payees, held symbols, amounts in exact and rounded forms, hosts, secrets and
paths), kept in memory only and run against the exact commit being pushed. The screenshots
were captured only from the stub stack. Any hit that was not on a short list of
expected matches stopped the push. A reviewer with no prior context then read the scrubbed
files for biography that a term list cannot catch. Its first pass blocked the commit with
eight findings, among them a comment that still gave away my crypto share. The builds ran on a separate rootless
Docker daemon, so the copy could never attach to the volumes or networks of the stack I
actually use.

## How the work was split

Catallax has one author. I decided what to build, in which order, and how each part should
behave, and I wrote those decisions down as phased plans with acceptance criteria before the code
for each phase was written. Claude Code agents did most of the implementation work against those plans,
and separate review agents checked the results against the plan, read diffs for security
problems, and judged the interface. The planning documents are not in this copy, but their
size gives a sense of the balance: over the private history about 18,600 lines were added to
plans, against about 36,000 lines of backend code and 28,000 lines of frontend code.

The private repository has 389 commits between 11 June and 13 October 2026: 26 in June, 54 in
July, 119 in August, 156 in September and 34 in the first half of October. 314 of them carry an
AI co-author trailer. That number shows how heavily the tooling was used. It does not show who
wrote which line, and I would not read it as a split of the work.

| Area | Lines added | Lines deleted |
|---|---:|---:|
| Backend application | 35,778 | 2,563 |
| Frontend | 27,930 | 4,095 |
| Backend tests | 21,293 | 698 |
| Planning documents | 18,578 | 2,997 |
| Docs and per-area conventions (`skills/`) | 8,387 | 180 |
| Database migrations | 1,413 | 0 |

## Limits

This is a single-user LAN app. It speaks plain HTTP, has a passphrase gate and no user
accounts, and has never had to survive hostile traffic, which is why there is no hosted demo.
The copy is a frozen snapshot: it will not get fixes, and the market-data adapters depend on
public pages that change without notice. Several of them send a browser-like User-Agent, so
anyone running it beyond personal research should check each source's terms first. The
backend suite passes without a `.env` (1006 passed, 55 skipped when no Postgres is
reachable), but the frontend tests were not run for this copy. Finally, the stub persona makes
every screen look plausible, so the screenshots show what the software does and say nothing
about investment results.
