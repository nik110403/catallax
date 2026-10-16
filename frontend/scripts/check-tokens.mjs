// Drift guard for the design tokens (Phase 14c). theme.ts is the canonical
// source; index.css :root mirrors it for the CSS chrome. This script asserts the
// two agree on every color token, so the fork can't silently diverge. Zero deps —
// run with `npm run check:tokens` (or `node scripts/check-tokens.mjs`). Exits 1 on
// any mismatch/missing token. Colors only: they are what drifts visibly; the type
// scale + font families are edited rarely and compared by eye.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const themeSrc = readFileSync(join(here, "../src/theme.ts"), "utf8");
const cssSrc = readFileSync(join(here, "../src/index.css"), "utf8");

const hex = (s) => (s ? s.toLowerCase() : s);

// theme.ts: `key: "#hex"` for scalar color tokens.
const themeColor = (key) => {
  const m = themeSrc.match(new RegExp(`\\b${key}:\\s*"(#[0-9a-fA-F]{3,8})"`));
  return m ? hex(m[1]) : undefined;
};
// theme.ts: the ordered `chart: [ "#..", ... ]` array.
const chartArr = (() => {
  const block = themeSrc.match(/chart:\s*\[([^\]]*)\]/s);
  if (!block) return [];
  return [...block[1].matchAll(/"(#[0-9a-fA-F]{3,8})"/g)].map((m) => hex(m[1]));
})();
// index.css: `--name: #hex;`
const cssVar = (name) => {
  const m = cssSrc.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{3,8})`));
  return m ? hex(m[1]) : undefined;
};

// theme.ts key  ->  css var name
const pairs = [
  ["bg", "bg"],
  ["panel", "panel"],
  ["panelHeader", "panel-header"],
  ["border", "border"],
  ["text", "text"],
  ["muted", "muted"],
  ["accent", "accent"],
  ["up", "up"],
  ["down", "down"],
  ["warn", "warn"],
  ["grid", "grid"],
];

const errors = [];
for (const [tKey, cName] of pairs) {
  const t = themeColor(tKey);
  const c = cssVar(cName);
  if (!t) errors.push(`theme.ts missing color "${tKey}"`);
  else if (!c) errors.push(`index.css missing --${cName}`);
  else if (t !== c) errors.push(`mismatch ${tKey}: theme.ts ${t} vs --${cName} ${c}`);
}
// chart palette: theme.chart[i] <-> --chart-(i+1)
if (chartArr.length !== 8) errors.push(`theme.chart has ${chartArr.length} entries, expected 8`);
chartArr.forEach((t, i) => {
  const c = cssVar(`chart-${i + 1}`);
  if (!c) errors.push(`index.css missing --chart-${i + 1}`);
  else if (t !== c) errors.push(`mismatch chart[${i}]: theme.ts ${t} vs --chart-${i + 1} ${c}`);
});

if (errors.length) {
  console.error("TOKEN DRIFT — theme.ts and index.css :root disagree:");
  for (const e of errors) console.error("  - " + e);
  process.exit(1);
}
console.log(`tokens OK — ${pairs.length} core + ${chartArr.length} chart colors match across theme.ts and index.css`);
