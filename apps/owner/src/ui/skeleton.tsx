import { StyleSheet, View } from "react-native";

import { colors, radius, spacing } from "./theme";

type Props = { lines?: number; testID?: string };

// The loading state every screen has: grey bars in place of the content.
export function Skeleton({ lines = 3, testID }: Props) {
  return (
    <View
      accessibilityLabel="Loading"
      accessibilityState={{ busy: true }}
      style={styles.skeleton}
      testID={testID}
    >
      {Array.from({ length: lines }, (_, i) => (
        <View key={i} style={[styles.bar, i === lines - 1 && styles.last]} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: { backgroundColor: colors.skeleton, borderRadius: radius.sm, height: 14 },
  last: { width: "60%" },
  skeleton: { gap: spacing.sm, padding: spacing.lg },
});
