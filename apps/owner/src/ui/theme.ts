// The owner app's one theme (experience.md S5 "Design system", EX-I43). Screens take
// colours, spacing and type from here and use only the shared components in this folder.

// The Spinach brand tokens (showcase.md §2): a copy of services/api/static/brand.css, keys
// in camelCase. tests/unit/test_brand_tokens.py fails when the two differ.
export const brand = {
  green900: "#123710",
  green700: "#1e6b1c",
  green600: "#268923",
  green500: "#2a9727",
  green50: "#eaf5e9",
  lime: "#8ecc06",
  orange: "#db5f00",
  ink: "#262b25",
  inkSoft: "#323831",
  muted: "#5a6559",
  muted2: "#7b847a",
  line: "rgba(38, 43, 37, 0.08)",
  bg: "#f8fbf7",
  bg2: "#efefee",
  white: "#ffffff",
  danger: "#e03030",
  radiusCard: "24px",
  radiusControl: "12px",
  shadow: "0 10px 30px rgba(38, 43, 37, 0.06)",
  shadowLift: "0 22px 70px rgba(18, 55, 16, 0.14)",
  gradient: "linear-gradient(91deg, #1e6b1c, #2a9727)",
  font: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
} as const;

// showcase.md §3.2.
export const colors = {
  background: brand.bg,
  surface: brand.white,
  text: brand.ink,
  muted: brand.muted,
  border: brand.line,
  primary: brand.green500,
  primaryPressed: brand.green700,
  onPrimary: brand.white,
  // The live badge and the discount card's ring.
  accent: brand.lime,
  // The hero card (the Home discount card): green-900 with white text.
  hero: brand.green900,
  onHero: brand.white,
  // The selected tab.
  tabActive: brand.green700,
  info: brand.green50,
  infoText: brand.green700,
  warning: "#fdf1e6",
  warningText: brand.orange,
  danger: brand.danger,
  skeleton: brand.bg2,
  scrim: "rgba(38, 43, 37, 0.4)",
} as const;

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 } as const;

export const radius = { sm: 12, md: 16, lg: 24 } as const;

export const type = {
  title: { fontSize: 20, fontWeight: "600" as const, color: colors.text },
  heading: { fontSize: 16, fontWeight: "600" as const, color: colors.text },
  body: { fontSize: 15, color: colors.text },
  caption: { fontSize: 13, color: colors.muted },
  stat: { fontSize: 28, fontWeight: "700" as const, color: colors.text },
} as const;

export const theme = { brand, colors, spacing, radius, type } as const;
export type Theme = typeof theme;
