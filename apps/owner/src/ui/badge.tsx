import { StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing } from "./theme";

type Props = { text: string; testID?: string };

// A small lime pill, such as the Orders "Live" badge (showcase.md §3.2 `accent`).
export function Badge({ text, testID }: Props) {
  return (
    <View style={styles.badge} testID={testID}>
      <Text style={styles.text}>{text}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    backgroundColor: colors.accent,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
  },
  text: { color: colors.text, fontSize: 12, fontWeight: "700" },
});
