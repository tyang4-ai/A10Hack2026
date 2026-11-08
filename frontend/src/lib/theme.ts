// Colour themes. The CSS lives in app/globals.css ([data-theme] blocks); this list
// drives the switcher. Swatches are [chrome, accent, paper] for the preview.
export const THEMES = [
  { id: "graphite-teal", label: "Graphite and teal", swatches: ["#18181B", "#14B8A6", "#FFFFFF"] },
  { id: "graphite-amber", label: "Graphite and amber", swatches: ["#18181B", "#F59E0B", "#FFFFFF"] },
  { id: "graphite-violet", label: "Graphite and violet", swatches: ["#18181B", "#8B5CF6", "#FFFFFF"] },
  { id: "navy-gold", label: "Navy and gold", swatches: ["#0B1F3A", "#C9A227", "#FBFAF7"] },
  { id: "stone-ember", label: "Stone and ember", swatches: ["#1C1917", "#F97316", "#FFFDF9"] },
  { id: "slate-blue", label: "Slate and blue (previous)", swatches: ["#0F172A", "#F59E0B", "#FFFFFF"] },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];
export const DEFAULT_THEME: ThemeId = "graphite-teal";
export const THEME_KEY = "axelrod-theme";

export function readTheme(): ThemeId {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (t && THEMES.some((x) => x.id === t)) return t as ThemeId;
  } catch {
    // Storage can be blocked; fall back to the default.
  }
  return DEFAULT_THEME;
}

export function applyTheme(t: ThemeId) {
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {
    // Not persisted, but still applied for this page.
  }
}

// Runs before first paint (inlined in layout.tsx) so the saved theme never flashes.
export const THEME_BOOT = `try{var t=localStorage.getItem("${THEME_KEY}");if(t)document.documentElement.dataset.theme=t}catch(e){}`;
