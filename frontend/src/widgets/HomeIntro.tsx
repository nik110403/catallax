// Home tab welcome banner (Phase 14b). Wordmark + one-line standfirst, a
// "start here" row that jumps straight to the three tabs a new visitor most
// wants, and the demo-mode notice. Full width, short — the three hero cards
// seeded below it (net-worth / markets-board / market-regime) carry the
// actual snapshot. Not lazy: no heavy deps, and it's the first thing painted.
import { getSession } from "../api/client";
import { requestNav } from "../navigation";
import { theme } from "../theme";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

// Routes via the same PaletteCommand ("nav") the command palette itself
// dispatches — see navigation.ts's requestNav/onNavigate for why a widget
// needs this indirection instead of a prop.
const START_HERE = [
  { label: "Personal", namespace: "personal" },
  { label: "Markets", namespace: "markets" },
  { label: "Market Research", namespace: "market-research" },
];

function HomeIntro(_props: WidgetProps<Record<string, never>>) {
  // Widgets only get `config`/`onConfigChange` (widget-system rule) — re-read
  // the session here rather than threading demo_mode down through App/
  // Dashboard/Panel. Cheap: /api/v1/session is public and uncached client-side.
  const { data: session } = useAsync(() => getSession(), []);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 20,
        height: "100%",
        padding: "0 16px",
      }}
    >
      <div style={{ flex: "1 1 260px", minWidth: 220 }}>
        <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.base, fontWeight: 600, color: theme.muted, lineHeight: 1.45 }}>
          A self-hosted terminal for one person's money and the markets around it: portfolio, macro,
          research and quant tooling in one place.
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 5, flex: "0 0 auto" }}>
        <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.08em", textTransform: "uppercase" }}>
          start here
        </span>
        <div style={{ display: "flex", gap: 6 }}>
          {START_HERE.map((s) => (
            <button
              key={s.namespace}
              type="button"
              className="home-nav-btn"
              onClick={() => requestNav({ kind: "nav", namespace: s.namespace })}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ flex: "1 1 160px", minWidth: 160, textAlign: "right", display: "flex", flexDirection: "column", gap: 4 }}>
        {session?.demo_mode && (
          <span
            style={{
              fontSize: theme.fontSize.micro,
              color: theme.accent,
              alignSelf: "flex-end",
              border: `1px solid ${theme.accent}`,
              borderRadius: 4,
              padding: "2px 6px",
            }}
          >
            Demo data. This is a sample portfolio, not real holdings.
          </span>
        )}
        <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>Press ⌘K to jump anywhere</span>
      </div>
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "home-intro",
  title: "Home",
  component: HomeIntro,
  defaultSize: { w: 12, h: 6 },
  minSize: { w: 6, h: 4 },
  defaultConfig: {},
  category: "Personal",
  description: "Welcome banner: quick nav to Personal / Markets / Market Research",
});
