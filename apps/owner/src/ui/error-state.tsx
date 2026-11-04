import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing, type } from "./theme";

type Props = { text?: string; onRetry: () => void; testID?: string };

// The error state every screen has. Offline or a failed request shows "Try again" and
// keeps no cached writes (experience.md §16 "Owner app offline").
export function ErrorState({ text = "Something went wrong.", onRetry, testID }: Props) {
  return (
    <View accessibilityRole="alert" style={styles.error} testID={testID}>
      <Text style={[type.body, styles.text]}>{text}</Text>
      <Pressable
        accessibilityRole="button"
        onPress={onRetry}
        style={({ pressed }) => [styles.button, pressed && styles.pressed]}
      >
        <Text style={styles.buttonText}>Try again</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  button: {
    backgroundColor: colors.primary,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonText: { color: colors.onPrimary, fontSize: 15, fontWeight: "600" },
  pressed: { backgroundColor: colors.primaryPressed },
  error: { alignItems: "center", gap: spacing.md, padding: spacing.xl },
  text: { textAlign: "center" },
});
