import type { ReactNode } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing, type } from "./theme";

type Props = {
  title?: string;
  children?: ReactNode;
  // When set, the whole card is one tap target (the Home discount card, XG13).
  onPress?: () => void;
  accessibilityLabel?: string;
  // "hero": the green-900 card with white text and a lime ring (showcase.md §3.2).
  tone?: "default" | "hero";
  testID?: string;
};

export function Card({
  title,
  children,
  onPress,
  accessibilityLabel,
  tone = "default",
  testID,
}: Props) {
  const hero = tone === "hero";
  const body = (
    <>
      {title ? <Text style={[styles.title, hero && styles.heroTitle]}>{title}</Text> : null}
      {children}
    </>
  );
  if (onPress) {
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel ?? title}
        onPress={onPress}
        style={({ pressed }) => [styles.card, hero && styles.hero, pressed && styles.pressed]}
        testID={testID}
      >
        {body}
      </Pressable>
    );
  }
  return (
    <View style={[styles.card, hero && styles.hero]} testID={testID}>
      {body}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderColor: colors.border,
    borderRadius: radius.md,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.lg,
  },
  hero: { backgroundColor: colors.hero, borderColor: colors.accent, borderWidth: 2 },
  heroTitle: { color: colors.onHero },
  pressed: { opacity: 0.85 },
  title: type.heading,
});
