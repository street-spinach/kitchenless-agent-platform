import type { ReactNode } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing, type } from "./theme";

type Props = {
  title?: string;
  children?: ReactNode;
  // When set, the whole card is one tap target (the Home discount card, XG13).
  onPress?: () => void;
  accessibilityLabel?: string;
  testID?: string;
};

export function Card({ title, children, onPress, accessibilityLabel, testID }: Props) {
  const body = (
    <>
      {title ? <Text style={styles.title}>{title}</Text> : null}
      {children}
    </>
  );
  if (onPress) {
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel ?? title}
        onPress={onPress}
        style={({ pressed }) => [styles.card, pressed && styles.pressed]}
        testID={testID}
      >
        {body}
      </Pressable>
    );
  }
  return (
    <View style={styles.card} testID={testID}>
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
  pressed: { opacity: 0.85 },
  title: type.heading,
});
