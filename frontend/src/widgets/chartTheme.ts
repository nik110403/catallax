import { ColorType, type ChartOptions, type DeepPartial } from "lightweight-charts";

import { LOCALE } from "../format";
import { theme } from "../theme";

export function chartOptions(): DeepPartial<ChartOptions> {
  return {
    layout: {
      background: { type: ColorType.Solid, color: theme.panel },
      textColor: theme.muted,
      fontFamily: theme.fontMono,
      fontSize: theme.fontSize.micro,
      attributionLogo: false, // hide the lightweight-charts watermark (v4.2+)
    },
    grid: {
      vertLines: { color: theme.grid },
      horzLines: { color: theme.grid },
    },
    rightPriceScale: { borderColor: theme.border },
    timeScale: { borderColor: theme.border, timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    autoSize: false,
    // lightweight-charts defaults `localization.locale` to the browser's
    // language and uses it internally (toLocaleString) for axis tick/crosshair
    // date formatting — same viewer-dependent-separator problem as the widgets'
    // own toLocaleString calls, just inside the library. Pin it explicitly.
    localization: { locale: LOCALE },
  };
}

export const controlStyle: React.CSSProperties = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 4,
  padding: "2px 5px",
  font: "inherit",
  fontSize: theme.fontSize.micro,
};
