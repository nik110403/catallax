// Phase 14d — the five permanent, URL-addressable sections.
// The nav is IDENTICAL for every viewer; sections are a static presentational
// grouping over the existing tab namespaces (workspaces.ts owns the tabs + their
// per-namespace layouts — this file never touches persistence). Each section's
// FIRST member is its default (bare `/section`); other members get `/section/sub`.
//
// URL scheme:
//   /                      -> home
//   /portfolio             -> personal        /portfolio/{risk,reports}
//   /markets               -> markets         /markets/{macro,rates,research}
//   /quant                 -> research (equity) /quant/{signals,backtest,lab}
//   /live                  -> ai              /live/algo
// Section-scoped sub-slugs disambiguate the two "research" tabs for free:
// /markets/research = Market Research, /quant (bare) = equity Research.

export interface Section {
  slug: string;
  name: string;
  members: string[]; // tab namespaces, in sub-nav order; [0] is the default
}

export const SECTIONS: Section[] = [
  // Phase 19: journal + notes appended after reports — both take their
  // namespace verbatim as their sub-slug (no SUBSLUG entry needed), so they
  // land at /portfolio/journal and /portfolio/notes exactly as the brief's
  // "Done when" requires.
  { slug: "portfolio", name: "Portfolio", members: ["personal", "risk", "reports", "journal", "notes"] },
  { slug: "markets", name: "Markets", members: ["markets", "macro", "rates", "market-research"] },
  { slug: "quant", name: "Quant", members: ["research", "signals", "backtest", "quant-lab"] },
  { slug: "live", name: "Live", members: ["ai", "algo"] },
];

// namespace -> URL sub-slug (only for NON-default members; the default is the bare
// section path). Anything absent here uses the namespace verbatim as its sub-slug.
const SUBSLUG: Record<string, string> = {
  "market-research": "research",
  "quant-lab": "lab",
};
const subslugFor = (ns: string) => SUBSLUG[ns] ?? ns;

const sectionOf = (ns: string): Section | undefined => SECTIONS.find((s) => s.members.includes(ns));

// namespace -> pathname. Home -> "/". Default member -> "/slug". Others -> "/slug/sub".
// Returns null for an unknown namespace (caller leaves the URL alone).
export function pathForNamespace(ns: string): string | null {
  if (ns === "home") return "/";
  const sec = sectionOf(ns);
  if (!sec) return null;
  if (sec.members[0] === ns) return `/${sec.slug}`;
  return `/${sec.slug}/${subslugFor(ns)}`;
}

// pathname -> namespace, or null if the path matches no section (bare "/" -> "home").
export function namespaceForPath(pathname: string): string | null {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length === 0) return "home";
  const sec = SECTIONS.find((s) => s.slug === parts[0]);
  if (!sec) return null;
  if (parts.length === 1) return sec.members[0]; // bare section -> its default
  const sub = parts[1];
  return sec.members.find((ns) => subslugFor(ns) === sub) ?? null;
}

// Landing namespace for a fresh load: an explicit deep link wins; a bare "/" or an
// unrecognized path lands on Home (a documented design decision — keeps R4 for the
// default path while making deep links real).
export function landingNamespace(pathname: string): string {
  const ns = namespaceForPath(pathname);
  return ns ?? "home";
}

// The section a namespace belongs to (Home is its own implicit section).
export function sectionSlugOf(ns: string): string {
  if (ns === "home") return "home";
  return sectionOf(ns)?.slug ?? "home";
}
