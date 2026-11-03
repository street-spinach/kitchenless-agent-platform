// The owner app's one theme (experience.md S5 "Design system", EX-I43). Screens take
// colours, spacing and type from here and use only the shared components in this folder.

export const colors = {
  background: "#F6F7F9",
  surface: "#FFFFFF",
  text: "#14181F",
  muted: "#5B6472",
  border: "#E2E5EA",
  primary: "#1F6F5C",
  onPrimary: "#FFFFFF",
  info: "#E8F1FB",
  infoText: "#1D4E89",
  warning: "#FDF3E1",
  warningText: "#7A4B00",
  danger: "#B42318",
  skeleton: "#E6E8EC",
  scrim: "rgba(20, 24, 31, 0.4)",
} as const;

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 } as const;

export const radius = { sm: 6, md: 12, lg: 16 } as const;

export const type = {
  title: { fontSize: 20, fontWeight: "600" as const, color: colors.text },
  heading: { fontSize: 16, fontWeight: "600" as const, color: colors.text },
  body: { fontSize: 15, color: colors.text },
  caption: { fontSize: 13, color: colors.muted },
  stat: { fontSize: 28, fontWeight: "700" as const, color: colors.text },
} as const;

export const theme = { colors, spacing, radius, type } as const;
export type Theme = typeof theme;
