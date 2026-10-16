// Locale-explicit number/date formatting — the SINGLE place this choice
// lives. Every widget-local `fmt`/`fmt0`/`pct`/`eur` helper (and the two
// bare date formatters) called `toLocaleString`/`toLocaleTimeString` with
// `undefined` for the locale, which means the VIEWER's browser locale
// decided the separator convention — the same portfolio total rendered
// "12,352.67" for one person and "12.352,67" for another. This is a
// financial terminal shown to other people; the numbers must read
// identically for every viewer.
//
// en-GB: comma thousands separator, dot decimal ("1,234.56") — the
// convention the rest of the UI already assumes for mono tabular figures.
// The reporting currency is EUR; this is a separator choice, not a claim
// the app is British. A future change to the convention is a one-line
// edit here.
export const LOCALE = "en-GB";

// 24-hour clock — a terminal should not render "2:05 PM".
const CLOCK_OPTS: Intl.DateTimeFormatOptions = { hour12: false };

// Date + time, e.g. "15/08/2026, 14:05:12".
export function formatDateTime(d: Date | string | number): string {
  return new Date(d).toLocaleString(LOCALE, CLOCK_OPTS);
}

// Time only, e.g. "14:05:12".
export function formatTime(d: Date | string | number): string {
  return new Date(d).toLocaleTimeString(LOCALE, CLOCK_OPTS);
}
